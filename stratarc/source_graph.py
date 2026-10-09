"""Map every connection between the files of a stratarc source root and flag the ones that break.

One graph over rules, hooks, hook libraries, scripts, adapters, git hooks,
launchd plists, skills, commands, agents, instruction files, indexes,
registries, docs pages, tests, project-local configuration and projects. The
edges are read from the files themselves: hook registrations, shell and Python
invocations and imports, backticked names and paths, Markdown links, the rules
index, the rules digest, the control plane tables, sibling tests and the
COMMON_RULES selector. Every edge carries the file and line it was read from.

The graph is read from the commit, not from the checkout: existence is
``git ls-files``, a path a tracked ``.gitignore`` matches is a ``derived``
node, an untracked file is not a node, and ``root`` is the engine name from
``stratarc.toml`` (``STRATARC_NAME``, else ``stratarc``), never the checkout's
directory. The content of a tracked file is still read from the working tree,
so an uncommitted edit changes the graph; untracked and ignored files do not.

A dangling edge is a reference to something that no longer exists in source.
That is what a change in one file that conflicts with another looks like
mechanically: a hook renamed but still registered, a rule retired but still
selected, a skill that names an agent nobody ships. The same pass checks the
definitions a runtime keys skills by: a skill's name against its directory and
its description against the listing limit. ``check`` prints each one
with its evidence; ``impact`` prints everything a change would reach; ``build``
writes the graph as node-link JSON and as a self-contained viewer.

Usage:
    python -m stratarc.source_graph [--root DIR] build [--out DIR]   write source-graph.json and source-graph.html (default docs/graph/)
    python -m stratarc.source_graph [--root DIR] check [--strict]    print every finding; --strict exits 1 on any error-level finding
    python -m stratarc.source_graph [--root DIR] impact PATH...      print everything that depends on PATH, transitively
    python -m stratarc.source_graph [--root DIR] impact --changed    the same for every file git reports as changed

Without ``--root`` the source root is ``STRATARC_SOURCE``, else the nearest ``stratarc.toml``, else the current directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import weakref
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import unquote

from stratarc.paths import engine_name, source_root

OUT_DIR = Path("docs") / "graph"
GENERATOR = "stratarc.source_graph"


def fixed_root(root: Path | None = None) -> str:
    """The name the graph gives its root: the configured engine name, never the checkout's directory, so two checkouts of one commit write the same bytes."""
    return engine_name(root)


# An edge to a path .gitignore matches: a derived output the commit never holds, so neither present nor missing.
DERIVED_EDGE = "derives"

# The three documentation roots: user documentation, internal work product and contributor documentation.
# A tree holds any subset of them, before or after the split, and every page under each is a doc.
DOC_TOPS = ("docs", ".docs", "developer-docs")
TOP_DIRS = ("rules", "hooks", "scripts", "skills", "commands", "agents", *DOC_TOPS, "projects-root")
ROOT_FILES = {
    "AGENTS.md": "instruction",
    "README.md": "instruction",
    "SCRIPTS.md": "index",
    "control-plane.md": "inventory",
    "permissions.json": "policy",
}
SKIP_DIRS = {"__pycache__", ".git", ".obsidian", "node_modules", ".ruff_cache"}
DOC_SKIP = tuple(f"{top}/{name}/" for top in DOC_TOPS for name in ("prompts", "questions"))
CODE_TYPES = {"hook", "hook-lib", "script", "adapter", "git-hook", "launchd", "test"}
TEXT_TYPES = {"instruction", "index", "inventory", "rule", "skill", "command", "agent", "doc"}
CAPABILITY_TYPES = ("skill", "agent", "command")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
IMPACT_EXPANDS = {"rule", "hook", "hook-lib", "script", "adapter", "git-hook", "launchd", "registry", "policy", "skill", "agent", "command", "project-config"}

BACKTICK = re.compile(r"`([^`\n]+)`")
# A leading dot admits the ``.docs/`` root; ``./x`` still fails, since the dot must be followed by a word character.
TOKEN = re.compile(r"^\.?[A-Za-z0-9][A-Za-z0-9_./-]*$")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
PHRASE = re.compile(r"\b(?:the|a|an|this)\s+`?([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`?\s+(skill|agent|command|rule)s?\b")
USE_PAREN = re.compile(r"\(use (/[a-z][a-z0-9-]*[a-z0-9]|[a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?:\s[^)]*)?\)")
DIGEST_HEADING = re.compile(r"^### ([a-z0-9-]+)(?: \((?:global|common)\))?\s*$")
INDEX_ROW = re.compile(r"^\|\s*`([^`]+\.md)`\s*\|")
TABLE_LINK_ROW = re.compile(r"^\|\s*\[[^\]]*\]\(([^)]+)\)\s*\|(.*)\|\s*$")
PY_IMPORT = re.compile(r"^\s*(?:from\s+\.?([A-Za-z_][A-Za-z0-9_]*)\s+import|import\s+([A-Za-z_][A-Za-z0-9_]*))")
PATH_IN_CODE = re.compile(r"(?<![\w/.$-])((?:hooks|scripts)/[\w./-]*\w\.(?:sh|py|ts|json|plist))(?![\w-])")
HOOK_DIR_REF = re.compile(r"\$HOOK_DIR/((?:lib/)?[\w.-]+\.(?:sh|py|json))")
ROOT_REF = re.compile(r"\$(?:root|ROOT|REPO_ROOT|STRATARC_SOURCE)/((?:hooks|scripts)/[\w./-]*\w\.(?:sh|py|ts))")
TS_INVOKE = re.compile(r"invoke\(\s*\"([\w.-]+\.sh)\"")
COMMON_RULES_BLOCK = re.compile(r"COMMON_RULES\s*=\s*\((.*?)\)", re.S)
QUOTED_NAME = re.compile(r"\"([^\"]+\.md)\"")
# A deployed path under the runtime home names the source file sync copies there.
HOME_PATH = re.compile(r"(?:~|\$HOME|\$\{HOME\})/\.claude/((?:hooks|skills|agents|commands|rules)(?:/[\w.-]+)*)")
# Directories a skill ships beside its SKILL.md; a path under one resolves inside the skill.
SKILL_LOCAL_DIRS = {"references", "reference", "templates", "examples", "assets", "scripts", "agents"}
# Source-root trees a deployed skill, agent or command cannot reach by a relative path:
# it runs in the deploying project's directory, where rules/, hooks/ and scripts/ are that project's own.
PROJECT_RELATIVE_TOPS = ("rules", "hooks", "scripts")
FRONTMATTER_KEY = re.compile(r"^([A-Za-z0-9_-]+):\s?(.*)$")
SKILL_DESCRIPTION_LIMIT = 1024


def frontmatter(lines: list[str]) -> dict[str, tuple[str, int]]:
    """Top-level frontmatter keys as (value, line number), with quoted and block scalars unfolded.

    Enough YAML for the name and description of a definition file; nested mappings and lists are not modelled.
    """
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, tuple[str, int]] = {}
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        match = FRONTMATTER_KEY.match(lines[index])
        if not match:
            index += 1
            continue
        key, raw, line_no = match.group(1), match.group(2).strip(), index + 1
        index += 1
        if raw[:1] in (">", "|"):
            block: list[str] = []
            while index < len(lines) and lines[index].strip() != "---" and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
                block.append(lines[index].strip())
                index += 1
            value = ("\n" if raw.startswith("|") else " ").join(part for part in block if part)
        elif len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            value = raw[1:-1].replace("''", "'") if raw[0] == "'" else raw[1:-1].replace('\\"', '"')
        else:
            # A plain scalar may continue on indented lines, which YAML folds into one line.
            parts = [raw]
            while index < len(lines) and lines[index].strip() != "---" and lines[index].startswith((" ", "\t")) and lines[index].strip():
                parts.append(lines[index].strip())
                index += 1
            value = " ".join(part for part in parts if part)
        result[key] = (value, line_no)
    return result


def classify(rel: str) -> str | None:
    """The node type for a repository-relative path, or None when the file is not modelled."""
    parts = rel.split("/")
    name = parts[-1]
    if len(parts) == 1:
        return ROOT_FILES.get(name)
    top = parts[0]
    if top == "rules" and len(parts) == 2:
        if name == "README.md":
            return "index"
        if name == "retired.json":
            return "registry"
        return "rule" if name.endswith(".md") else None
    if top == "hooks":
        if ".test." in name:
            return "test" if name.endswith((".sh", ".py")) else None
        if len(parts) == 2:
            if name in (
                "hooks.json",
                "claude-worktree-hooks.json",
                "claude-agent-graph-hooks.json",
            ):
                return "registry"
            if name.endswith((".sh", ".ts")):
                return "hook"
            if name.endswith(".md"):
                return "doc"
            return None
        if parts[1] == "lib" and len(parts) == 3 and name.endswith((".py", ".sh", ".json")):
            return "hook-lib"
        return None
    if top == "scripts":
        if ".test." in name:
            return "test" if name.endswith((".sh", ".py")) else None
        if len(parts) == 2:
            return "script" if name.endswith((".py", ".sh")) else None
        if parts[1] == "adapters" and len(parts) == 3 and name.endswith(".py") and name != "__init__.py":
            return "adapter"
        if parts[1] == "git-hooks" and len(parts) == 3:
            return "git-hook"
        if parts[1] == "launchd" and name.endswith(".plist"):
            return "launchd"
        return None
    if top == "skills":
        return "registry" if len(parts) == 2 and name == "sync-exclude.json" else None
    if top == "commands" and len(parts) == 2 and name.endswith(".md"):
        return "command"
    if top == "agents" and len(parts) == 2 and name.endswith(".md"):
        return "agent"
    if top in DOC_TOPS and name.endswith(".md") and not rel.startswith(DOC_SKIP):
        return "doc"
    if top == "projects-root" and len(parts) >= 3:
        return "project-config"
    return None


def boundary(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")


def tracked_files(root: Path) -> set[str] | None:
    """Every path git tracks under ``root``, relative to ``root``, or None when ``root`` is not inside a git work tree.

    A source root may be a subdirectory of a larger repository (an example tree, a monorepo): only the
    paths under it are listed, and only a ``.gitignore`` under it is consulted. The graph is read from the
    commit, not from whatever a checkout happens to hold: an untracked file is not a node, and a gitignored
    path is a derived output. A plain directory (a test fixture), or a subdirectory of a work tree that
    tracks nothing under it, has no commit, so it reads the filesystem instead. Git errors stop discovery rather than silently reading
    the checkout, because that would make the graph depend on untracked files.
    """
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git rev-parse failed: {error}") from error
    if top.returncode != 0:
        if "not a git repository" in top.stderr and not any((parent / ".git").exists() for parent in (root, *root.parents)):
            return None
        raise RuntimeError(f"git rev-parse exited {top.returncode}: {top.stderr.strip()}")
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git ls-files failed: {error}") from error
    if listed.returncode != 0:
        raise RuntimeError(f"git ls-files exited {listed.returncode}: {listed.stderr.strip()}")
    tracked = {path for path in listed.stdout.split("\0") if path}
    if not tracked and Path(top.stdout.strip()).resolve() != root.resolve():
        return None
    return tracked


class Graph:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_index: dict[tuple[str, str, str], dict] = {}
        self.retired: set[str] = set()
        self.excluded_skills: set[str] = set()
        self.findings: list[dict] = []
        self._lines: dict[str, list[str]] = {}
        self.rule_names: dict[str, str] = {}
        self.code_names: dict[str, str] = {}
        self.py_stems: dict[str, str] = {}
        self.hook_names: dict[str, str] = {}
        self.capabilities: dict[str, dict[str, str]] = {t: {} for t in CAPABILITY_TYPES}
        self.tracked: set[str] | None = tracked_files(self.root)
        self._tracked_dirs: set[str] = set()
        for path in self.tracked or ():
            parts = path.split("/")
            for depth in range(1, len(parts)):
                self._tracked_dirs.add("/".join(parts[:depth]))
        self._ignored: dict[tuple[str, bool], bool] = {}
        self._ignore_checkout: Path | None = None

    # ----- discovery -----

    def exists(self, rel: str) -> bool:
        """Whether a repository path is part of the commit: a tracked file, or a directory holding one. A plain directory reads the filesystem."""
        rel = rel.strip("/")
        if self.tracked is None:
            return (self.root / rel).exists()
        return rel in self.tracked or rel in self._tracked_dirs

    def ignored(self, rel: str, directory: bool = False) -> bool:
        """Whether a tracked ``.gitignore`` matches a path the commit does not hold, so it is a derived output rather than a missing file.

        Only a ``.gitignore`` in the commit counts: a match from ``.git/info/exclude`` or the machine's
        ``core.excludesFile`` is one checkout's opinion and would make two checkouts disagree. The caller
        says whether the reference names a directory, and only that form is asked of git: the path with a
        trailing slash for a directory, which is how a ``dir/`` pattern matches a directory that is not on
        disk, and the bare path otherwise. Asking for exactly one form is what lets a ``!dir/`` negation
        after a bare ``dir`` pattern leave the directory unignored, as git does.
        """
        if self.tracked is None or rel in self.tracked:
            return False
        key = (rel, directory)
        if key not in self._ignored:
            candidate = rel + "/" if directory else rel
            self._ignored[key] = self._ignore_source(candidate) in self.tracked
        return self._ignored[key]

    def _ignore_source(self, path: str) -> str:
        """The tracked ignore file whose pattern matches ``path``, or empty when none does."""
        if self._ignore_checkout is None:
            ignore_root = Path(tempfile.mkdtemp(prefix="source-graph-ignore-"))
            weakref.finalize(self, shutil.rmtree, ignore_root, ignore_errors=True)
            self._ignore_checkout = ignore_root
            try:
                initialized = subprocess.run(["git", "-C", str(ignore_root), "init", "-q"], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"source-graph: cannot initialize tracked ignore rules: {error}", file=sys.stderr)
                return ""
            if initialized.returncode != 0:
                print(f"source-graph: cannot initialize tracked ignore rules: {initialized.stderr.strip()}", file=sys.stderr)
                return ""
            for rel in sorted(self.tracked or ()):
                if rel != ".gitignore" and not rel.endswith("/.gitignore"):
                    continue
                target = ignore_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes((self.root / rel).read_bytes())
                except OSError as error:
                    print(f"source-graph: cannot read tracked ignore file {rel}: {error}", file=sys.stderr)
        ignore_root = self._ignore_checkout
        try:
            result = subprocess.run(["git", "-C", str(ignore_root), "-c", "core.excludesFile=/dev/null", "check-ignore", "-v", "-z", "--stdin"], input=path + "\0", capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"source-graph: git check-ignore failed for {path} ({error}); treating it as not ignored", file=sys.stderr)
            return ""
        if result.returncode != 0:
            return ""
        rule = result.stdout.split("\0", 3)
        if len(rule) != 4 or rule[2].startswith("!"):
            return ""
        return rule[0]

    @staticmethod
    def hidden_directory(name: str, depth: int) -> bool:
        """Whether a directory at ``depth`` below the root is left out of the graph: a skipped name, or a dot directory other than the ``.docs`` root."""
        if name in SKIP_DIRS:
            return True
        return name.startswith(".") and not (depth == 0 and name in DOC_TOPS)

    def paths(self) -> list[str]:
        """Every modelled candidate path: the commit's tracked files, or the files on disk for a plain directory, outside the skipped and dot directories."""
        if self.tracked is not None:
            return sorted(path for path in self.tracked if not any(self.hidden_directory(part, depth) for depth, part in enumerate(path.split("/")[:-1])))
        result: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            depth = len(Path(dirpath).relative_to(self.root).parts)
            dirnames[:] = sorted(d for d in dirnames if not self.hidden_directory(d, depth))
            for filename in sorted(filenames):
                result.append((Path(dirpath) / filename).relative_to(self.root).as_posix())
        return result

    def directories(self, top: str) -> list[str]:
        """The directories directly under ``top``: those holding a tracked file, or on disk for a plain directory."""
        if self.tracked is not None:
            names = {path.split("/")[1] for path in self.tracked if path.startswith(f"{top}/") and path.count("/") >= 2}
        else:
            base = self.root / top
            names = {entry.name for entry in base.iterdir() if entry.is_dir()} if base.is_dir() else set()
        return sorted(name for name in names if not name.startswith("."))

    def add_node(self, node_id: str, node_type: str, **attrs) -> dict:
        node = self.nodes.get(node_id)
        if node is None:
            node = {"id": node_id, "type": node_type, "label": attrs.pop("label", node_id.rsplit("/", 1)[-1])}
            self.nodes[node_id] = node
        node.update(attrs)
        return node

    def discover(self) -> None:
        for rel in self.paths():
            node_type = classify(rel)
            if node_type:
                self.add_node(rel, node_type, path=rel)
        for name in self.directories("skills"):
            has_entry = self.exists(f"skills/{name}/SKILL.md")
            self.add_node(f"skills/{name}", "skill", label=name, path=f"skills/{name}/SKILL.md" if has_entry else f"skills/{name}", entry=has_entry)
        for name in self.directories("projects-root"):
            self.add_node(f"project:{name}", "project", label=name, path=f"projects-root/{name}")
        retired = self.root / "rules" / "retired.json"
        if self.exists("rules/retired.json") and retired.is_file():
            try:
                self.retired = {Path(n).stem for n in json.loads(retired.read_text(encoding="utf-8")).get("retired", [])}
            except (json.JSONDecodeError, AttributeError):
                self.retired = set()
        exclude = self.root / "skills" / "sync-exclude.json"
        if self.exists("skills/sync-exclude.json") and exclude.is_file():
            try:
                self.excluded_skills = set(json.loads(exclude.read_text(encoding="utf-8")).get("exclude", {}))
            except (json.JSONDecodeError, AttributeError):
                self.excluded_skills = set()
        for node_id, node in self.nodes.items():
            name = node["label"]
            if node["type"] == "rule":
                self.rule_names[Path(name).stem] = node_id
            elif node["type"] in CAPABILITY_TYPES:
                self.capabilities[node["type"]][Path(name).stem if node["type"] != "skill" else name] = node_id
            elif node["type"] in CODE_TYPES or node["type"] == "registry":
                self.code_names[name] = node_id
                if name.endswith(".py"):
                    self.py_stems[Path(name).stem] = node_id
                if node["type"] == "hook":
                    self.hook_names[name] = node_id

    # ----- helpers -----

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            try:
                self._lines[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._lines[rel] = []
        return self._lines[rel]

    def node_for(self, rel: str) -> str | None:
        rel = rel.strip("/")
        if rel in self.nodes:
            return rel
        parts = rel.split("/")
        if parts[0] == "skills" and len(parts) >= 2 and f"skills/{parts[1]}" in self.nodes:
            return f"skills/{parts[1]}"
        return None

    def add_edge(self, source: str, target: str, edge_type: str, evidence: str, dangling: bool = False, retired: bool = False, **attrs) -> None:
        if source == target:
            return
        key = (source, target, edge_type)
        edge = self._edge_index.get(key)
        if edge is None:
            edge = {"source": source, "target": target, "type": edge_type, "evidence": evidence, "count": 0, "dangling": dangling, "retired": retired}
            edge.update(attrs)
            self._edge_index[key] = edge
            self.edges.append(edge)
        edge["count"] += 1

    def ref(self, source: str, rel: str, edge_type: str, evidence: str, soft: bool = False, **attrs) -> None:
        """Add an edge to a repository path: resolved to its node, skipped when the commit holds it unmodelled, a derived edge when .gitignore matches it, dangling when missing.

        ``soft`` drops a missing target instead of recording it, for tests that name fixture files they create themselves.
        """
        directory = rel.endswith("/")  # normpath erases the slash, and the ignore check needs to know which form was written
        rel = os.path.normpath(rel).replace(os.sep, "/")
        if rel.startswith("..") or rel in (".", ""):
            return
        parts = rel.split("/")
        if not soft and parts[0] == "skills" and len(parts) > 2 and f"skills/{parts[1]}" in self.nodes and not self.exists(rel):
            # node_for folds every file under a skill into the skill node, which would hide a missing reference inside it.
            if self.ignored(rel, directory=directory):
                self.add_edge(source, rel, DERIVED_EDGE, evidence, derived=True, **attrs)
            else:
                self.add_edge(source, rel, edge_type, evidence, dangling=True, **attrs)
            return
        target = self.node_for(rel)
        if target:
            self.add_edge(source, target, edge_type, evidence, **attrs)
            return
        if soft or self.exists(rel):
            return
        if self.ignored(rel, directory=directory):
            self.add_edge(source, rel, DERIVED_EDGE, evidence, derived=True, **attrs)
            return
        self.add_edge(source, rel, edge_type, evidence, dangling=True, retired=Path(rel).stem in self.retired and rel.startswith("rules/"), **attrs)

    def capability(self, source: str, name: str, category: str | None, evidence: str, edge_type: str = "cites", confidence: str = "high") -> None:
        """An edge to a named skill, agent, command or rule; dangling under the named category when unknown.

        ``confidence`` is ``low`` when the name came from prose phrasing such as "the X agent", which also matches adjectives.
        """
        if category == "rule":
            if name in self.rule_names:
                self.add_edge(source, self.rule_names[name], edge_type, evidence)
            elif name in self.retired:
                self.add_edge(source, f"rules/{name}.md", edge_type, evidence, dangling=True, retired=True, confidence=confidence)
            else:
                self.add_edge(source, f"rules/{name}.md", edge_type, evidence, dangling=True, confidence=confidence)
            return
        for kind in CAPABILITY_TYPES:
            if name in self.capabilities[kind]:
                self.add_edge(source, self.capabilities[kind][name], edge_type, evidence)
                return
        if category:
            self.add_edge(source, f"{category}s/{name}", edge_type, evidence, dangling=True, confidence=confidence)
        else:
            self.add_edge(source, f"capability:{name}", edge_type, evidence, dangling=True, confidence=confidence)

    # ----- extraction -----

    def extract(self) -> None:
        for node_id, node in list(self.nodes.items()):
            kind = node["type"]
            if kind == "registry" and node_id.startswith("hooks/"):
                self.extract_registry(node_id)
            elif kind == "registry" and node_id == "skills/sync-exclude.json":
                for name in sorted(self.excluded_skills):
                    self.ref(node_id, f"skills/{name}", "excludes", f"{node_id}:1")
            elif kind in CODE_TYPES:
                self.extract_code(node_id, node)
            elif kind in TEXT_TYPES:
                self.extract_markdown(node_id, node)
            elif kind == "project-config":
                self.extract_project_config(node_id)
        self.extract_common_rules()

    def extract_registry(self, node_id: str) -> None:
        text = "\n".join(self.lines(node_id))
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.findings.append({"severity": "error", "check": "invalid-json", "node": node_id, "message": "registry is not valid JSON", "evidence": f"{node_id}:1"})
            return
        for event, groups in data.items():
            for group in groups if isinstance(groups, list) else []:
                for hook in group.get("hooks", []):
                    command = hook.get("command", "")
                    match = re.search(r"([\w.-]+\.(?:sh|py|ts))(?:\s|$)", command)
                    if not match:
                        continue
                    line = next(
                        (i for i, text in enumerate(self.lines(node_id), 1) if command in text), 1
                    )
                    self.ref(node_id, f"hooks/{match.group(1)}", "registers", f"{node_id}:{line}", event=event, matcher=group.get("matcher", ""))

    def extract_code(self, node_id: str, node: dict) -> None:
        is_test = node["type"] == "test"
        subject = None
        if is_test:
            stem = node["label"].split(".test.")[0]
            parent = node_id.rsplit("/", 1)[0]
            for candidate in (stem, f"{stem}.py", f"{stem}.sh"):
                if f"{parent}/{candidate}" in self.nodes:
                    subject = f"{parent}/{candidate}"
                    break
        seen_this_line: set[tuple[str, str]] = set()
        in_docstring = False
        for number, line in enumerate(self.lines(node_id), 1):
            evidence = f"{node_id}:{number}"
            stripped = line.strip()
            in_comment = in_docstring or (stripped.startswith("#") and not stripped.startswith("#!"))
            if node_id.endswith(".py") and line.count('"""') % 2 == 1:
                in_docstring = not in_docstring
                in_comment = True
            prose_type = "tests" if is_test else ("mentions" if in_comment else "invokes")
            for match in PATH_IN_CODE.finditer(line):
                self.ref(node_id, match.group(1), prose_type, evidence, soft=is_test)
            for match in HOOK_DIR_REF.finditer(line):
                self.ref(node_id, f"hooks/{match.group(1)}", prose_type, evidence, soft=is_test)
            for match in ROOT_REF.finditer(line):
                self.ref(node_id, match.group(1), prose_type, evidence, soft=is_test)
            for match in TS_INVOKE.finditer(line):
                self.ref(node_id, f"hooks/{match.group(1)}", prose_type, evidence, soft=is_test)
            if node_id.endswith(".py"):
                match = PY_IMPORT.match(line)
                if match:
                    stem = match.group(1) or match.group(2)
                    if stem in self.py_stems and self.py_stems[stem] != node_id:
                        self.add_edge(node_id, self.py_stems[stem], "tests" if is_test else "imports", evidence)
            for name, target in self.code_names.items():
                if target == node_id or (node_id, target) in seen_this_line:
                    continue
                if boundary(name).search(line):
                    seen_this_line.add((node_id, target))
                    self.add_edge(node_id, target, "tests" if is_test else "mentions", evidence)
        if subject:
            self.add_edge(node_id, subject, "tests", f"{node_id}:1")

    def extract_markdown(self, node_id: str, node: dict) -> None:
        rel = node.get("path", node_id)
        if node["type"] == "skill" and not node.get("entry"):
            return
        base = rel.rsplit("/", 1)[0] if "/" in rel else ""
        in_fence = False
        for number, line in enumerate(self.lines(rel), 1):
            evidence = f"{rel}:{number}"
            if line.strip().startswith("```"):
                in_fence = not in_fence
            if node_id == "AGENTS.md":
                heading = DIGEST_HEADING.match(line)
                if heading:
                    self.capability(node_id, heading.group(1), "rule", evidence)
                    continue
            if node_id == "rules/README.md":
                row = INDEX_ROW.match(line)
                if row:
                    self.ref(node_id, f"rules/{row.group(1)}", "indexes", evidence)
            # A link written inside an inline code span, or quoted inside a fenced code
            # block, is an example of the syntax, not a link.
            if not in_fence:
                for match in MD_LINK.finditer(BACKTICK.sub("", line)):
                    target = unquote(match.group(1).split("#", 1)[0])
                    if not target or "://" in target or target.startswith("mailto:"):
                        continue
                    self.ref(node_id, os.path.join(base, target), "links", evidence)
            if node["type"] != "doc":
                # Documents discuss runtime output that no source file writes; instructions, rules and definitions run it.
                for match in HOME_PATH.finditer(line):
                    self.ref(node_id, match.group(1).rstrip("."), "cites", evidence)  # a sentence may end on the path
            for match in BACKTICK.finditer(line):
                token = match.group(1).strip()
                if not TOKEN.match(token):
                    continue
                if "/" in token:
                    self.cite_path(node_id, token, evidence)
                    continue
                stem = token[:-3] if token.endswith(".md") else token
                named_rule = token.endswith(".md") or "-" in stem
                if named_rule and (stem in self.rule_names or stem in self.retired):
                    if self.rule_names.get(stem) != node_id:
                        self.capability(node_id, stem, "rule", evidence)
                    continue
                for kind in CAPABILITY_TYPES:
                    if stem in self.capabilities[kind]:
                        self.add_edge(node_id, self.capabilities[kind][stem], "cites", evidence)
                        break
                else:
                    if token in self.code_names:
                        self.add_edge(node_id, self.code_names[token], "cites", evidence)
            if in_fence:
                continue
            for match in PHRASE.finditer(line):
                self.capability(node_id, match.group(1), match.group(2), evidence, confidence="low")
            for match in USE_PAREN.finditer(line):
                self.capability(node_id, match.group(1).lstrip("/"), None, evidence)
        if node_id == "control-plane.md":
            self.extract_control_plane()
        if node["type"] == "skill":
            self.check_skill_definition(node_id, rel)

    def check_skill_definition(self, node_id: str, rel: str) -> None:
        """The frontmatter a runtime keys a skill by: its name must be its directory, its description within the listing limit."""
        fields = frontmatter(self.lines(rel))
        directory = node_id.split("/", 1)[1]
        name, name_line = fields.get("name", ("", 1))
        if name != directory:
            self.findings.append({"severity": "error", "check": "skill-name-mismatch", "node": node_id, "message": f"name {name!r} is not the directory name {directory!r}; runtimes list the skill under one and load it under the other", "evidence": f"{rel}:{name_line}"})
        description, description_line = fields.get("description", ("", 1))
        if len(description) > SKILL_DESCRIPTION_LIMIT:
            self.findings.append({"severity": "error", "check": "description-too-long", "node": node_id, "message": f"description is {len(description)} characters, over the {SKILL_DESCRIPTION_LIMIT} character skill limit", "evidence": f"{rel}:{description_line}"})

    def cite_path(self, node_id: str, token: str, evidence: str) -> None:
        """A backticked repository path. Paths inside a skill resolve against the skill first.

        A skill, agent or command runs in whichever project deploys it, so its ``docs/`` paths are that
        project's documents, not the source root's, and the source root's own paths do not resolve at all.
        """
        top = token.split("/", 1)[0]
        is_definition = self.nodes[node_id]["type"] in CAPABILITY_TYPES
        if node_id.startswith("skills/") and top in SKILL_LOCAL_DIRS:
            if self.exists(f"{node_id}/{token}"):
                return
            if top not in TOP_DIRS:
                self.ref(node_id, f"{node_id}/{token}", "cites", evidence)
                return
        if top not in TOP_DIRS:
            return
        if top in DOC_TOPS and (is_definition or not Path(token).suffix):
            return
        if is_definition and top in PROJECT_RELATIVE_TOPS and not Path(token).suffix:
            return  # a bare directory such as scripts/ is the deploying project's own
        if is_definition and top in PROJECT_RELATIVE_TOPS and self.exists(token):
            self.findings.append({"severity": "error", "check": "project-relative-path", "node": node_id, "message": f"cites {token}, which resolves only inside the source root; the definition runs in the deploying project, so name the rule or skill, or use its runtime path", "evidence": evidence})
        self.ref(node_id, token, "cites", evidence)

    def extract_control_plane(self) -> None:
        columns: list[str] = []
        for number, line in enumerate(self.lines("control-plane.md"), 1):
            if line.startswith("| option |"):
                columns = [c.strip() for c in line.strip().strip("|").split("|")][1:]
                for column in columns:
                    if column != "global":
                        self.add_node(f"project:{column}", "project", label=column)
                continue
            row = TABLE_LINK_ROW.match(line)
            if not row or not columns:
                continue
            source = self.node_for(row.group(1))
            if not source:
                continue
            cells = [c.strip() for c in row.group(2).split("|")]
            for column, cell in zip(columns, cells):
                if cell != "x":
                    continue
                if column == "global":
                    self.nodes[source]["global"] = True
                else:
                    self.add_edge(source, f"project:{column}", "deploys", f"control-plane.md:{number}")

    def extract_project_config(self, node_id: str) -> None:
        """A project-local file configures its project. A ``surfaces.json`` ``copy`` list names the
        instruction surfaces sync-projects renders from the project's AGENTS.md (CODEX.md and so on),
        not files beside it, so the list is kept as an attribute and produces no path edges."""
        project = node_id.split("/")[1]
        self.add_edge(node_id, f"project:{project}", "configures", f"{node_id}:1")
        if node_id.endswith("surfaces.json"):
            try:
                data = json.loads("\n".join(self.lines(node_id)))
            except json.JSONDecodeError:
                self.findings.append({"severity": "error", "check": "invalid-json", "node": node_id, "message": "surfaces file is not valid JSON", "evidence": f"{node_id}:1"})
                return
            self.nodes[node_id]["surfaces"] = list(data.get("copy", []))
            master = f"projects-root/{project}/AGENTS.md"
            if data.get("copy") and master not in self.nodes:
                self.add_edge(node_id, master, "configures", f"{node_id}:1", dangling=True)

    def extract_common_rules(self) -> None:
        node_id = "scripts/rules_digest.py"
        if node_id not in self.nodes:
            return
        text = "\n".join(self.lines(node_id))
        block = COMMON_RULES_BLOCK.search(text)
        if not block:
            return
        for match in QUOTED_NAME.finditer(block.group(1)):
            line = next(
                (i for i, text in enumerate(self.lines(node_id), 1) if match.group(0) in text), 1
            )
            self.capability(node_id, Path(match.group(1)).stem, "rule", f"{node_id}:{line}", edge_type="selects")

    # ----- checks -----

    def incoming(self) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = defaultdict(list)
        for edge in self.edges:
            result[edge["target"]].append(edge)
        return result

    def check(self, live: bool = True) -> list[dict]:
        incoming = self.incoming()
        findings = list(self.findings)

        def add(severity: str, check: str, node: str, message: str, evidence: str) -> None:
            findings.append({"severity": severity, "check": check, "node": node, "message": message, "evidence": evidence})

        for edge in self.edges:
            if not edge["dangling"]:
                continue
            source_type = self.nodes[edge["source"]]["type"]
            message = f"{edge['type']} {edge['target']} which does not exist"
            if edge["retired"]:
                add("error" if source_type in CODE_TYPES else "warning", "retired-reference", edge["source"], f"names retired rule {edge['target']}", edge["evidence"])
            elif edge["type"] == "mentions":
                add("warning", "stale-comment", edge["source"], f"a comment names {edge['target']} which does not exist", edge["evidence"])
            elif edge.get("confidence") == "low":
                add("warning", "possible-dangling-reference", edge["source"], f"prose names {edge['target']} which does not exist; a real handoff or an adjective", edge["evidence"])
            elif source_type == "doc" or edge["target"].split("/", 1)[0] in DOC_TOPS:
                add("warning", "dangling-reference", edge["source"], message, edge["evidence"])
            else:
                add("error", "dangling-reference", edge["source"], message, edge["evidence"])
        for node_id, node in self.nodes.items():
            kind = node["type"]
            inbound = incoming.get(node_id, [])
            if kind == "hook" and node_id.endswith(".sh"):
                if not any(e["type"] == "registers" for e in inbound) and not any(e["type"] in ("invokes", "mentions") and self.nodes[e["source"]]["type"] in CODE_TYPES - {"test"} for e in inbound):
                    add("error", "unregistered-hook", node_id, "no registry names this hook and no script runs it", f"{node_id}:1")
            if kind == "skill" and not node.get("entry") and node["label"] not in self.excluded_skills:
                add("error", "skill-without-entry", node_id, "no SKILL.md and not listed in skills/sync-exclude.json", f"{node_id}/")
            if kind == "rule" and "rules/README.md" in self.nodes and not any(e["source"] == "rules/README.md" and e["type"] == "indexes" for e in inbound):
                add("error", "index-mismatch", node_id, "rule has no row in rules/README.md", f"{node_id}:1")
            if kind in ("script", "adapter", "hook", "git-hook") or (kind == "hook-lib" and node_id.endswith((".py", ".sh"))):
                if node_id.endswith(".ts"):
                    continue
                if not any(e["type"] == "tests" for e in inbound):
                    add("warning", "missing-test", node_id, "no sibling test names this file", f"{node_id}:1")
            if kind in ("script", "hook") and node_id.endswith((".py", ".sh")):
                stem = node["label"].rsplit(".", 1)[0].replace("_", "-")
                # The script pages live under one documentation root at a time: docs/ before the split, developer-docs/ after.
                pages = [f"{top}/scripts/{'hook-' if kind == 'hook' else ''}{stem}.md" for top in DOC_TOPS]
                if not any(page in self.nodes for page in pages):
                    add("warning", "missing-doc-page", node_id, f"no page at {' or '.join(pages)}", f"{node_id}:1")
            if kind == "rule":
                referenced = any(e["type"] in ("cites", "selects") and self.nodes[e["source"]]["type"] in TEXT_TYPES - {"inventory", "index", "doc"} for e in inbound)
                if not referenced:
                    add("info", "unreferenced", node_id, "no instruction file, rule, skill, agent or command names this rule", f"{node_id}:1")
            if kind in ("script", "hook-lib") and node_id.endswith((".py", ".sh")):
                runners = [e for e in inbound if e["type"] in ("invokes", "imports", "registers", "mentions") and self.nodes[e["source"]]["type"] in CODE_TYPES - {"test"} | {"registry"}]
                if not runners:
                    add("info", "unreferenced", node_id, "no hook, script or registry runs this; manual invocation only", f"{node_id}:1")
        names: dict[str, list[str]] = defaultdict(list)
        for kind in CAPABILITY_TYPES:
            for name, node_id in self.capabilities[kind].items():
                names[name].append(node_id)
        for name, ids in names.items():
            if len(ids) > 1:
                add("error", "duplicate-name", ids[0], f"{name} is defined as {', '.join(ids)}; a bare reference is ambiguous", f"{ids[0]}:1")
        if live:
            findings.extend(self.drift_checks())
        findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["check"], f["node"], f["evidence"]))
        return findings

    def drift_checks(self) -> list[dict]:
        """Generated outputs that no longer match their source: the rules digest in AGENTS.md and the control plane."""
        found: list[dict] = []
        environment = self.child_environment()
        agents = self.root / "AGENTS.md"
        if (self.root / "rules").is_dir() and agents.is_file():
            result = subprocess.run([sys.executable, "-m", "stratarc.gen_rules_digest", "--check", str(agents), "--rules-root", str(self.root / "rules")], capture_output=True, text=True, timeout=60, env=environment)
            if result.returncode != 0:
                found.append({"severity": "error", "check": "digest-drift", "node": "AGENTS.md", "message": "rules digest differs from rules/; run python -m stratarc.gen_rules_digest --embed AGENTS.md", "evidence": "AGENTS.md:1"})
        if (self.root / "control-plane.md").is_file():
            result = subprocess.run([sys.executable, "-m", "stratarc.reconcile", "--check", "--root", str(self.root)], capture_output=True, text=True, timeout=60, cwd=self.root, env=environment)
            if result.returncode != 0:
                found.append({"severity": "error", "check": "control-plane-drift", "node": "control-plane.md", "message": "inventory differs from source; run python -m stratarc.reconcile", "evidence": "control-plane.md:1"})
        return found

    def child_environment(self) -> dict[str, str]:
        """The environment a drift check runs under: this process's, naming this graph's root as the source and putting the running engine first on PYTHONPATH."""
        engine = str(Path(__file__).resolve().parent.parent)
        existing = os.environ.get("PYTHONPATH", "")
        return {**os.environ, "STRATARC_SOURCE": str(self.root), "PYTHONPATH": engine + (os.pathsep + existing if existing else "")}

    # ----- impact -----

    def dependents(self, start: list[str]) -> list[tuple[int, str, dict]]:
        """Everything that references a start node, transitively, plus the projects it deploys to.

        Documents, indexes, tests and projects are reported but not expanded: the inventory and the
        script index link every file, so walking through them would return the whole repository.
        A comment mention is reported and not expanded for the same reason.
        """
        incoming = self.incoming()
        outgoing: dict[str, list[dict]] = defaultdict(list)
        for edge in self.edges:
            outgoing[edge["source"]].append(edge)
        seen = set(start)
        queue = deque((node_id, 0) for node_id in start)
        result: list[tuple[int, str, dict]] = []
        while queue:
            node_id, depth = queue.popleft()
            steps = [(e["source"], e) for e in incoming.get(node_id, [])]
            steps += [(e["target"], e) for e in outgoing.get(node_id, []) if e["type"] == "deploys"]
            for other, edge in sorted(steps, key=lambda s: s[0]):
                if other in seen:
                    continue
                seen.add(other)
                result.append((depth + 1, other, edge))
                other_type = self.nodes.get(other, {}).get("type")
                if other_type in IMPACT_EXPANDS and edge["type"] != "mentions":
                    queue.append((other, depth + 1))
        return result

    def changed_paths(self) -> list[str]:
        result = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True)
        paths: list[str] = []
        for line in result.stdout.splitlines():
            path = line[3:].split(" -> ")[-1].strip().strip('"')
            if path:
                paths.append(path)
        return paths

    # ----- output -----

    def to_json(self) -> dict:
        nodes = [dict(n) for n in self.nodes.values()]
        known = set(self.nodes)
        for edge in self.edges:
            if edge["target"] not in known:
                known.add(edge["target"])
                nodes.append({"id": edge["target"], "type": "derived" if edge.get("derived") else "missing", "label": edge["target"], "path": None})
        return {"directed": True, "multigraph": False, "graph": {"root": fixed_root(self.root), "generator": GENERATOR}, "nodes": nodes, "links": [dict(e) for e in self.edges]}


def build_graph(root: Path) -> Graph:
    graph = Graph(root)
    graph.discover()
    graph.extract()
    return graph


def render_html(payload: dict, findings: list[dict], fragment: bool = False) -> str:
    """The viewer as a full page, or as a fragment (title, styles, body and scripts) for a host that supplies the document skeleton."""
    data = json.dumps({"graph": payload, "findings": findings}, separators=(",", ":")).replace("</", "<\\/")
    return (FRAGMENT if fragment else TEMPLATE).replace("__DATA__", data)


def format_finding(finding: dict) -> str:
    return f"{finding['severity']:<7} {finding['check']:<22} {finding['node']}: {finding['message']} ({finding['evidence']})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stratarc source-graph", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None, help="source root (default: $STRATARC_SOURCE, then the nearest stratarc.toml, then the current directory)")
    sub = parser.add_subparsers(dest="mode", required=True)
    build = sub.add_parser("build", help="write the graph JSON and the viewer")
    build.add_argument("--out", type=Path, default=None, help="output directory (default: docs/graph under the root)")
    build.add_argument("--fragment", type=Path, default=None, help="also write the viewer without the document skeleton to this path, for publishing as an artifact")
    check = sub.add_parser("check", help="print findings")
    check.add_argument("--strict", action="store_true", help="exit 1 when any error-level finding exists")
    check.add_argument("--no-live", action="store_true", help="skip the digest and control plane drift subprocesses")
    impact = sub.add_parser("impact", help="print everything that depends on the given paths")
    impact.add_argument("paths", nargs="*", help="repository-relative or absolute paths")
    impact.add_argument("--changed", action="store_true", help="use every path git reports as changed")
    args = parser.parse_args(argv)

    root = source_root(args.root)
    graph = build_graph(root)

    if args.mode == "build":
        out = (args.out or root / OUT_DIR).resolve()
        out.mkdir(parents=True, exist_ok=True)
        findings = graph.check(live=True)
        payload = graph.to_json()
        (out / "source-graph.json").write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        (out / "source-graph.html").write_text(render_html(payload, findings), encoding="utf-8")
        if args.fragment:
            args.fragment.parent.mkdir(parents=True, exist_ok=True)
            args.fragment.write_text(render_html(payload, findings, fragment=True), encoding="utf-8")
        errors = sum(f["severity"] == "error" for f in findings)
        print(f"source-graph: {len(payload['nodes'])} nodes, {len(payload['links'])} edges, {len(findings)} findings ({errors} errors) written to {out}")
        return 0

    if args.mode == "check":
        findings = graph.check(live=not args.no_live)
        for finding in findings:
            print(format_finding(finding))
        errors = sum(f["severity"] == "error" for f in findings)
        warnings = sum(f["severity"] == "warning" for f in findings)
        print(f"source-graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges, {errors} errors, {warnings} warnings, {len(findings) - errors - warnings} info")
        return 1 if args.strict and errors else 0

    paths = graph.changed_paths() if args.changed else list(args.paths)
    if not paths:
        print("source-graph: nothing to assess", file=sys.stderr)
        return 2
    starts: list[str] = []
    for raw in paths:
        path = Path(raw)
        rel = path.resolve().relative_to(root).as_posix() if path.is_absolute() else raw
        node_id = graph.node_for(rel)
        if node_id:
            starts.append(node_id)
        else:
            print(f"source-graph: {rel} is not in the graph")
    if not starts:
        return 2
    for node_id in starts:
        print(f"impact of {node_id}")
        for depth, other, edge in graph.dependents([node_id]):
            print(f"  {depth}  {other}  via {edge['type']} at {edge['evidence']}")
    return 0


FRAGMENT = r"""<title>source graph</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root { color-scheme: light dark; --bg: #f3f4f6; --panel: #ffffff; --ink: #1b1e24; --muted: #6a707c; --line: #d7dbe2; --accent: #2b5fd9; --danger: #b3261e; --warn: #9a5b00; --ok: #2f7a3e; --mono: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace; --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #14161a; --panel: #1c1f25; --ink: #e8eaee; --muted: #98a0ad; --line: #2b3038; --accent: #7ea2ff; --danger: #ff7b72; --warn: #f0b458; --ok: #6fcf85; } }
:root[data-theme="dark"] { --bg: #14161a; --panel: #1c1f25; --ink: #e8eaee; --muted: #98a0ad; --line: #2b3038; --accent: #7ea2ff; --danger: #ff7b72; --warn: #f0b458; --ok: #6fcf85; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 13px/1.45 var(--sans); height: 100vh; display: flex; flex-direction: column; }
code, .evidence, .node-title, svg text { font-family: var(--mono); }
header { padding: 10px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 14px; align-items: center; flex-wrap: wrap; background: var(--panel); }
header h1 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
header .stats { display: flex; gap: 6px; flex-wrap: wrap; }
.chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); font-size: 12px; font-variant-numeric: tabular-nums; }
.chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.chip.error::before { background: var(--danger); }
.chip.warning::before { background: var(--warn); }
.chip.clean::before { background: var(--ok); }
main { flex: 1; display: flex; min-height: 0; }
aside { width: 240px; padding: 12px 16px; border-right: 1px solid var(--line); overflow: auto; }
#panel { width: 340px; padding: 12px 16px; border-left: 1px solid var(--line); overflow: auto; }
#stage { flex: 1; min-width: 0; position: relative; }
svg { width: 100%; height: 100%; display: block; }
aside h2, #panel h2 { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 14px 0 6px; }
aside label { display: flex; align-items: center; gap: 6px; padding: 2px 0; cursor: pointer; font-variant-numeric: tabular-nums; }
.swatch { width: 10px; height: 10px; border-radius: 50%; flex: none; }
input[type=search] { width: 100%; padding: 6px 8px; border: 1px solid var(--line); border-radius: 4px; background: var(--panel); color: var(--ink); font: inherit; }
input[type=search]:focus, button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.finding { padding: 6px 8px; border-left: 3px solid var(--line); margin: 4px 0; cursor: pointer; background: var(--panel); }
.finding.error { border-left-color: var(--danger); }
.finding.warning { border-left-color: var(--warn); }
.finding .check { font-weight: 600; }
.finding .evidence, .edge .evidence { color: var(--muted); font-size: 11px; }
.edge { padding: 4px 0; border-bottom: 1px solid var(--line); }
.edge code { font-size: 11px; color: var(--muted); }
.edge a { color: var(--accent); text-decoration: none; word-break: break-all; }
.edge a:hover { text-decoration: underline; }
.node-title { font-weight: 500; font-size: 14px; word-break: break-all; }
.pill { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; color: #fff; }
button { background: var(--panel); color: var(--ink); border: 1px solid var(--line); border-radius: 4px; padding: 4px 10px; cursor: pointer; font: inherit; }
.hint { color: var(--muted); }
@media (max-width: 900px) { main { flex-direction: column; } aside, #panel { width: auto; max-height: 30vh; border: 0; border-bottom: 1px solid var(--line); } #stage { min-height: 50vh; } }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
<header>
  <h1>source graph</h1>
  <span class="stats" id="stats"></span>
  <span class="hint">click a node for what it references and what references it; dangling edges are red</span>
</header>
<main>
  <aside>
    <input type="search" id="search" placeholder="find a node">
    <h2>node types</h2>
    <div id="types"></div>
    <h2>edge types</h2>
    <div id="edgetypes"></div>
    <h2>view</h2>
    <label><input type="checkbox" id="onlyDangling"> only dangling edges</label>
    <label><input type="checkbox" id="showLabels" checked> labels</label>
    <p><button id="reset">reset</button></p>
  </aside>
  <div id="stage"><svg></svg></div>
  <section id="panel"></section>
</main>
<script id="data" type="application/json">__DATA__</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<script>
(function () {
  const raw = JSON.parse(document.getElementById('data').textContent);
  const nodes = raw.graph.nodes.map(n => Object.assign({}, n));
  const byId = new Map(nodes.map(n => [n.id, n]));
  const links = raw.graph.links.map(l => Object.assign({}, l, { source: byId.get(l.source), target: byId.get(l.target) })).filter(l => l.source && l.target);
  const findings = raw.findings;
  const COLORS = { rule: '#2f6fed', hook: '#e4572e', 'hook-lib': '#f3a712', script: '#17a398', adapter: '#0f766e', 'git-hook': '#8a5a44', launchd: '#a16207', test: '#9ca3af', registry: '#7c3aed', policy: '#7c3aed', skill: '#d946ef', command: '#ec4899', agent: '#c026d3', instruction: '#0ea5e9', index: '#0891b2', inventory: '#0891b2', doc: '#94a3b8', project: '#65a30d', 'project-config': '#84cc16', missing: '#c62828', derived: '#64748b' };
  const color = t => COLORS[t] || '#888';
  const typeCounts = d3.rollup(nodes, v => v.length, d => d.type);
  const edgeCounts = d3.rollup(links, v => v.length, d => d.type);
  const visibleTypes = new Set(typeCounts.keys());
  const visibleEdges = new Set(edgeCounts.keys());
  let selected = null;
  let query = '';

  const errors = findings.filter(f => f.severity === 'error').length, warnings = findings.filter(f => f.severity === 'warning').length;
  document.getElementById('stats').innerHTML = '<span class="chip">' + nodes.length + ' nodes</span><span class="chip">' + links.length + ' edges</span>' + (errors ? '<span class="chip error">' + errors + ' errors</span>' : '<span class="chip clean">no errors</span>') + (warnings ? '<span class="chip warning">' + warnings + ' warnings</span>' : '');

  const types = d3.select('#types');
  for (const t of [...typeCounts.keys()].sort()) {
    const label = types.append('label');
    label.append('input').attr('type', 'checkbox').property('checked', true).on('change', function () { this.checked ? visibleTypes.add(t) : visibleTypes.delete(t); refresh(); });
    label.append('span').attr('class', 'swatch').style('background', color(t));
    label.append('span').text(t + ' (' + typeCounts.get(t) + ')');
  }
  const edgeTypes = d3.select('#edgetypes');
  for (const t of [...edgeCounts.keys()].sort()) {
    const label = edgeTypes.append('label');
    label.append('input').attr('type', 'checkbox').property('checked', true).on('change', function () { this.checked ? visibleEdges.add(t) : visibleEdges.delete(t); refresh(); });
    label.append('span').text(t + ' (' + edgeCounts.get(t) + ')');
  }

  const svg = d3.select('svg');
  const stage = document.getElementById('stage');
  const width = () => stage.clientWidth, height = () => stage.clientHeight;
  const g = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.1, 6]).on('zoom', e => g.attr('transform', e.transform)));
  svg.append('defs').append('marker').attr('id', 'arrow').attr('viewBox', '0 -4 8 8').attr('refX', 14).attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto').append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', '#999');

  const link = g.append('g').selectAll('line').data(links).join('line')
    .attr('stroke', d => d.dangling ? '#c62828' : '#999').attr('stroke-opacity', 0.5)
    .attr('stroke-width', d => d.dangling ? 2 : 1).attr('stroke-dasharray', d => d.dangling ? '4 3' : null)
    .attr('marker-end', 'url(#arrow)');
  const node = g.append('g').selectAll('g').data(nodes).join('g').attr('cursor', 'pointer')
    .call(d3.drag().on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }).on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; }).on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }))
    .on('click', (e, d) => { e.stopPropagation(); select(d.id); });
  node.append('circle').attr('r', d => d.type === 'project' ? 9 : d.type === 'missing' ? 5 : 6).attr('fill', d => color(d.type)).attr('stroke', d => d.type === 'missing' ? '#c62828' : '#fff').attr('stroke-width', 1.5);
  node.append('text').text(d => d.label).attr('x', 9).attr('y', 4).attr('font-size', 10).attr('fill', 'currentColor').attr('pointer-events', 'none');
  node.append('title').text(d => d.id + ' (' + d.type + ')');
  svg.on('click', () => select(null));

  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(l => l.type === 'deploys' ? 120 : 45).strength(0.4))
    .force('charge', d3.forceManyBody().strength(-140))
    .force('center', d3.forceCenter(width() / 2, height() / 2))
    .force('collide', d3.forceCollide(12))
    .on('tick', () => {
      link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      node.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
    });

  function neighbours(id) {
    const set = new Set([id]);
    for (const l of links) { if (l.source.id === id) set.add(l.target.id); if (l.target.id === id) set.add(l.source.id); }
    return set;
  }

  function refresh() {
    const onlyDangling = document.getElementById('onlyDangling').checked;
    const showLabels = document.getElementById('showLabels').checked;
    const linkVisible = l => visibleEdges.has(l.type) && visibleTypes.has(l.source.type) && visibleTypes.has(l.target.type) && (!onlyDangling || l.dangling);
    const near = selected ? neighbours(selected) : null;
    const matches = query ? new Set(nodes.filter(n => n.id.toLowerCase().includes(query)).map(n => n.id)) : null;
    node.style('display', d => visibleTypes.has(d.type) ? null : 'none')
      .attr('opacity', d => (near && !near.has(d.id)) ? 0.15 : 1)
      .select('circle').attr('stroke', d => d.id === selected ? '#000' : (matches && matches.has(d.id)) ? '#f59e0b' : d.type === 'missing' ? '#c62828' : '#fff').attr('stroke-width', d => d.id === selected || (matches && matches.has(d.id)) ? 3 : 1.5);
    node.select('text').style('display', showLabels ? null : 'none');
    link.style('display', l => linkVisible(l) ? null : 'none')
      .attr('stroke-opacity', l => near ? ((l.source.id === selected || l.target.id === selected) ? 0.9 : 0.05) : 0.5);
  }

  function pill(t) { return '<span class="pill" style="background:' + color(t) + '">' + t + '</span>'; }
  function esc(s) { return String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }

  function renderPanel() {
    const panel = document.getElementById('panel');
    if (!selected) {
      const groups = d3.group(findings, f => f.severity);
      let html = '<h2>findings</h2>';
      for (const sev of ['error', 'warning', 'info']) {
        const list = groups.get(sev) || [];
        if (!list.length) continue;
        html += '<h2>' + sev + ' (' + list.length + ')</h2>';
        for (const f of list) html += '<div class="finding ' + sev + '" data-node="' + esc(f.node) + '"><span class="check">' + esc(f.check) + '</span> ' + esc(f.node) + '<br>' + esc(f.message) + '<br><span class="evidence">' + esc(f.evidence) + '</span></div>';
      }
      panel.innerHTML = html;
      panel.querySelectorAll('.finding').forEach(el => el.addEventListener('click', () => select(el.dataset.node)));
      return;
    }
    const n = byId.get(selected);
    const out = links.filter(l => l.source.id === selected), inn = links.filter(l => l.target.id === selected);
    const own = findings.filter(f => f.node === selected);
    let html = '<div class="node-title">' + esc(n.id) + '</div><p>' + pill(n.type) + (n.global ? ' global opt-in' : '') + (n.path ? ' <code>' + esc(n.path) + '</code>' : '') + '</p>';
    if (own.length) { html += '<h2>findings (' + own.length + ')</h2>'; for (const f of own) html += '<div class="finding ' + f.severity + '"><span class="check">' + esc(f.check) + '</span> ' + esc(f.message) + '<br><span class="evidence">' + esc(f.evidence) + '</span></div>'; }
    html += '<h2>references (' + out.length + ')</h2>';
    for (const l of out) html += '<div class="edge"><code>' + esc(l.type) + '</code> <a href="#" data-node="' + esc(l.target.id) + '">' + esc(l.target.id) + '</a>' + (l.dangling ? ' <span style="color:var(--danger)">missing</span>' : '') + '<br><span class="evidence">' + esc(l.evidence) + (l.count > 1 ? ' and ' + (l.count - 1) + ' more' : '') + '</span></div>';
    html += '<h2>referenced by (' + inn.length + ')</h2>';
    for (const l of inn) html += '<div class="edge"><code>' + esc(l.type) + '</code> <a href="#" data-node="' + esc(l.source.id) + '">' + esc(l.source.id) + '</a><br><span class="evidence">' + esc(l.evidence) + '</span></div>';
    panel.innerHTML = html;
    panel.querySelectorAll('a[data-node]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); select(a.dataset.node); }));
  }

  function select(id) { selected = id && byId.has(id) ? id : null; renderPanel(); refresh(); }

  document.getElementById('search').addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); refresh(); });
  document.getElementById('onlyDangling').addEventListener('change', refresh);
  document.getElementById('showLabels').addEventListener('change', refresh);
  document.getElementById('reset').addEventListener('click', () => { selected = null; query = ''; document.getElementById('search').value = ''; document.getElementById('onlyDangling').checked = false; visibleTypes.clear(); typeCounts.forEach((v, k) => visibleTypes.add(k)); visibleEdges.clear(); edgeCounts.forEach((v, k) => visibleEdges.add(k)); document.querySelectorAll('aside input[type=checkbox]').forEach(i => { if (i.id !== 'onlyDangling' && i.id !== 'showLabels') i.checked = true; }); renderPanel(); refresh(); });
  window.addEventListener('resize', () => sim.force('center', d3.forceCenter(width() / 2, height() / 2)).alpha(0.3).restart());
  renderPanel();
  refresh();
})();
</script>
"""

TEMPLATE = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n' + FRAGMENT + "</html>\n"


if __name__ == "__main__":
    raise SystemExit(main())
