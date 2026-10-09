"""Check a source root for the defects the reference graph does not model, then fold the graph's error level reference checks in beside them.

The reference graph (`agentrc.source_graph`) carries the reference checks: dangling agent, skill and command names, hook registrations that resolve to no file, rule paths named in prose that do not exist, the rules index, the digest's common rule selector, and skill directories with no entry. This module does not reimplement any of that; it reuses the graph's `check` output. What it adds are the checks that are not references at all: an agent's declared tool names against the runtime's own vocabulary, every tool an agent or command body tells it to use against that declaration, a leading `paths:` line left over from an unfenced rule, the binding budget of every rule, the shape of `components.json`, the shape of frontmatter and headings on every page under `docs/` and `.docs/`, rule tier membership, the pinning of workflow actions, and the format of the Python a source root delivers into managed projects.

Usage:
    python -m agentrc.validate [--root PATH]            print every finding, exit 0
    python -m agentrc.validate [--root PATH] --strict   exit 1 when any error level finding exists
    python -m agentrc.validate --checks generic         run only the built in checks (see CHECKS)

Every check is either generic, built in and readable from any source root, or private, supplied by the source root itself. A source root adds private checks in `scripts/private/validate_checks.py`, a module that declares `VALIDATE_CHECKS`, a list of callables that each take the source root and return a list of findings (see `finding`). `--checks generic` runs the built in set only, `--checks private` runs only the module's, and `--checks all`, the default, runs both. A module that cannot be loaded, or a check that raises, is reported as an error finding and never loses the rest of the gate.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from agentrc.paths import SOURCE_VARIABLE, source_root

# Known agent tool names, spelled as the runtime spells them: the union of the
# tool names the runtimes document for an agent's `tools:` line.
KNOWN_TOOLS = (
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "Edit",
    "Glob",
    "Grep",
    "KillShell",
    "NotebookEdit",
    "Read",
    "Skill",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)

# A tool an MCP server contributes is named mcp__<server>__<tool>, so it cannot be listed in advance.
MCP_TOOL = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")

STATUS_VALUES = ("NEW", "PROPOSED", "ACCEPTED", "DRAFT", "INPRG", "REVIEW", "DONE", "ARCHIVED")
CANON_DOC_KEYS = ("domain", "category", "sub-category", "topics", "types", "date-created", "date-revised", "status", "aliases", "tags")
GENERATED_MARKER = "<!-- script-doc:begin -->"
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOP_KEY = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
LIST_ITEM = re.compile(r"^\s+-\s*(.+?)\s*$")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# Where a source root keeps its private checks, relative to the root.
PRIVATE_CHECKS_PATH = "scripts/private/validate_checks.py"


def finding(severity: str, check: str, path: str, message: str) -> dict:
    return {"severity": severity, "check": check, "path": path, "message": message}


@contextlib.contextmanager
def bound_source(root: Path) -> Iterator[None]:
    """Name root as the source root for everything called inside the block, and restore the old value after.

    Modules that resolve the source root lazily (the component manifest's platform extension, the engine name) read `AGENTRC_SOURCE`, so a validation of an explicit root has to bind it.
    """
    previous = os.environ.get(SOURCE_VARIABLE)
    os.environ[SOURCE_VARIABLE] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(SOURCE_VARIABLE, None)
        else:
            os.environ[SOURCE_VARIABLE] = previous


# ----- check 1: agent tool declarations -----


def check_agent_tools(root: Path) -> list[dict]:
    findings: list[dict] = []
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return findings
    for path in sorted(agents_dir.glob("*.md")):
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines or lines[0].strip() != "---":
            findings.append(finding("error", "agent-tools-declaration", f"{rel}:1", "file does not open with frontmatter"))
            continue
        tools_value = None
        tools_line = None
        for offset, line in enumerate(lines[1:], start=2):
            if line.strip() == "---":
                break
            match = re.match(r"^tools:\s*(.*)$", line)
            if match:
                tools_value = match.group(1)
                tools_line = offset
                break
        if tools_value is None:
            findings.append(finding("error", "agent-tools-declaration", rel, "frontmatter has no tools: line"))
            continue
        names = [name.strip() for name in tools_value.split(",") if name.strip()]
        if not names:
            findings.append(finding("error", "agent-tools-declaration", f"{rel}:{tools_line}", "tools: line names no tools"))
        for name in names:
            if name not in KNOWN_TOOLS and not MCP_TOOL.match(name):
                findings.append(finding("error", "agent-tools-declaration", f"{rel}:{tools_line}", f"tools: line names {name!r}, which is not a known tool"))
    return findings


TOOL_NAMES = "|".join(KNOWN_TOOLS)
# A tool the body tells the agent to use: a backticked name, or a name after a verb of use. A bare
# capitalized word is not enough, since "Read the file" and "Edit the list" are ordinary prose.
TOOL_USE = re.compile(r"`(" + TOOL_NAMES + r")`|\b(?:[Uu]se|[Uu]sing|[Cc]all|[Ii]nvoke|[Vv]ia|[Ww]ith|[Rr]un)\s+(?:the\s+)?(" + TOOL_NAMES + r")\b(?![\w-])")
DECLARATION_KEYS = {"agents": "tools", "commands": "allowed-tools"}


def check_agent_tool_use(root: Path) -> list[dict]:
    """A tool the body of an agent or command tells it to use must be in its declaration.

    A runtime grants only the declared tools, so an instruction to use any other one fails when the
    agent reaches it. A command with no allowed-tools line inherits every tool and is not checked.
    Fenced blocks are examples and are skipped.
    """
    findings: list[dict] = []
    for directory, key in DECLARATION_KEYS.items():
        for path in sorted((root / directory).glob("*.md")):
            rel = path.relative_to(root).as_posix()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if not lines or lines[0].strip() != "---":
                continue
            declared: set[str] | None = None
            body_start = len(lines)
            for offset, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    body_start = offset + 1
                    break
                match = re.match(rf"^{re.escape(key)}:\s*(.*)$", line)
                if match:
                    declared = {name.strip().split("(", 1)[0].strip() for name in match.group(1).split(",") if name.strip()}
            if declared is None:
                continue
            in_fence = False
            for number, line in enumerate(lines[body_start:], start=body_start + 1):
                if line.strip().startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    continue
                for match in TOOL_USE.finditer(line):
                    name = match.group(1) or match.group(2)
                    if name not in declared:
                        findings.append(finding("error", "agent-tool-undeclared", f"{rel}:{number}", f"the body uses {name}, which the {key}: line does not declare"))
    return findings


def check_agent_frontmatter(root: Path) -> list[dict]:
    """Reject plain description scalars that YAML treats as mappings."""
    findings: list[dict] = []
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return findings
    for path in sorted(agents_dir.glob("*.md")):
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        for offset, line in enumerate(lines[1:], start=2):
            if line.strip() == "---":
                break
            match = re.match(r"^description:\s*(.*)$", line)
            if not match:
                continue
            value = match.group(1)
            if value and value[0] not in "'\"|>" and re.search(r":(?:\s|$)", value):
                findings.append(finding("error", "agent-frontmatter-yaml", f"{rel}:{offset}", "plain description contains a colon and must be quoted or a block scalar"))
            break
    return findings


# ----- check 2: no leading paths: line in a rule -----


def check_rule_paths_line(root: Path) -> list[dict]:
    findings: list[dict] = []
    rules_dir = root / "rules"
    if not rules_dir.is_dir():
        return findings
    for path in sorted(rules_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        rel = path.relative_to(root).as_posix()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() == "":
                continue
            if line.strip().startswith("paths:"):
                findings.append(finding("error", "rule-leading-paths-line", f"{rel}:1", "the first non-empty line is paths:, which belongs in a fenced example, not the rule body"))
            break
    return findings


# ----- check 2b: the 120-word binding budget -----

BINDING_BUDGET_WORDS = 120
BINDING_SECTION = re.compile(r"^##\s+binding\s*\n(.*?)(?=^##\s|\Z)", re.S | re.M)


def check_rule_binding_budget(root: Path) -> list[dict]:
    """A rule's binding section is capped at 120 words, and that cap is a checked limit."""
    findings: list[dict] = []
    rules_dir = root / "rules"
    if not rules_dir.is_dir():
        return findings
    for path in sorted(rules_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        rel = path.relative_to(root).as_posix()
        match = BINDING_SECTION.search(path.read_text(encoding="utf-8", errors="replace"))
        if not match:
            findings.append(finding("error", "rule-binding-budget", rel, "no ## binding section"))
            continue
        words = len(match.group(1).split())
        if words > BINDING_BUDGET_WORDS:
            findings.append(finding("error", "rule-binding-budget", rel, f"binding section is {words} words, over the budget of {BINDING_BUDGET_WORDS}"))
    return findings


# ----- check 2c: components.json -----


def check_components_manifest(root: Path) -> list[dict]:
    """Validate components.json with `agentrc.components`, so a malformed manifest or a literal secret fails the source gate."""
    path = root / "components.json"
    if not path.exists():
        return []
    from agentrc import components

    try:
        document = components.load(path)
    except components.ManifestError as error:
        return [finding("error", "components-manifest", "components.json", str(error))]
    return [finding("error", "components-manifest", "components.json", problem) for problem in components.validate(document)]


# ----- check 3: docs/ and .docs/ frontmatter and headings -----

# The directories whose Markdown carries the frontmatter, heading and
# provenance shape: a project's public docs/ and a dot-prefixed .docs/ that
# keeps the same rules out of a tree's main listing. Both are walked the same
# way, so every check below sees a .docs/ page exactly as it sees a docs/ page.
DOC_DIRECTORIES = ("docs", ".docs")

# The naming rule reserves these uppercase names; the Markdown style rule has
# a reserved filename's H1 keep its literal uppercase name with extension.
RESERVED_UPPERCASE_NAMES = ("README.md", "CLAUDE.md", "MEMORY.md", "CODEX.md", "GEMINI.md", "AGENTS.md", "PLAN.md", "COMMENTS-PROCESS.md")


def is_reserved_name(filename: str) -> bool:
    return filename in RESERVED_UPPERCASE_NAMES or filename.endswith(".pointer")


def reserved_h1_forms(filename: str) -> tuple[str, ...]:
    """The H1 spellings a reserved file may carry: the literal name, or the name with its extension in capitals (README.MD)."""
    return (filename, filename.upper())


def tracked_or_unignored_markdown(root: Path, directory: Path) -> list[Path]:
    """Markdown files under directory, minus any that git ignores in root's repository.

    One `git check-ignore --stdin -z` call covers the whole listing. A root that is not a
    git repository, or a missing git, drops nothing.
    """
    paths = sorted(directory.rglob("*.md"))
    if not paths:
        return paths
    payload = "".join(f"{path.relative_to(root).as_posix()}\0" for path in paths)
    try:
        result = subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin", "-z"], input=payload, capture_output=True, text=True, check=False)
    except OSError:
        return paths
    if result.returncode not in (0, 1):
        return paths
    ignored = {item for item in result.stdout.split("\0") if item}
    return [path for path in paths if path.relative_to(root).as_posix() not in ignored]


def is_generated_doc(rel: str, text: str) -> bool:
    if GENERATED_MARKER in text:
        return True
    if rel.lstrip(".").startswith("docs/graph/") and Path(rel).suffix in (".json", ".html"):
        return True
    return False


def check_frontmatter_keys(rel: str, fm_lines: list[str]) -> list[dict]:
    findings: list[dict] = []
    keys: list[tuple[str, int, str]] = []
    for offset, line in enumerate(fm_lines):
        match = TOP_KEY.match(line)
        if match:
            keys.append((match.group(1), offset + 2, match.group(2)))
    names = [key[0] for key in keys]
    expected_canon = [name for name in CANON_DOC_KEYS if name in names]
    for missing_key in (name for name in CANON_DOC_KEYS if name not in names):
        findings.append(finding("error", "doc-frontmatter-keys", f"{rel}:1", f"frontmatter is missing the canonical key {missing_key!r}"))
    canon_present = [name for name in names if name in CANON_DOC_KEYS]
    if canon_present != expected_canon:
        findings.append(finding("error", "doc-frontmatter-order", f"{rel}:1", f"canonical frontmatter keys are out of order: got {canon_present}, expected {expected_canon}"))
    further = [(name, line_no) for name, line_no, _ in keys if name not in CANON_DOC_KEYS]
    if further:
        tags_lines = [line_no for name, line_no, _ in keys if name == "tags"]
        tags_line = tags_lines[0] if tags_lines else None
        if tags_line is not None:
            for name, line_no in further:
                if line_no < tags_line:
                    findings.append(finding("error", "doc-frontmatter-order", f"{rel}:{line_no}", f"further key {name!r} appears before tags:, and further keys belong after it"))
        further_names = [name for name, _ in further]
        if further_names != sorted(further_names, key=str.lower):
            findings.append(finding("error", "doc-frontmatter-order", f"{rel}:{further[0][1]}", f"further keys are not sorted A to Z: got {further_names}"))
    for name, line_no, raw in keys:
        if name == "status":
            value = raw.strip().strip("\"'")
            if value not in STATUS_VALUES:
                findings.append(finding("error", "doc-frontmatter-status", f"{rel}:{line_no}", f"status {value!r} is not one of {', '.join(STATUS_VALUES)}"))
    findings.extend(check_provenance_shape(rel, fm_lines, keys))
    return findings


def check_provenance_shape(rel: str, fm_lines: list[str], keys: list[tuple[str, int, str]]) -> list[dict]:
    """models, providers and session-link are checked for placement and shape only when present; the frontmatter is never required to carry them."""
    findings: list[dict] = []
    keyed = {name: (line_no, raw) for name, line_no, raw in keys}
    for array_key in ("models", "providers"):
        if array_key not in keyed:
            continue
        line_no, raw = keyed[array_key]
        raw = raw.strip()
        if raw == "[]":
            continue
        if raw.startswith("["):
            findings.append(finding("error", "doc-provenance-shape", f"{rel}:{line_no}", f"{array_key!r} is flow style; provenance arrays are block style"))
            continue
        if raw:
            findings.append(finding("error", "doc-provenance-shape", f"{rel}:{line_no}", f"{array_key!r} carries an inline value instead of a block style list"))
            continue
        items: list[str] = []
        cursor = line_no - 2 + 1
        while cursor < len(fm_lines):
            item_match = LIST_ITEM.match(fm_lines[cursor])
            if not item_match:
                break
            items.append(item_match.group(1).strip("\"'"))
            cursor += 1
        if not items:
            findings.append(finding("warning", "doc-provenance-shape", f"{rel}:{line_no}", f"{array_key!r} has no list items"))
        elif items != sorted(items, key=str.lower):
            findings.append(finding("error", "doc-provenance-shape", f"{rel}:{line_no}", f"{array_key!r} is not sorted case-insensitively: got {items}"))
    if "session-link" in keyed:
        line_no, raw = keyed["session-link"]
        if not raw.strip():
            findings.append(finding("error", "doc-provenance-shape", f"{rel}:{line_no}", "session-link has no value; write an empty string when the runtime exposes none"))
    return findings


def check_headings(rel: str, body_lines: list[str], body_start: int) -> list[dict]:
    findings: list[dict] = []
    filename = rel.rsplit("/", 1)[-1]
    is_reserved = is_reserved_name(filename)
    stem = filename[:-3] if filename.endswith(".md") else filename
    h1_text = None
    in_fence = False
    for offset, line in enumerate(body_lines):
        line_no = body_start + offset + 1
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        level, text = match.groups()
        if is_reserved and len(level) == 1:
            if text not in reserved_h1_forms(filename):
                findings.append(finding("error", "doc-heading-case", f"{rel}:{line_no}", f"H1 {text!r} of a {filename} file must read exactly {filename.upper()} (or {filename})"))
            h1_text = text
            continue
        if not KEBAB.match(text):
            findings.append(finding("error", "doc-heading-case", f"{rel}:{line_no}", f"heading {text!r} is not lowercase kebab-case"))
        if len(level) == 1:
            h1_text = text
    if not is_reserved and h1_text is not None and h1_text != stem:
        findings.append(finding("error", "doc-h1-mismatch", f"{rel}:1", f"H1 {h1_text!r} does not match the filename stem {stem!r}"))
    return findings


def check_one_doc(rel: str, text: str) -> list[dict]:
    findings: list[dict] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        findings.append(finding("error", "doc-frontmatter-missing", f"{rel}:1", "file does not open with frontmatter (---) on line 1"))
        body_start = 0
    else:
        end = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end = index
                break
        if end is None:
            findings.append(finding("error", "doc-frontmatter-missing", f"{rel}:1", "frontmatter opens with --- but never closes"))
            body_start = 0
        else:
            findings.extend(check_frontmatter_keys(rel, lines[1:end]))
            body_start = end + 1
    findings.extend(check_headings(rel, lines[body_start:], body_start))
    return findings


def check_doc_frontmatter(root: Path) -> list[dict]:
    findings: list[dict] = []
    for name in DOC_DIRECTORIES:
        docs_dir = root / name
        if not docs_dir.is_dir():
            continue
        for path in tracked_or_unignored_markdown(root, docs_dir):
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            if is_generated_doc(rel, text):
                continue
            findings.extend(check_one_doc(rel, text))
    return findings


# ----- check 4: rule tier membership -----


def check_rule_tiers(root: Path) -> list[dict]:
    """rules/tiers.json maps each tier name to the list of rule file names it carries.

    An absent manifest is reported as a skip rather than a failure.
    """
    findings: list[dict] = []
    tiers_path = root / "rules" / "tiers.json"
    if not tiers_path.is_file():
        findings.append(finding("info", "rule-tier-membership", "rules/tiers.json", "rules/tiers.json does not exist; this check is skipped"))
        return findings
    try:
        data = json.loads(tiers_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        findings.append(finding("error", "rule-tier-membership", "rules/tiers.json:1", f"rules/tiers.json is not valid JSON: {exc}"))
        return findings
    if not isinstance(data, dict):
        findings.append(finding("error", "rule-tier-membership", "rules/tiers.json:1", "rules/tiers.json must be a JSON object mapping tier name to a list of rule file names"))
        return findings
    rules_dir = root / "rules"
    rule_files = {path.name for path in rules_dir.glob("*.md") if path.name != "README.md"}
    retired_names: set[str] = set()
    retired_path = rules_dir / "retired.json"
    if retired_path.is_file():
        try:
            retired_data = json.loads(retired_path.read_text(encoding="utf-8"))
            retired_names = {Path(name).name for name in retired_data.get("retired", [])}
        except (json.JSONDecodeError, AttributeError):
            retired_names = set()
    membership: dict[str, list[str]] = {}
    for tier, members in data.items():
        if not isinstance(members, list):
            findings.append(finding("error", "rule-tier-membership", "rules/tiers.json:1", f"tier {tier!r} is not a list"))
            continue
        for name in members:
            if not isinstance(name, str):
                findings.append(finding("error", "rule-tier-membership", "rules/tiers.json:1", f"tier {tier!r} contains non-string member {name!r}"))
                continue
            membership.setdefault(name, []).append(tier)
            if name not in rule_files:
                findings.append(finding("error", "rule-tier-membership", "rules/tiers.json:1", f"tier {tier!r} names {name!r}, which is not a file under rules/"))
            if name in retired_names:
                findings.append(finding("error", "rule-tier-membership", "rules/tiers.json:1", f"tier {tier!r} names {name!r}, which rules/retired.json lists as retired"))
    for name, tiers in membership.items():
        if len(tiers) > 1:
            findings.append(finding("error", "rule-tier-membership", "rules/tiers.json:1", f"{name!r} appears in more than one tier: {', '.join(sorted(tiers))}"))
    for name in sorted(rule_files - set(membership)):
        findings.append(finding("error", "rule-tier-membership", f"rules/{name}", "rule file does not appear in any tier of rules/tiers.json"))
    return findings


# ----- check 5: delegate the reference checks to the source graph -----


def check_reference_graph(root: Path) -> list[dict]:
    try:
        source_graph = importlib.import_module("agentrc.source_graph")
    except ModuleNotFoundError as error:
        if error.name != "agentrc.source_graph":
            raise
        return [finding("info", "reference-graph", ".", "agentrc.source_graph is not available; the reference checks were skipped")]
    graph = source_graph.build_graph(root)
    findings: list[dict] = []
    for item in graph.check(live=False):
        if item["severity"] != "error":
            continue
        findings.append(finding("error", item["check"], item["node"], f"{item['message']} ({item['evidence']})"))
    return findings


# ----- check: workflow action pins -----

WORKFLOW_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*(#.*)?$")
WORKFLOW_SHA_PIN = re.compile(r"^([^@\s]+)@([0-9a-f]{40})$")
WORKFLOW_VERSION_COMMENT = re.compile(r"#\s*v[0-9][\w.\-]*\b")


def check_workflow_action_pins(root: Path) -> list[dict]:
    """Every third-party action a workflow the source root ships uses is pinned to a
    full commit SHA with the released version named in a trailing comment,
    covering both the workflows rendered into managed projects
    (scripts/ci/*.yml) and the root's own .github/workflows/. A tag such as
    @v4 is mutable. A local action (./...) or a container reference needs no
    version pin and is not third-party, so both are skipped.
    """
    findings: list[dict] = []
    paths = sorted(
        p
        for pattern in ("*.yml", "*.yaml")
        for base in (root / ".github" / "workflows", root / "scripts" / "ci")
        for p in base.glob(pattern)
    )
    for path in paths:
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            match = WORKFLOW_USES.match(line)
            if not match:
                continue
            ref, comment = match.group(1), match.group(2)
            if ref.startswith("./") or ref.startswith("docker://"):
                continue
            pin = WORKFLOW_SHA_PIN.match(ref)
            if not pin:
                findings.append(finding("error", "workflow-action-pin", f"{rel}:{lineno}", f"{ref} is not pinned to a full commit SHA"))
                continue
            if not comment or not WORKFLOW_VERSION_COMMENT.search(comment):
                findings.append(finding("error", "workflow-action-pin", f"{rel}:{lineno}", f"{ref} has no released-version comment (e.g. # v4.4.0)"))
    return findings


# ----- check: delivered Python ruff format -----

# The ruff version the delivered pre-commit configuration pins.
RUFF_PINNED_VERSION = "0.7.1"
RUFF_VERSION_LINE = re.compile(r"^ruff\s+(\S+)")


def _ruff_version(command: list[str]) -> str | None:
    """The exact version token `<command> --version` reports, or None if the
    command cannot run. A substring check (`"0.7.1" in output`) would also
    accept 0.7.10, so the version is parsed out and compared exactly. A cold
    `uvx` with no network and nothing cached can hang to the timeout instead
    of failing fast, so TimeoutExpired is treated the same as a command that
    could not run at all, not left to crash the caller.
    """
    try:
        result = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = RUFF_VERSION_LINE.match(result.stdout.strip())
    return match.group(1) if match else None


def _ruff_command() -> list[str] | None:
    """A command that runs ruff at RUFF_PINNED_VERSION, or None when no such
    ruff can be reached. Tries an already-installed `ruff` first (fast, no
    network); falls back to an isolated `uvx ruff@<version>` when uv is on
    PATH. Either way, the returned command is verified with --version
    before use, so a stale PATH `ruff` at the wrong version never silently
    passes as this one.
    """
    ruff = shutil.which("ruff")
    if ruff and _ruff_version([ruff]) == RUFF_PINNED_VERSION:
        return [ruff]
    if shutil.which("uvx"):
        command = ["uvx", f"ruff@{RUFF_PINNED_VERSION}"]
        if _ruff_version(command) == RUFF_PINNED_VERSION:
            return command
    return None


def _delivered_python_files(root: Path) -> list[Path]:
    """The .py files the project delivery copies byte for byte from the source root
    into every managed project: the carried guard files under root/hooks/ and the
    attribution check script. Read from `agentrc.projects` so a future addition
    there is picked up here without editing this check. Only files the source root
    itself carries count; the engine's packaged defaults are not the root's to format.
    """
    from agentrc import projects

    paths = [root / "hooks" / rel for rel in projects.CARRIED_GUARD_FILES if rel.endswith(".py")]
    check_script = root / projects.CHECK_SCRIPT_SOURCE
    if check_script.suffix == ".py":
        paths.append(check_script)
    return [path for path in paths if path.is_file()]


def check_delivered_python_format(root: Path) -> list[dict]:
    """Every .py file the source root delivers into managed projects passes `ruff
    format --check --isolated` at the pinned version, ruff's default style, so a
    managed project's own ruff-format hook does not fail the moment it receives a
    file this check would have caught.
    Skips with an info finding, never an error, when no matching ruff can
    be reached, so an offline or ruff-less environment is not blocked.
    """
    try:
        paths = _delivered_python_files(root)
    except Exception as exc:  # defensive: a broken projects module is reported elsewhere
        return [finding("info", "delivered-python-format", ".", f"could not enumerate delivered Python files: {exc}")]
    if not paths:
        return []

    command = _ruff_command()
    if command is None:
        return [finding("info", "delivered-python-format", ".", f"ruff {RUFF_PINNED_VERSION} is not reachable (no matching `ruff` on PATH and no `uvx`); skipping the delivered-python-format gate")]

    findings: list[dict] = []
    try:
        result = subprocess.run(
            [*command, "format", "--check", "--isolated", *(str(path) for path in paths)],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [finding("info", "delivered-python-format", ".", f"ruff format --check could not run: {exc}; skipping the delivered-python-format gate")]
    if result.returncode != 0:
        reformat_prefix = "Would reformat: "
        reported = False
        for line in result.stdout.splitlines():
            if line.startswith(reformat_prefix):
                target = line[len(reformat_prefix):].strip()
                rel = str(Path(target).relative_to(root)) if Path(target).is_absolute() else target
                findings.append(finding("error", "delivered-python-format", rel, f"not ruff format-clean at {RUFF_PINNED_VERSION} (run: ruff format {rel})"))
                reported = True
        if not reported:
            detail = (result.stdout.strip() + " " + result.stderr.strip()).strip()
            findings.append(finding("error", "delivered-python-format", ".", f"ruff format --check failed: {detail}"))
    return findings


# ----- the source root's private checks -----


def private_checks(root: Path) -> tuple[list, list[dict]]:
    """The callables the source root's private module declares, and any finding that loading it produced.

    A missing module is the absent extension: no checks and no findings. A module that cannot be imported, or that does not declare `VALIDATE_CHECKS` as a list of callables, is an error finding naming it.
    """
    path = root / PRIVATE_CHECKS_PATH
    if not path.is_file():
        return [], []
    spec = importlib.util.spec_from_file_location("agentrc_private_validate_checks", path)
    if spec is None or spec.loader is None:
        return [], [finding("error", "private-checks", PRIVATE_CHECKS_PATH, "cannot be loaded")]
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # a private module may fail in any way, and the gate must still report
        return [], [finding("error", "private-checks", PRIVATE_CHECKS_PATH, f"cannot be imported: {type(error).__name__}: {error}")]
    checks = getattr(module, "VALIDATE_CHECKS", None)
    if not isinstance(checks, (list, tuple)) or not all(callable(check) for check in checks):
        return [], [finding("error", "private-checks", PRIVATE_CHECKS_PATH, "must declare VALIDATE_CHECKS, a list of callables that each take the source root")]
    return list(checks), []


def run_private(root: Path) -> list[dict]:
    """Run the source root's private checks; a check that raises is one error finding."""
    checks, findings = private_checks(root)
    for check in checks:
        name = getattr(check, "__name__", "check")
        try:
            produced = check(root)
        except Exception as error:
            findings.append(finding("error", "private-checks", PRIVATE_CHECKS_PATH, f"{name} raised {type(error).__name__}: {error}"))
            continue
        findings.extend(produced or [])
    return findings


# ----- assembly and output -----

# Every built in check, in the order it runs. Each reads only what any source
# root carries (agents, rules, hooks, docs, components.json, the workflows and
# the delivered Python) and skips a subject the root does not have.
CHECKS: tuple = (
    check_agent_tools,
    check_agent_tool_use,
    check_agent_frontmatter,
    check_rule_paths_line,
    check_rule_binding_budget,
    check_components_manifest,
    check_doc_frontmatter,
    check_rule_tiers,
    check_reference_graph,
    check_workflow_action_pins,
    check_delivered_python_format,
)
CHECK_SETS = ("generic", "private", "all")


def run_all(root: Path, checks: str = "all") -> list[dict]:
    if checks not in CHECK_SETS:
        raise ValueError(f"checks must be one of {', '.join(CHECK_SETS)}, not {checks!r}")
    findings: list[dict] = []
    if checks in ("all", "generic"):
        for check in CHECKS:
            findings += check(root)
    if checks in ("all", "private"):
        findings += run_private(root)
    findings.sort(key=lambda item: (SEVERITY_ORDER.get(item.get("severity"), 3), str(item.get("check")), str(item.get("path"))))
    return findings


def format_finding(item: dict) -> str:
    return f"{item['severity']:<7} {item['check']:<26} {item['path']}: {item['message']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentrc validate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None, help="source root (default: the resolved source root)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any error-level finding exists")
    parser.add_argument("--checks", choices=CHECK_SETS, default="all", help="which class of checks to run (default: all)")
    args = parser.parse_args(argv)

    root = source_root(args.root)
    with bound_source(root):
        findings = run_all(root, args.checks)
    for item in findings:
        print(format_finding(item))
    errors = sum(item["severity"] == "error" for item in findings)
    warnings = sum(item["severity"] == "warning" for item in findings)
    infos = len(findings) - errors - warnings
    print(f"validate: {len(findings)} findings, {errors} errors, {warnings} warnings, {infos} info")
    return 1 if args.strict and errors else 0


if __name__ == "__main__":
    sys.exit(main())
