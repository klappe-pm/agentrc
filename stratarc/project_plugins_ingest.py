"""Ingest Claude Code plugins an operator added by hand to one managed project.

A plugin enabled by hand in a project's .claude/settings.local.json (or
settings.json), or installed at project or local scope for that checkout, is
unknown to components.json. The project render owns only the plugin keys
components.json declares, so such a plugin keeps working until its key is
declared, and from then on it is owned in every project and stripped from any
project whose column does not select it. Declaring it without selecting it for
the project it came from would therefore turn it off there.

This module closes that gap during `stratarc sync`, in two phases around the
control-plane reconcile:

    plan = scan(root)              # every project first, nothing written
    declare(root, plan, today)     # components.json gains one entry per key
    ... reconcile adds the plugin:<name> rows, all off ...
    opt_in(root, plan)             # an x in each originating project's column

A key found in several projects yields one entry and an x in each of those
columns, never in `global`. A key already declared, already delivered to the
project, enabled at user scope, or false is left alone. Public targets and
checkouts git cannot read are skipped. Nothing is written when nothing is
hand-added.

Three properties hold throughout:

- Report only where a write would break a deploy. A stable deploy requires a
  clean tree at the promoted commit, a linked worktree is not the source of
  record, and `--check` writes nothing. In each the caller prints
  `Plan.would_declare()` and never calls `declare`.
- Every checkout is scanned before anything is written. An input that exists
  but cannot be read (a settings file, the install record, a delivered
  manifest, the user settings) withholds every candidate and says so in
  `Plan.notes`, because declaring a key owns it everywhere and would strip it
  from the project whose file could not be read.
- A failure never leaves a half-applied state unreported. `declare` writes
  components.json atomically or not at all and raises ValueError; `opt_in`
  returns one line per cell it could not set and sets the rest.

    python -m stratarc.project_plugins_ingest [--check] [--root PATH]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field

from stratarc import paths
from stratarc.adapters import _components
from stratarc.components import _check_entry, known_runtimes, validate
from stratarc.control_plane import ControlPlane, set_cell
from stratarc.public_targets import PublicTargetsUnreadable, is_public_checkout, load_public_targets

PROJECT_SETTINGS = (".claude/settings.local.json", ".claude/settings.json")
# The tool that installs a marketplace plugin, which is what components.py
# asks an owner to name for a component this engine does not render itself.
OWNER = "claude-code"
COMMON_ORDER = ("name", "runtimes", "owner", "wanted", "description", "marketplace")


@dataclass
class Candidate:
    """One plugin key to declare, with every project it was found in."""

    key: str
    name: str
    marketplace: str | None
    projects: list[str] = field(default_factory=list)
    # True for a key components.json already declares whose row no column
    # selects (a declare that was not followed by its opt-in): only the cell
    # is missing, so nothing is declared again.
    declared: bool = False


@dataclass
class Plan:
    """What scan found: candidates to declare, and notes about keys it left alone."""

    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def would_declare(self) -> list[str]:
        return [
            f"would {'select' if candidate.declared else 'declare'} plugin {candidate.key} for {project}"
            for candidate in self.candidates
            for project in candidate.projects
        ]


def claude_home() -> pathlib.Path:
    return paths.home() / ".claude"


def delivered_manifest(root: pathlib.Path) -> pathlib.PurePosixPath:
    """The checkout-relative record of what the last sync delivered: .claude/<engine name>-delivered.json."""
    return pathlib.PurePosixPath(f".claude/{paths.engine_name(root)}-delivered.json")


def _git(path: pathlib.Path, *args: str) -> tuple[int, str]:
    # A hook inherits GIT_DIR and friends, which would make -C a no-op.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout.strip()


def is_linked_worktree(root: pathlib.Path) -> bool:
    """True when *root* is a linked worktree rather than the primary checkout."""
    code, common = _git(root, "rev-parse", "--git-common-dir")
    if code != 0 or not common:
        return False
    common_dir = pathlib.Path(common)
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    return common_dir.resolve().parent != root.resolve()


def _read_object(path: pathlib.Path) -> dict | None:
    """The JSON object at *path*: {} when the file is absent, None when it exists but is not a readable object."""
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _enabled(document: dict) -> set[str]:
    plugins = document.get("enabledPlugins")
    if not isinstance(plugins, dict):
        return set()
    return {key for key, value in plugins.items() if isinstance(key, str) and value is True}


def _flags(document: dict) -> dict[str, bool]:
    plugins = document.get("enabledPlugins")
    if not isinstance(plugins, dict):
        return {}
    return {key: value for key, value in plugins.items() if isinstance(key, str) and isinstance(value, bool)}


def _declared(components: dict) -> tuple[set[str], set[str], dict[str, str]]:
    """(claude plugin keys, every declared plugin name, selectable key to name) in components.json.

    The third is the claude entries that get a control-plane row: wanted and
    not hosted, the same filter the reconciler applies.
    """
    keys: set[str] = set()
    names: set[str] = set()
    selectable: dict[str, str] = {}
    for entry in components.get("plugins") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        names.add(str(entry["name"]))
        if "claude" in (entry.get("runtimes") or []):
            key = _components.plugin_key(entry)
            keys.add(key)
            if entry.get("wanted") is True and entry.get("hosted") is not True:
                selectable[key] = str(entry["name"])
    return keys, names, selectable


def _delivered(document: dict) -> set[str]:
    value = document.get("project_plugins")
    return {key for key in value if isinstance(key, str)} if isinstance(value, list) else set()


def _installed_for(checkout: pathlib.Path, installed: dict) -> set[str]:
    """Keys installed_plugins.json lists at project or local scope for *checkout*."""
    plugins = installed.get("plugins")
    if not isinstance(plugins, dict):
        return set()
    here = checkout.resolve()
    keys: set[str] = set()
    for key, records in plugins.items():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or record.get("scope") not in ("project", "local"):
                continue
            target = record.get("projectPath")
            if isinstance(target, str) and target and pathlib.Path(target).expanduser().resolve() == here:
                keys.add(str(key))
    return keys


def _is_source_tree(path: pathlib.Path) -> bool:
    """True when *path* is itself an engine source tree, by content."""
    if (path / paths.CONFIG_NAME).is_file():
        return True
    return (path / "control-plane.md").is_file() and (path / "projects-root").is_dir()


def _checkout_for(name: str, cp: ControlPlane, root: pathlib.Path, public: frozenset[str], base: pathlib.Path):
    """The one readable, non-public, non-source-tree checkout of *name*, or None."""
    if name in public:
        return None
    hits = cp.find_checkout(name, base)
    if len(hits) != 1:
        return None
    checkout = hits[0]
    if checkout.resolve() == root.resolve() or _is_source_tree(checkout):
        return None
    if is_public_checkout(checkout, public):
        return None
    if _git(checkout, "rev-parse", "--git-dir")[0] != 0:
        return None
    return checkout


def _split(key: str) -> tuple[str, str | None]:
    name, separator, marketplace = key.rpartition("@")
    return (name, marketplace) if separator and name else (key, None)


def _entry(candidate: Candidate, today: datetime.date) -> dict:
    where = ", ".join(candidate.projects)
    entry: dict = {
        "name": candidate.name,
        "runtimes": ["claude"],
        "owner": OWNER,
        "wanted": True,
        "description": f"Added by hand to {where}; ingested {today.isoformat()}.",
    }
    if candidate.marketplace:
        entry["marketplace"] = candidate.marketplace
    return {key: entry[key] for key in COMMON_ORDER if key in entry}


def scan(
    root: pathlib.Path,
    *,
    projects: pathlib.Path | None = None,
    claude: pathlib.Path | None = None,
    today: datetime.date | None = None,
) -> Plan:
    """Every hand-added plugin across the active projects, writing nothing.

    All projects are read before any candidate is settled, so a key present in
    several yields one candidate that names each of them. When any input that
    decides ownership cannot be read, no candidate is returned and a note names
    the input: a partial scan must not declare a key.
    """
    plan = Plan()
    base = projects or paths.projects_dir(root)
    claude = claude or claude_home()
    today = today or datetime.date.today()
    manifest = root / "components.json"
    control_plane = root / "control-plane.md"
    if not manifest.is_file() or not control_plane.is_file():
        return plan
    components = _read_object(manifest)
    if components is None:
        plan.notes.append("plugin ingest withheld: components.json is not a readable JSON object")
        return plan
    try:
        public = load_public_targets(root)
    except PublicTargetsUnreadable as error:
        plan.notes.append(f"plugin ingest withheld: public targets are unreadable ({error})")
        return plan
    unreadable: list[str] = []

    def read(path: pathlib.Path, label: str) -> dict:
        document = _read_object(path)
        if document is None:
            unreadable.append(label)
            return {}
        return document

    declared_keys, declared_names, selectable = _declared(components)
    user_enabled = _enabled(read(claude / "settings.json", "user settings.json"))
    installed = read(claude / "plugins" / "installed_plugins.json", "installed_plugins.json")
    cp = ControlPlane.load(control_plane)
    delivered_path = delivered_manifest(root)

    found: dict[str, list[str]] = {}
    recovered: dict[str, list[str]] = {}
    for project in cp.active_projects():
        checkout = _checkout_for(project, cp, root, public, base)
        if checkout is None:
            continue
        # settings.local.json overrides settings.json, as Claude Code reads them.
        local = _flags(read(checkout / PROJECT_SETTINGS[0], f"{project} {PROJECT_SETTINGS[0]}"))
        shared = _flags(read(checkout / PROJECT_SETTINGS[1], f"{project} {PROJECT_SETTINGS[1]}"))
        delivered = _delivered(read(checkout / delivered_path, f"{project} {delivered_path}"))
        effective = {**shared, **local}
        # An install record is only a presence; a project that sets the
        # plugin false in either file has turned it off on purpose.
        switched_off = {key for key, value in (*shared.items(), *local.items()) if value is False}
        keys = {key for key, value in effective.items() if value} | (_installed_for(checkout, installed) - switched_off)
        for key in sorted(keys - delivered):
            if key in declared_keys:
                # Declared already: only a declared plugin no column selects,
                # which a declare not followed by its opt-in leaves behind,
                # is still ours to select for the project it is enabled in.
                row = f"plugin:{selectable.get(key)}"
                if key not in selectable or any(cp.enabled(column, row) for column in cp.columns):
                    continue
                if key in user_enabled:
                    plan.notes.append(f"plugin {key} in {project} is enabled at user scope; not selected")
                    continue
                recovered.setdefault(key, [])
                if project not in recovered[key]:
                    recovered[key].append(project)
                continue
            if key in user_enabled:
                plan.notes.append(f"plugin {key} in {project} is enabled at user scope; not ingested")
                continue
            found.setdefault(key, [])
            if project not in found[key]:
                found[key].append(project)

    if unreadable:
        plan.notes.append(
            "plugin ingest withheld: cannot read " + ", ".join(sorted(set(unreadable))) + "; fix or remove the file and run sync again"
        )
        return plan

    by_name: dict[str, list[str]] = {}
    for key in found:
        by_name.setdefault(_split(key)[0], []).append(key)
    runtimes = known_runtimes()
    for key in sorted(found):
        name, marketplace = _split(key)
        where = ", ".join(found[key])
        if len(by_name[name]) > 1:
            plan.notes.append(
                f"plugin {key} ({where}) shares the name {name} with {', '.join(sorted(set(by_name[name]) - {key}))}; "
                "rows are keyed by bare name, so it is not ingested"
            )
            continue
        if name in declared_names:
            plan.notes.append(
                f"plugin {key} ({where}) shares the name {name} with a declared plugin of another marketplace or runtime; "
                "rows are keyed by bare name, so it is not ingested"
            )
            continue
        candidate = Candidate(key, name, marketplace, found[key])
        problems = _check_entry("plugins", _entry(candidate, today), set(), runtimes)
        if problems:
            plan.notes.append(f"plugin {key} ({where}) cannot be declared: {'; '.join(problems)}")
            continue
        plan.candidates.append(candidate)
    for key in sorted(recovered):
        name, marketplace = _split(key)
        plan.candidates.append(Candidate(key, name, marketplace, recovered[key], declared=True))
    return plan


def _plugins_span(text: str) -> tuple[int, int, str]:
    """(index of `[`, index of `]`, the key's indent) of the top-level plugins list."""
    version = re.search(r'^( *)"version"', text, re.M)
    indent = version.group(1) if version else "  "
    match = re.search(rf'^{indent}"plugins"\s*:\s*', text, re.M)
    if not match or text[match.end()] != "[":
        raise ValueError("components.json has no top-level plugins list")
    start = match.end()
    _, end = json.JSONDecoder().raw_decode(text, start)
    return start, end - 1, indent


def _render_entry(entry: dict, indent: str, multiline: bool) -> str:
    item = indent * 2
    if not multiline:
        return item + json.dumps(entry)
    inner = item + indent
    body = ",\n".join(f"{inner}{json.dumps(key)}: {json.dumps(value)}" for key, value in entry.items())
    return f"{item}{{\n{body}\n{item}}}"


def _add_plugins_list(text: str, entries: list[dict]) -> str:
    """text with a new top-level plugins list holding *entries*, placed last."""
    version = re.search(r'^( *)"version"', text, re.M)
    indent = version.group(1) if version else "  "
    rendered = ",\n".join(_render_entry(entry, indent, True) for entry in entries)
    close = text.rstrip().rfind("}")
    head = text[:close].rstrip()
    separator = "" if head.endswith("{") else ","
    return f'{head}{separator}\n{indent}"plugins": [\n{rendered}\n{indent}]\n{text[close:]}'


def _append_entries(text: str, entries: list[dict]) -> str:
    """text with *entries* appended to the plugins list, every other byte kept."""
    try:
        start, end, indent = _plugins_span(text)
    except ValueError:
        document = json.loads(text)
        if isinstance(document, dict) and "plugins" not in document:
            return _add_plugins_list(text, entries)
        raise
    existing = json.loads(text[start : end + 1])
    if existing:
        last = text[start:end].rstrip()
        tail = last[last.rfind("\n") + 1 :] if "\n" in last else last
        multiline = tail.strip() == "}"
        rendered = ",\n".join(_render_entry(entry, indent, multiline) for entry in entries)
        cut = start + len(last)
        return text[:cut] + ",\n" + rendered + text[cut:]
    rendered = ",\n".join(_render_entry(entry, indent, True) for entry in entries)
    return text[:start] + "[\n" + rendered + "\n" + indent + "]" + text[end + 1 :]


def _write_atomically(path: pathlib.Path, text: str) -> None:
    """Replace *path* with *text* or leave it untouched, keeping its permission bits."""
    temporary = path.with_name(path.name + ".ingest-tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def declare(root: pathlib.Path, plan: Plan, today: datetime.date | None = None) -> list[str]:
    """Write one components.json entry per candidate; one announcement line per project.

    The result is validated before it is written: an append that would add
    any error leaves the file as it was and raises ValueError, and so does a
    file that cannot be read or replaced.
    """
    fresh = [candidate for candidate in plan.candidates if not candidate.declared]
    lines: list[str] = []
    if fresh:
        today = today or datetime.date.today()
        path = root / "components.json"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"components.json cannot be read; nothing written: {error}") from error
        entries = [_entry(candidate, today) for candidate in fresh]
        try:
            updated = _append_entries(text, entries)
            before = json.loads(text)
            after = json.loads(updated)
        except ValueError as error:
            raise ValueError(f"components.json cannot take the append; nothing written: {error}") from error
        if after.get("plugins") != (before.get("plugins") or []) + entries or {
            key: value for key, value in after.items() if key != "plugins"
        } != {key: value for key, value in before.items() if key != "plugins"}:
            raise ValueError("components.json append changed more than the plugins list; nothing written")
        introduced = [problem for problem in validate(after) if problem not in set(validate(before))]
        if introduced:
            raise ValueError("components.json would be invalid after ingestion; nothing written: " + "; ".join(introduced))
        try:
            _write_atomically(path, updated)
        except OSError as error:
            raise ValueError(f"components.json could not be written; nothing written: {error}") from error
        lines += [
            f"plugins: declared {candidate.key} for {project}; commit components.json and control-plane.md"
            for candidate in fresh
            for project in candidate.projects
        ]
    # A key declared by an earlier run whose cell was never set: only the cell is written.
    lines += [
        f"plugins: selected {candidate.key} for {project}; commit control-plane.md"
        for candidate in plan.candidates
        if candidate.declared
        for project in candidate.projects
    ]
    return lines


def opt_in(root: pathlib.Path, plan: Plan) -> list[str]:
    """An x in each originating project's column of the plugin:<name> row.

    Run after the reconcile has added the rows. A row, column or file that is
    missing or unreadable is named, never guessed at, and leaves the other
    cells set.
    """
    notes: list[str] = []
    path = root / "control-plane.md"
    for candidate in plan.candidates:
        for project in candidate.projects:
            try:
                set_cell(path, f"plugin:{candidate.name}", project, on=True)
            except KeyError as error:
                notes.append(f"plugins: {candidate.key} not selected for {project}: {error.args[0]}")
            except (OSError, UnicodeDecodeError) as error:
                notes.append(f"plugins: {candidate.key} not selected for {project}: {error}")
    return notes


STABLE_NOTE = (
    "plugins: note: this checkout deploys from stable, where a source edit would block every deploy; "
    "declare these plugins on main"
)


def deploys_from_stable(root: pathlib.Path) -> bool:
    """True when the environment root deploys from names the stable ref."""
    from stratarc.deploy_guard import UnknownEnvironment, deploy_ref

    try:
        return deploy_ref(root) == "stable"
    except UnknownEnvironment:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stratarc-plugins-ingest", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report what would be declared; write nothing; exit 1 when anything would be")
    parser.add_argument("--root", metavar="PATH", help=f"the source root (default: ${paths.SOURCE_VARIABLE}, then the nearest {paths.CONFIG_NAME})")
    args = parser.parse_args(argv)
    root = paths.source_root(pathlib.Path(args.root) if args.root else None)
    plan = scan(root)
    for note in plan.notes:
        print(f"plugins: note: {note}", file=sys.stderr)
    if not plan.candidates:
        return 0
    stable = deploys_from_stable(root)
    if args.check or stable or is_linked_worktree(root):
        for line in plan.would_declare():
            print(line)
        if stable and not args.check:
            print(STABLE_NOTE, file=sys.stderr)
        return 1 if args.check else 0
    # Same gate and same child environment as `stratarc sync`: an invalid
    # tree is refused before any write, and the reconciler stages from root.
    from stratarc import sync as sync_engine

    refusal = sync_engine.refuse_on_invalid_source(root)
    if refusal is not None:
        return refusal
    try:
        for line in declare(root, plan):
            print(line)
    except ValueError as error:
        print(f"plugins: {error}", file=sys.stderr)
        return 2
    result = subprocess.run(
        [sys.executable, "-m", "stratarc.reconcile", "--root", str(root)],
        capture_output=True,
        text=True,
        env=sync_engine.child_environment(root),
    )
    if result.returncode:
        sys.stderr.write(result.stderr)
        return result.returncode
    failures = opt_in(root, plan)
    for note in failures:
        print(note, file=sys.stderr)
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
