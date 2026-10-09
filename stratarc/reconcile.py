"""Reconcile control-plane.md with the managed source tree and project layout.

The control plane keeps opt-in choices. This module owns inventory, ordering, file links, project manifest rows, project columns, and project snapshots. It is safe to run repeatedly and makes no change when the repository state is already represented.

Run from a linked worktree, it writes and checks only that worktree's control-plane.md; the per-project snapshots are rendered only from the primary checkout (see is_linked_worktree).

The source root is `--root` when given, else `STRATARC_SOURCE`, else the nearest `stratarc.toml` at or above the current directory, else the current directory (`stratarc.paths.source_root`). Nothing here reads a path relative to the module itself.

Every path is derived when it is used, never at import, so a test or a command line flag can redirect the source root, the home and the projects root after the module is loaded.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

from stratarc.adapters._common import runtime_registry
from stratarc.config import ConfigError
from stratarc.control_plane import ControlPlane
from stratarc.paths import engine_name, projects_dir, source_root
from stratarc.public_targets import PublicTargetsUnreadable, is_public_checkout, load_public_targets, origin_slug
from stratarc.staging import PROJECT_DIRECTORIES as CONFIG_DIRECTORIES

CONTROL_PLANE_NAME = "control-plane.md"
SNAPSHOT_DIR = ".docs"
GENERATED_PARTS = {"__pycache__", ".pytest_cache"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}

# Set by the --root flag for the length of one main() call; None means the shared resolution in stratarc.paths.
_root_flag: pathlib.Path | None = None


def configure_root(root: pathlib.Path) -> None:
    """Point the module at *root* (the --root flag) until the next call."""
    global _root_flag
    _root_flag = pathlib.Path(root).expanduser().resolve()


def root() -> pathlib.Path:
    """The source root being reconciled."""
    return _root_flag if _root_flag is not None else source_root()


def control_plane_path() -> pathlib.Path:
    """The control-plane.md this run reads and writes."""
    return root() / CONTROL_PLANE_NAME


def snapshot_name() -> str:
    """The file name of the per-project snapshot, derived from the engine name."""
    return f"{engine_name(root())}-control-plane.md"


def source_path(path: pathlib.Path) -> str:
    return path.relative_to(root()).as_posix()


def _infer_tier(name: str) -> str:
    """Normalize legacy trust directory names to a canonical tier label."""
    if name.endswith("-trust"):
        name = name[: -len("-trust")]
    if "-" in name and name[0].isdigit():
        _, name = name.split("-", 1)
    if name:
        return name
    return "normal"


def _git_env() -> dict[str, str]:
    """Environment for a git subprocess call, with repository-location variables removed.

    This module can run from the `post-commit` hook of a linked worktree. A git hook inherits GIT_DIR, GIT_COMMON_DIR, GIT_WORK_TREE, and related variables from the commit that triggered it. Left in place, `git -C <other-repo> ...` ignores -C and reads whichever repository those variables name instead of the one it was pointed at, which is how a linked worktree's post-commit misattributes another project's origin to the repository the hook actually ran in.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def canonical_checkout() -> pathlib.Path:
    """Return the primary checkout for this repository, resolved via git.

    The source root is whatever directory was asked for, so a run from a linked worktree sees the worktree path, not the canonical checkout. `git rev-parse --git-common-dir` always names the shared `.git` directory, which sits inside the primary checkout even when the run starts in a linked worktree, so its parent is the primary checkout. Falls back to the source root when git cannot answer, which keeps a plain (non-worktree) checkout behaving exactly as before.
    """
    base = root()
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return base
    if result.returncode != 0:
        return base
    common_dir = pathlib.Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = base / common_dir
    return common_dir.resolve().parent


def is_linked_worktree() -> bool:
    """True when the source root is a linked worktree rather than the primary checkout.

    A linked worktree carries a branch's unmerged inventory. Its run keeps its own control-plane.md current, but the per-project snapshots describe what is deployed, which only the primary checkout's run may render; otherwise every branch that changes the inventory rewrites live snapshots with work that has not merged, and every --check from such a branch fails on snapshots it has no business comparing.
    """
    return canonical_checkout().resolve() != root().resolve()


def _is_canonical(path: pathlib.Path, canonical: pathlib.Path) -> bool:
    """True when path is the running source root or the git-resolved primary checkout."""
    resolved = path.resolve()
    return resolved == root().resolve() or resolved == canonical.resolve()


def _is_vault_candidate(path: pathlib.Path) -> bool:
    """A non-git directory under active/ that looks like an Obsidian vault.

    F-36: this directory has no `.git`, so the checkout scan never finds it and it receives no instruction file. Recognising it here registers it as `unmanaged` without enrolling it: it carries no origin, gets no generated snapshot (active_checkout_paths stays git-only), and gets a control-plane column only once a projects-root/ entry for it also exists.
    """
    return (path / ".obsidian").is_dir() or (path / "README.md").is_file()


def public_targets() -> frozenset[str]:
    """The entries of projects-root/public-targets.json: project names and owner/repo slugs.

    A public target is a repository the source tree must not write into. Discovery never records it: no manifest row, no column, no snapshot, no note. A missing file declares nothing. One that exists and cannot be read raises ManifestUnreadable, for the same reason components.json does: reading it as empty would register a public checkout as an ordinary project and write a snapshot into it on the very run meant to keep that from happening. `stratarc.public_targets` is the one reader of the file.
    """
    try:
        return load_public_targets(root())
    except PublicTargetsUnreadable as error:
        raise ManifestUnreadable(str(error)) from None


def is_public(path: pathlib.Path, public: frozenset[str]) -> bool:
    """True when *path* is a public target: its name is listed, or the checkout there is (by its main worktree's name or its origin slug, so a worktree or a differently named clone of a public repository is skipped too)."""
    return path.name in public or is_public_checkout(path, public)


def active_checkout_paths() -> dict[str, pathlib.Path]:
    """Return active checkout names and paths from the managed projects tree."""
    active = projects_dir(root=root()) / "active"
    found: dict[str, pathlib.Path] = {}
    if not active.is_dir():
        return found
    canonical = canonical_checkout()
    public = public_targets()
    for entry in sorted(active.iterdir(), key=lambda path: path.name.casefold()):
        if not entry.is_dir() or entry.name.startswith(".") or _is_canonical(entry, canonical):
            continue
        if (entry / ".git").exists():
            if not is_public(entry, public):
                found[entry.name] = entry
            continue
        for project in sorted(entry.iterdir(), key=lambda path: path.name.casefold()):
            if (
                _is_canonical(project, canonical)
                or not project.is_dir()
                or project.name.startswith(".")
                or project.name.startswith("_")
                or is_public(project, public)
            ):
                continue
            if (project / ".git").exists():
                found[project.name] = project
    return found


def checkout_records() -> dict[str, tuple[str, str, pathlib.Path]]:
    """Discover managed checkouts as status, tier, and path records."""
    records: dict[str, tuple[str, str, pathlib.Path]] = {}
    projects = projects_dir(root=root())
    if not projects.is_dir():
        return records
    canonical = canonical_checkout()
    public = public_targets()
    for status_dir in sorted(projects.iterdir(), key=lambda path: path.name.casefold()):
        if not status_dir.is_dir() or status_dir.name.startswith((".", "_")):
            continue
        status = status_dir.name
        if status == "active":
            for tier_dir in sorted(
                status_dir.iterdir(), key=lambda path: path.name.casefold()
            ):
                if not tier_dir.is_dir() or tier_dir.name.startswith((".", "_")):
                    continue
                if (tier_dir / ".git").exists():
                    for project in (tier_dir,):
                        if (
                            _is_canonical(project, canonical)
                            or not project.is_dir()
                            or project.name.startswith((".", "_"))
                            or project.name.startswith("_")
                            or is_public(project, public)
                        ):
                            continue
                        if (project / ".git").exists():
                            records[project.name] = (status, "normal", project)
                    continue
                tier = _infer_tier(tier_dir.name)
                git_children = any(
                    project.is_dir() and (project / ".git").exists()
                    for project in tier_dir.iterdir()
                )
                if git_children:
                    for project in tier_dir.iterdir():
                        if (
                            _is_canonical(project, canonical)
                            or not project.is_dir()
                            or project.name.startswith((".", "_"))
                            or is_public(project, public)
                        ):
                            continue
                        if (project / ".git").exists():
                            records[project.name] = (status, tier, project)
                elif (
                    not _is_canonical(tier_dir, canonical)
                    and tier_dir.name not in public
                    and _is_vault_candidate(tier_dir)
                ):
                    # No .git anywhere under this entry and it carries a
                    # vault marker: F-36 registers it as unmanaged instead of
                    # silently dropping it as an empty legacy tier directory.
                    records[tier_dir.name] = ("unmanaged", "-", tier_dir)
            continue
        if status not in {"new", "inactive", "archived", "external"}:
            continue
        for project in status_dir.iterdir():
            if not project.is_dir() or project.name.startswith((".", "_")) or is_public(project, public):
                continue
            if (project / ".git").exists():
                records[project.name] = (
                    status,
                    "low" if status in {"archived", "external"} else "normal",
                    project,
                )
    return records


def checkout_origin(checkout: pathlib.Path) -> str | None:
    """Return an owner/repository slug for a safe GitHub origin URL."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # One normalization for every reader: the same parser public-targets.py
    # applies to the origin it reads from the checkout's config file.
    return origin_slug(result.stdout.strip())


def source_projects() -> dict[str, pathlib.Path]:
    projects = root() / "projects-root"
    if not projects.is_dir():
        return {}
    return {
        path.name: path
        for path in projects.iterdir()
        if path.is_dir() and not path.name.startswith((".", "_"))
    }


HELD_PROJECTS_VARIABLE = "LLM_ROOT_HELD_PROJECTS"

Records = dict[str, tuple[str, str, pathlib.Path]]


def held_projects(records: Records) -> set[str] | None:
    """The projects this machine declares it holds, or None for all of them.

    control-plane.md is tracked and shared, but its project rows and columns are derived from checkouts on disk, and a container holds fewer checkouts than a developer machine. Unset, empty, or `all`: this machine holds every project, so a project with neither a checkout nor a projects-root/ source is removed. `present`: this machine holds exactly the projects in `records` (its checkouts and unmanaged vault directories). Anything else: a comma separated list of project names, matched exactly. The keywords ignore case. A project outside the held set keeps its committed row and column cells, because this machine cannot tell whether another machine still holds it; its removal lands on a machine that holds it.
    """
    value = os.environ.get(HELD_PROJECTS_VARIABLE, "").strip()
    if value.casefold() in ("", "all"):
        return None
    if value.casefold() == "present":
        return set(records)
    return {name.strip() for name in value.split(",") if name.strip()}


def project_manifest(
    cp: ControlPlane,
    sources: dict[str, pathlib.Path],
    held: set[str] | None = None,
    records: Records | None = None,
) -> dict[str, dict[str, str]]:
    """Return the manifest from live checkouts and project-local sources.

    With a held set (see held_projects), a checkout outside it is ignored, a committed row outside it is carried forward unchanged, and a committed row inside it keeps its status and tier: only the machine holding every project has the directory layout that records them (a container clones flat under active/).
    """
    if records is None:
        records = checkout_records()
    checkouts = {
        name: record
        for name, record in records.items()
        if held is None or name in held
    }
    names = set(checkouts) | set(sources)
    if held is not None:
        names |= set(cp.manifest) - held
    # A public target never has a row, whatever a committed file or a scoped
    # machine's held set would otherwise carry forward.
    names -= public_targets()
    manifest: dict[str, dict[str, str]] = {}
    for name in sorted(names, key=str.casefold):
        previous = cp.manifest.get(name, {})
        if held is not None and name not in held and name in cp.manifest:
            # Verbatim, blank cells included: ControlPlane.load reads a `-`
            # cell as empty, so every empty field is written back as `-`.
            manifest[name] = {
                key: previous.get(key) or "-"
                for key in ("status", "tier", "template", "origin")
            }
            continue
        previous_origin = previous.get("origin")
        if name in checkouts:
            status, tier, checkout = checkouts[name]
            origin = (
                checkout_origin(checkout)
                if not previous_origin or previous_origin == "-"
                else previous_origin
            )
            tier = tier or previous.get("tier") or "normal"
            if held is not None and name in cp.manifest:
                status = previous.get("status") or status
                tier = previous.get("tier") or tier
        else:
            status = previous.get("status") or "new"
            tier = previous.get("tier") or "normal"
            # A source without a checkout here still names the same
            # repository; the recorded origin is not lost with the checkout.
            origin = previous_origin
        manifest[name] = {
            "status": status,
            "tier": tier,
            "template": previous.get("template") or "base",
            "origin": origin or "-",
        }
    return manifest


def hook_events() -> dict[str, list[tuple[str, pathlib.Path]]]:
    found: dict[str, list[tuple[str, pathlib.Path]]] = {}
    base = root()
    manifests = (
        base / "hooks" / "hooks.json",
        base / "hooks" / "claude-worktree-hooks.json",
        base / "hooks" / "claude-agent-graph-hooks.json",
    )
    for path in manifests:
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for event, groups in data.items():
            entries = dict(found.get(event, []))
            for group in groups:
                for hook in group.get("hooks", []):
                    command = str(hook.get("command", ""))
                    names = re.findall(r"([A-Za-z0-9][A-Za-z0-9._-]*\.sh)\b", command)
                    if not names:
                        continue
                    file = base / "hooks" / names[-1]
                    if file.is_file() and not file.name.endswith(".test.sh"):
                        entries[f"hook:{file.stem}"] = file
            found[event] = sorted(entries.items(), key=lambda item: item[0].casefold())
    return found


def runtime_options(adapters_dir: pathlib.Path | None = None) -> list[tuple[str, pathlib.Path]]:
    """One row per runtime an adapter declares, linked to the source root's stratarc.toml.

    The adapters ship in the package, so the rows come from the runtime registry, not from a directory of the source root. A leading underscore marks a shared helper (_common, _inventory, _components), never a runtime. *adapters_dir* redirects the registry, for tests.
    """
    config = root() / "stratarc.toml"
    return [(f"runtime:{name}", config) for name in runtime_registry(adapters_dir)]


def option_groups(
    sources: dict[str, pathlib.Path],
) -> list[tuple[str, list[tuple[str, pathlib.Path]]]]:
    """Return ordered option families and their canonical source files."""
    base = root()
    groups: list[tuple[str, list[tuple[str, pathlib.Path]]]] = []
    groups.append(("instruction-file", [("AGENTS.md", base / "AGENTS.md")]))
    rules = [
        (f"rule:{path.stem}", path)
        for path in (base / "rules").glob("*.md")
        if path.name != "README.md"
    ]
    groups.append(("rules", sorted(rules, key=lambda item: item[0].casefold())))
    for event, entries in sorted(
        hook_events().items(), key=lambda item: item[0].casefold()
    ):
        groups.append((f"hooks-{kebab(event)}", entries))
    skills_root = base / "skills"
    skills = (
        [
            (f"skill:{path.name}", path / "SKILL.md")
            for path in skills_root.iterdir()
            if (path / "SKILL.md").is_file()
        ]
        if skills_root.is_dir()
        else []
    )
    groups.append(("skills", sorted(skills, key=lambda item: item[0].casefold())))
    commands = [
        (f"command:{path.stem}", path) for path in (base / "commands").glob("*.md")
    ]
    groups.append(("commands", sorted(commands, key=lambda item: item[0].casefold())))
    agents = [(f"agent:{path.stem}", path) for path in (base / "agents").glob("*.md")]
    groups.append(("agents", sorted(agents, key=lambda item: item[0].casefold())))
    # WI-28: declared external components a column selects. A group appears
    # only once the manifest declares one, so an empty section adds no table.
    for heading, options in component_options():
        if options:
            groups.append((heading, options))
    groups.append(("project-local-configuration", project_options(sources)))
    groups.append(("runtimes", sorted(runtime_options(), key=lambda item: item[0].casefold())))
    return groups


# components.json section -> (control-plane heading, row prefix). The same
# prefixes stratarc.staging (COMPONENT_ROWS) reads a column's selection from.
COMPONENT_GROUPS = (
    ("mcp_servers", "mcp-servers", "mcp:"),
    ("plugins", "plugins", "plugin:"),
)
COMPONENT_PREFIXES = tuple(prefix for _section, _heading, prefix in COMPONENT_GROUPS)


class ManifestUnreadable(RuntimeError):
    """components.json exists and cannot be read; reconciling would lose selections."""


def component_options() -> list[tuple[str, list[tuple[str, pathlib.Path]]]]:
    """One row per wanted MCP server and plugin in components.json.

    Each becomes a row a column selects, off by default. An unwanted entry is not something to select, and a hosted connector is started by the provider's account, so neither gets a row. Services, dependencies, foreign hook groups and third-party skills are machine- or tool-installed and are checked by the sync check rather than selected per project. A missing manifest declares nothing. One that exists and cannot be read raises ManifestUnreadable: reading it as empty would drop every component row and, once repaired, bring each back off, losing the operator's selections.
    """
    path = root() / "components.json"
    if not path.is_file():
        return [(heading, []) for _section, heading, _prefix in COMPONENT_GROUPS]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ManifestUnreadable(f"components.json cannot be read ({type(error).__name__}: {error})") from None
    if not isinstance(document, dict):
        raise ManifestUnreadable("components.json must hold a JSON object")
    for section, _heading, _prefix in COMPONENT_GROUPS:
        if not isinstance(document.get(section) or [], list):
            raise ManifestUnreadable(f"components.json {section} must be a list")
    out: list[tuple[str, list[tuple[str, pathlib.Path]]]] = []
    for section, heading, prefix in COMPONENT_GROUPS:
        names = {
            str(entry["name"])
            for entry in document.get(section) or []
            if isinstance(entry, dict)
            and entry.get("wanted") is True
            and entry.get("hosted") is not True
            and isinstance(entry.get("name"), str)
            and entry["name"]
        }
        out.append((heading, [(f"{prefix}{name}", path) for name in sorted(names, key=str.casefold)]))
    return out


def kebab(value: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"-\1", value).lower()


def first_file(path: pathlib.Path) -> pathlib.Path | None:
    if not path.is_dir():
        return None
    files = sorted(
        item
        for item in path.rglob("*")
        if item.is_file()
        and item.name != ".DS_Store"
        and not GENERATED_PARTS.intersection(item.parts)
        and item.suffix not in GENERATED_SUFFIXES
    )
    return files[0] if files else None


def project_options(sources: dict[str, pathlib.Path]) -> list[tuple[str, pathlib.Path]]:
    options: dict[str, pathlib.Path] = {}
    for project in sources.values():
        agents = project / "AGENTS.md"
        if agents.is_file():
            options.setdefault("project:AGENTS.md", agents)
        for name in CONFIG_DIRECTORIES:
            file = first_file(project / name)
            if file:
                options.setdefault(f"project:{name}", file)
    return sorted(options.items(), key=lambda item: item[0].casefold())


def load_rule_tiers() -> dict[str, list[str]]:
    """Load rules/tiers.json (F-23): tier name to rule filenames.

    Absent, unreadable, or malformed returns an empty mapping. That degrades every rule to "no tier known": the tier cell renders `-` and, per rule_tier_default, a brand new rule cell opts out everywhere instead of guessing a tier for it. The validator's rule-tier check is the gate that fails a rule missing from the manifest.
    """
    path = root() / "rules" / "tiers.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(tier_name): [str(name) for name in names]
        for tier_name, names in data.items()
        if isinstance(names, list)
    }


def rule_tier_lookup(rule_tiers: dict[str, list[str]]) -> dict[str, str]:
    """option id (e.g. "rule:repo-scope") to tier name, from load_rule_tiers's output."""
    lookup: dict[str, str] = {}
    for tier_name in ("global", "common", "project"):
        for filename in rule_tiers.get(tier_name, []):
            lookup[f"rule:{pathlib.Path(filename).stem}"] = tier_name
    return lookup


def rule_tier_default(rule_tier_name: str | None) -> bool:
    """Default opt-in for a brand-new rule cell, by its manifest tier.

    Global and common rules opt in everywhere by default; a project rule defaults to opted out until a project selects it (F-23: "there is no default tier" for a rule the manifest does not name, so an unknown tier also defaults to opted out rather than opted in).
    """
    return rule_tier_name in ("global", "common")


def old_value(
    cp: ControlPlane,
    option: str,
    column: str,
    default: bool,
    rule_tier_name: str | None = None,
    notes: list[str] | None = None,
) -> bool:
    if option in cp._grid and column in cp._grid[option]:
        value = cp.enabled(column, option)
        if rule_tier_name == "global" and not value:
            if notes is not None:
                notes.append(f"re-set cleared global {option} in {column}")
            return True
        return value
    return default


def project_default(
    option: str, cp: ControlPlane, rule_tier_name: str | None = None
) -> bool:
    if option.startswith("rule:"):
        return rule_tier_default(rule_tier_name)
    if option == "AGENTS.md":
        return True
    if option.startswith("hook:"):
        return any(cp.enabled(column, option) for column in cp.projects())
    return False


def values_for(
    option: str,
    columns: list[str],
    cp: ControlPlane,
    sources: dict[str, pathlib.Path],
    rule_tier_name: str | None = None,
    notes: list[str] | None = None,
) -> dict[str, bool]:
    values: dict[str, bool] = {}
    for column in columns:
        if column == "global":
            # A declared component defaults off globally too: too much
            # loading everywhere is the complaint the manifest answers.
            default = (
                rule_tier_default(rule_tier_name)
                if option.startswith("rule:")
                else not option.startswith(("project:",) + COMPONENT_PREFIXES)
            )
            values[column] = old_value(
                cp, option, column, default, rule_tier_name, notes
            )
            continue
        default = project_default(option, cp, rule_tier_name)
        if option.startswith("project:"):
            name = option.removeprefix("project:")
            project = sources.get(column)
            default = bool(
                project
                and (
                    (project / name).exists()
                    if name == "AGENTS.md"
                    else first_file(project / name)
                )
            )
        values[column] = old_value(cp, option, column, default, rule_tier_name, notes)
    return values


def render_table(
    columns: list[str],
    rows: list[tuple[str, pathlib.Path, dict[str, bool], str | None]],
    with_tier: bool = False,
) -> list[str]:
    header = ["option", *(["tier"] if with_tier else []), *columns]
    out = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for option, path, values, rule_tier_name in rows:
        link = f"[{option}]({source_path(path)})"
        cells = [link]
        if with_tier:
            cells.append(rule_tier_name or "-")
        cells.extend("x" if values[column] else "" for column in columns)
        out.append("| " + " | ".join(cells) + " |")
    return out


def render_columns(
    manifest: dict[str, dict[str, str]],
    sources: dict[str, pathlib.Path],
    held: set[str] | None = None,
    committed: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Return control-plane table columns: global, plus every project that should carry an opt-in cell.

    An active project earns a column once it has a real git checkout. An unmanaged project (F-36: a non-git vault) earns one only once a projects-root/ entry for it also exists, which is what turns discovery into enrollment; until then it is a manifest row only and receives no generated snapshot or instruction file. For a project outside this machine's held set, the committed column answers whether a checkout exists, since this machine's disk cannot: `committed` names the project columns of the committed file, and render always passes it with `held`.
    """
    present = active_checkout_paths()

    def has_checkout(name: str) -> bool:
        if held is None or name in held:
            return name in present
        return name in committed

    named = [
        name
        for name, record in manifest.items()
        if (record["status"] == "active" and has_checkout(name))
        or (record["status"] == "unmanaged" and name in sources)
    ]
    return ["global", *sorted(named, key=str.casefold)]


def render(
    cp: ControlPlane, rule_tiers: dict[str, list[str]] | None = None
) -> tuple[str, list[str]]:
    sources = source_projects()
    records = checkout_records()
    held = held_projects(records)
    manifest = project_manifest(cp, sources, held, records)
    columns = render_columns(manifest, sources, held, set(cp.projects()))
    tier_lookup = rule_tier_lookup(rule_tiers or {})
    notes: list[str] = []
    lines = [
        "# control-plane",
        "",
        "Generated inventory and deployment declaration for this source root. Do not add rows by hand. Run `stratarc reconcile` after adding, moving or deleting a source file or project, and the reconciler fills the tables below: one row per project, rule, hook, skill, command, agent and runtime, with one column for `global` and one per active project. An `x` in a cell opts that resource in for that scope; the reconciler keeps existing opt-in cells and adds new rows with their defaults.",
        "",
        "## projects",
        "",
        "| project | status | tier | template | origin |",
        "|---|---|---|---|---|",
    ]
    for name, record in manifest.items():
        lines.append(
            f"| {name} | {record['status']} | {record['tier']} | {record['template']} | {record['origin']} |"
        )
    lines.extend(
        [
            "",
            "## configuration",
            "",
            "Each table has `global` first, then active project columns in alphabetical order. Column A links to the source file. `x` opts in and any other cell opts out.",
        ]
    )
    for heading, options in option_groups(sources):
        lines.extend(["", f"## {heading}", ""])
        with_tier = heading == "rules"
        rows = []
        for option, path in options:
            rule_tier_name = tier_lookup.get(option) if with_tier else None
            values = values_for(option, columns, cp, sources, rule_tier_name, notes)
            rows.append((option, path, values, rule_tier_name))
        lines.extend(render_table(columns, rows, with_tier=with_tier))
    return "\n".join(lines) + "\n", notes


def refresh_snapshots(
    checkouts: dict[str, pathlib.Path], document: str, check: bool
) -> list[str]:
    """Write the per-project snapshot under .docs/, the internal work product folder, and remove one left behind at an earlier location (.claude/, then the project root) by a checkout that has not reconciled since the snapshot moved. The move is content the snapshot itself carries no record of, so every reconciliation call self-heals a stale copy rather than depending on a one-time migration that a project checkout might never run.
    """
    name = snapshot_name()
    header = (
        f"# {name.removesuffix('.md')}\n\n"
        "This generated snapshot is the active shared configuration declaration for this project. "
        "The canonical source is the source root's `control-plane.md`.\n\n"
    )
    body = (header + document).encode("utf-8")
    changes: list[str] = []
    for project, checkout in checkouts.items():
        target = checkout / SNAPSHOT_DIR / name
        for stale in (checkout / ".claude" / name, checkout / name):
            if stale.is_file():
                changes.append(f"{project}: remove {stale}")
                if not check:
                    stale.unlink()
        if target.exists() and target.read_bytes() == body:
            continue
        changes.append(f"{project}: {target}")
        if not check:
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(body)
    return changes


def reconcile(check: bool = False) -> tuple[bool, list[str]]:
    control_plane = control_plane_path()
    cp = ControlPlane.load(control_plane)
    rule_tiers = load_rule_tiers()
    document, render_notes = render(cp, rule_tiers)
    changed = (
        not control_plane.exists()
        or control_plane.read_text(encoding="utf-8") != document
    )
    notes: list[str] = list(render_notes)
    if changed:
        notes.append(str(control_plane))
        if not check:
            control_plane.write_text(document, encoding="utf-8")
    if not is_linked_worktree():
        notes.extend(refresh_snapshots(active_checkout_paths(), document, check))
    return changed or bool(notes), notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stratarc reconcile")
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="the source root to reconcile (default: $STRATARC_SOURCE, then the nearest stratarc.toml, then the current directory)",
    )
    args = parser.parse_args(argv)
    global _root_flag
    previous = _root_flag
    if args.root:
        configure_root(pathlib.Path(args.root))
    try:
        changed, notes = reconcile(check=args.check)
        linked = is_linked_worktree()
    except (ManifestUnreadable, ConfigError) as error:
        print(f"control-plane: refusing; {error}; nothing was written", file=sys.stderr)
        return 2
    finally:
        _root_flag = previous
    if linked:
        print(
            "control-plane: linked worktree; only this worktree's control-plane.md "
            "was covered, project snapshots are left to the primary checkout"
        )
    if args.check:
        for note in notes:
            print(f"control-plane: stale {note}")
        return 1 if changed else 0
    for note in notes:
        print(f"control-plane: updated {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
