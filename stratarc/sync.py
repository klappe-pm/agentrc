"""Sync the shared configuration to every installed runtime.

The source root is the source of truth for everything shared across agent runtimes: AGENTS.md, rules/, hooks/, skills/, commands/, agents/. This module regenerates derived files inside the source root, then hands the tree to one adapter per runtime, which translates it into that runtime's own format and location.

Usage:
  python -m stratarc.sync                 sync every detected runtime
  python -m stratarc.sync --check         report what is stale, write nothing, exit 1 if any
  python -m stratarc.sync --diff          stage and print what would change, write nothing, exit 0
  python -m stratarc.sync --only codex    one runtime
  python -m stratarc.sync --list          show detected runtimes and their targets
  python -m stratarc.sync --prune         delete what the shared configuration no longer owns
  python -m stratarc.sync --root PATH     stage from PATH instead of the resolved source root

`--dry-run` alone is `--diff`; with `--prune` it lists what a prune would delete and deletes nothing.

The source root is `--root` when given, else `STRATARC_SOURCE` from the environment, else the nearest directory at or above the current one that holds `stratarc.toml`, else the current directory. Every step this run starts, in process or in a child, sees the resolved root as `STRATARC_SOURCE`.

Runtimes are detected by the presence of their config dir. A runtime that is not installed is skipped silently, and a runtime whose `[runtimes.<name>]` table in `stratarc.toml` sets `enabled = false` is skipped too. A `target` in that table moves the directory the runtime is read from and written to. Add a runtime by adding an adapter module under `stratarc/adapters/` whose RUNTIME constant names it, plus its directory in permissions.json runtimeDirectories; the runtime registry in `stratarc/adapters/_common.py` lists it here and in the component renderer.

The branch guard applies only when `components.json` declares environments (see `stratarc.deploy_guard`): a checkout on a branch other than the environment's ref refuses to deploy unless `--allow-branch` names it.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import functools
import importlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys

from stratarc import validate
from stratarc.adapters._common import runtime_registry
from stratarc.paths import SOURCE_VARIABLE, home, source_root
from stratarc.validate import bound_source


def child_environment(root: pathlib.Path) -> dict[str, str]:
    """The environment a child process runs under: this process's, with the
    resolved source root named in STRATARC_SOURCE so a child that reads it
    stages from the same tree this run does, and the directory this engine was
    imported from first on PYTHONPATH so `python -m stratarc.<module>` runs the
    engine that is running, never another one."""
    engine = str(pathlib.Path(__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    return {
        **os.environ,
        SOURCE_VARIABLE: str(root),
        "PYTHONPATH": engine + (os.pathsep + existing if existing else ""),
    }


@functools.lru_cache(maxsize=1)
def _packaged_registry():
    return runtime_registry()


def _registry(adapters_dir: pathlib.Path | None = None):
    return runtime_registry(adapters_dir) if adapters_dir is not None else _packaged_registry()


def load_runtimes(
    adapters_dir: pathlib.Path | None = None,
    home_dir: pathlib.Path | None = None,
    root: pathlib.Path | None = None,
) -> dict[str, tuple[str, pathlib.Path]]:
    """name -> (adapter module, target dir), from the adapters' runtime registry.

    The target is the registry's directory under the home, unless the source root's `stratarc.toml` names another one for that runtime. The home is read when this is called, never at import.
    """
    from stratarc.config import load_config

    home_dir = home_dir if home_dir is not None else home()
    configured = load_config(root).runtimes if root is not None else {}
    out: dict[str, tuple[str, pathlib.Path]] = {}
    for name, runtime in _registry(adapters_dir).items():
        target = runtime.target(home_dir)
        override = configured.get(name)
        if override is not None and override.target is not None:
            target = override.target
        out[name] = (runtime.module, target)
    return out


def hook_event_maps(adapters_dir: pathlib.Path | None = None) -> dict[str, set[str]]:
    """The canonical events each adapter explicitly handles, from the registry.

    "Handles" includes a deliberate drop: cursor and opencode have no
    prompt-submit or session-lifecycle surface and skip UserPromptSubmit and
    SessionStart inside their adapters rather than misregistering them. Each
    adapter declares its set in its RUNTIME constant.
    """
    return {name: set(runtime.hook_events) for name, runtime in _registry(adapters_dir).items()}


def hook_registries(adapters_dir: pathlib.Path | None = None) -> dict[str, str]:
    """name -> the hook registry file inside that runtime's target root.

    Every registry shares the same inner shape once unwrapped: {event:
    [group, ...]} under a top-level "hooks" key, whether that key sits
    alongside a runtime's other settings (Claude, Gemini) or alone (Codex,
    Cursor). A runtime with no JSON hook registry (OpenCode, a local plugin
    instead) declares None and is absent here on purpose.
    """
    return {name: runtime.hook_registry for name, runtime in _registry(adapters_dir).items() if runtime.hook_registry}


def short(p: pathlib.Path) -> str:
    return str(p).replace(str(home()), "~")


# ---------------------------------------------------------------------------
# The change log, the adapter gate and the post-deploy verification. Recording is
# a no-op while the log is off and never fails a sync.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DeployState:
    """What one run learns as it goes: the change that caused the rest, the adapters already warned about, and the files it may have to put back."""

    verify: bool = False
    rollback: bool = False
    cause_id: str = ""
    deployed: bool = False
    warned: set[str] = dataclasses.field(default_factory=set)
    versions: dict[str, str | None] | None = None
    backups: dict[pathlib.Path, pathlib.Path | None] = dataclasses.field(default_factory=dict)
    modes: dict[pathlib.Path, int] = dataclasses.field(default_factory=dict)


def _logging_on() -> bool:
    try:
        from stratarc import changelog

        return changelog.is_enabled()
    except Exception:
        return False


def _digest(path: pathlib.Path | str | None) -> str:
    """The digest of a file for the log, or an empty string when the log is off or the file is absent."""
    if path is None or not _logging_on():
        return ""
    try:
        from stratarc import verify

        return verify.digest(pathlib.Path(path))
    except Exception:
        return ""


def _record_change(event: dict) -> str:
    """Record one change and return its id; an empty string when the log is off or the record failed. A locked database is handled inside the change log, which keeps the event in the files and says so once."""
    try:
        from stratarc import changelog

        if not changelog.is_enabled():
            return ""
        base = {"actor_kind": "ci" if os.environ.get("CI") else "human", "command": "stratarc sync"}
        return changelog.record({**base, **event}, enabled=True)
    except Exception:
        return ""


def _refused(read_only: bool, layer: str, key: str, runtime: str = "", file: str = "") -> None:
    """Record a refusal of a write as a failed change; a read-only run attempted nothing to refuse."""
    if read_only:
        return
    event: dict = {"layer": layer, "key": key, "file": file, "status": "failed"}
    if runtime:
        event["targets"] = [{"runtime": runtime, "path": ""}]
    _record_change(event)


def _project_names(root: pathlib.Path) -> list[str]:
    source = root / "projects-root"
    return sorted(p.name for p in source.iterdir() if p.is_dir()) if source.is_dir() else []


def _ensure_home() -> int | None:
    """Create the .stratarc layout and store the bundled adapter manifests, once per writing command. Returns an exit status when the home cannot be used."""
    from stratarc import home_layout, registry

    try:
        home_layout.ensure_layout()
        registry.register_bundled()
    except home_layout.ToolError as error:
        print(f"sync: {error.message}", file=sys.stderr)
        return error.exit
    return None


def _adapter_gate(runtime: str, state: DeployState) -> tuple[bool, list[str]]:
    """Whether the adapters for this runtime may deploy, and the messages to show. An unsupported adapter blocks; an outdated one warns once per run."""
    from stratarc import registry
    from stratarc.messages import CliError

    try:
        manifests = [m for m in registry.list_adapters().values() if m["runtime"] == runtime]
    except registry.ToolError as error:
        return False, [f"error {error.code}  {error.message}"]
    if state.versions is None and any(m["supports"] != "*" or m.get("tested") for m in manifests):
        state.versions = registry.detect_versions()
    installed = (state.versions or {}).get(runtime)
    allowed, shown = True, []
    for manifest in manifests:
        status = registry.evaluate(manifest, installed)
        decision = registry.sync_gate(status, state.warned)
        allowed = allowed and decision.allowed
        if decision.message:
            blocked = decision.state == registry.UNSUPPORTED
            message = CliError("msg-1114" if blocked else "msg-1115", param="adapter", detail=f'The adapter "{status.name}": {status.detail}')
            shown.append(f"{'error' if blocked else 'warning'} {message.id}  {message.problem}\n  {message.recovery}")
    return allowed, shown


def _reached_projects(root: pathlib.Path) -> list[str]:
    """The projects a sync of this root delivers to: the control plane's when it lists any, else the folders under projects-root."""
    try:
        from stratarc.control_plane import ControlPlane

        plane = ControlPlane.load(root / "control-plane.md")
        if plane.rows:
            return list(plane.projects())
    except Exception:
        pass
    return _project_names(root)


def _remember(path: pathlib.Path, state: DeployState) -> None:
    """Copy a file into the home backups before the run changes it, or note that the run will create it."""
    from stratarc import home_layout

    if path in state.backups:
        return
    if path.is_file():
        state.modes[path] = path.stat().st_mode & 0o7777
        state.backups[path] = home_layout._backup(path, None)
    else:
        state.backups[path] = None


def _back_up_planned(adapter, name: str, stage: pathlib.Path, target: pathlib.Path, root: pathlib.Path, state: DeployState) -> None:
    """Remember every deployed file the adapter is about to change, and the runtime's deploy stamp."""
    from stratarc import verify
    from stratarc.deploy_guard import stamp_name

    actions = verify._sync_call(adapter, name, stage, target, root, dry_run=True)
    named, _unplaced = verify._drifted_paths(actions, target)
    for rel in sorted(named):
        _remember(target / rel, state)
    _remember(target / stamp_name(root), state)


def _roll_back(state: DeployState) -> bool:
    """Put back every file the run backed up and remove the ones it created. Returns False when a backup is missing."""
    from stratarc.messages import CliError

    complete = True
    for path, backup in state.backups.items():
        try:
            if backup is None:
                path.unlink(missing_ok=True)
                print(f"sync: rollback: removed {short(path)}")
            elif backup.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(backup.read_bytes())
                if path in state.modes:
                    path.chmod(state.modes[path])
                print(f"sync: rollback: restored {short(path)}")
            else:
                complete = False
                error = CliError("msg-1119", param="backup", path=short(path))
                print(f"error {error.id}  {error.problem}\n  {error.recovery}", file=sys.stderr)
        except OSError as error:
            complete = False
            print(f"sync: rollback: {short(path)}: {error.strerror or error}", file=sys.stderr)
    return complete


def _verify_deployment(root: pathlib.Path, state: DeployState) -> int:
    """Run the recursive write-back test for this run's change; roll back from the home backups on drift when asked."""
    from stratarc import verify
    from stratarc.messages import DRIFT, UNAVAILABLE, CliError

    actor_kind = "ci" if os.environ.get("CI") else "human"
    reports = []
    try:
        scope = f"change:{state.cause_id}" if state.cause_id else "all"
        try:
            reports.append(verify.run(scope, root=root, actor_kind=actor_kind))
        except verify.VerifyError:
            reports.append(verify.run("all", root=root, actor_kind=actor_kind))
    except verify.VerifyError as error:
        print(f"sync: verify: {error}", file=sys.stderr)
        return 2
    for report in reports:
        print("sync: verify: " + verify.format_report(report.to_dict()).replace("\n", "\nsync: verify: "))
    drifted = sum(report.counts[verify.DRIFTED] for report in reports)
    if not drifted:
        return 0
    drift = CliError("msg-1117", param="drift", detail=f"{drifted} file(s)")
    print(f"error {drift.id}  {drift.problem}\n  {drift.recovery}", file=sys.stderr)
    if state.rollback and not _roll_back(state):
        return UNAVAILABLE
    return DRIFT


def present_action(action: str, stage: pathlib.Path) -> str:
    """An adapter's action line as the operator reads it: the deployed path, never a staging path.

    A copy line is "<verb> <staged file> -> <deployed file>"; it prints as "<verb> <deployed file>". The home shows as `~`, so the line is the same on every run.
    """
    verb, arrow, deployed = action.partition(" -> ")
    if arrow:
        for prefix in ("would copy ", "copy "):
            if verb.startswith(prefix) and verb[len(prefix):].startswith(str(stage)):
                return prefix + short(pathlib.Path(deployed))
    return action.replace(str(stage) + os.sep, "").replace(str(home()), "~")


def regenerate_derived(root: pathlib.Path | None = None) -> list[str]:
    """Rebuild derived files, returning their status and output.

    Always refreshes AGENTS.md at *root*. Also refreshes CLAUDE.md, CODEX.md
    and GEMINI.md at the same root when one already exists there: these
    are generated files whose master is AGENTS.md, and CLAUDE.md is the exact
    file a Claude Code session opened in the source root loads as its project
    instructions, so it must stay as current as AGENTS.md does. A fresh clone
    starts with none of the three when they are gitignored; existence gates
    which are refreshed, so nothing needs to seed them.

    AGENTS.md is committed and published, so it keeps every
    `${STRATARC_SOURCE}` token (the digest's pointers included). The three
    siblings are what a session in this checkout reads, so after the embed
    each is rendered for *root*: the token becomes this checkout's path, the
    same rendering staging applies to every staged copy.
    """
    from stratarc import gen_rules_digest
    from stratarc.rules_digest import render_source

    root = root if root is not None else source_root()
    targets = [root / "AGENTS.md"] + [
        root / name for name in ("CLAUDE.md", "CODEX.md", "GEMINI.md") if (root / name).is_file()
    ]
    stdout, stderr = io.StringIO(), io.StringIO()
    with bound_source(root), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = _call(
            gen_rules_digest.main,
            ["--embed", *(str(t) for t in targets), "--rules-root", str(root / "rules")],
        )
    detail = stdout.getvalue().strip() or stderr.getvalue().strip() or f"exit {code}"
    out = [f"rules digest: {detail}"]
    if code:
        raise RuntimeError(f"rules digest failed: {detail}")
    for target in targets[1:]:
        text = target.read_text(encoding="utf-8")
        rendered = render_source(text, root)
        if rendered != text:
            target.write_text(rendered, encoding="utf-8")
    return out


def _exit_code(value: object) -> int:
    """An exit status from a main()'s return value or a SystemExit's code."""
    if value is None:
        return 0
    return value if isinstance(value, int) else 1


def _call(function, argv: list[str]) -> int:
    """Run a command's main(argv) in this process and return its exit status, including one raised as SystemExit."""
    try:
        return _exit_code(function(argv))
    except SystemExit as exit_:
        return _exit_code(exit_.code)


def emit_process_output(result: subprocess.CompletedProcess[str]) -> None:
    """Forward a child process's output without hiding its diagnostics."""
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def unmapped_hook_events(
    stage: pathlib.Path, runtime: str, maps: dict[str, set[str]] | None = None
) -> set[str]:
    """Return canonical hook events an adapter does not explicitly support."""
    hooks_path = stage / "hooks" / "hooks.json"
    if not hooks_path.exists():
        return set()

    events = set(json.loads(hooks_path.read_text()))
    return events - (hook_event_maps() if maps is None else maps).get(runtime, set())


def unmanaged_runtime_targets(
    policy: dict, live: dict[str, tuple[str, pathlib.Path]]
) -> dict[str, pathlib.Path]:
    """Return live runtime targets omitted from the canonical writable roots.

    A `~` in runtimeDirectories is expanded under the home (STRATARC_HOME when
    set), not the process's $HOME, so the comparison is against the same home
    the targets were built under.
    """
    from stratarc.permissions import runtime_directories

    allowed = {_under_home(value) for value in runtime_directories(policy)}
    return {name: target for name, (_, target) in live.items() if target not in allowed}


def _under_home(value: str) -> pathlib.Path:
    """*value* with a leading `~` or `$HOME` expanded to the home."""
    if value == "~" or value.startswith("~/"):
        return home() / value[2:]
    if value == "$HOME" or value.startswith("$HOME/"):
        return home() / value[6:]
    return pathlib.Path(value).expanduser()


def detected(
    runtimes: dict[str, tuple[str, pathlib.Path]] | None = None,
) -> dict[str, tuple[str, pathlib.Path]]:
    runtimes = runtimes if runtimes is not None else load_runtimes()
    return {k: v for k, v in runtimes.items() if v[1].is_dir()}


# ---------------------------------------------------------------------------
# The --check reverse pass and --prune: what each runtime holds that the
# shared configuration does not own.
# ---------------------------------------------------------------------------

_REVERSE_PASS_HEADINGS = [
    ("orphan", "orphan"),
    ("retired", "retired"),
    ("excluded", "excluded"),
    ("foreign_hook_group", "foreign hook group"),
    ("dangling", "dangling registration"),
    ("mislocated", "mislocated registration"),
    # The external inventory's states. declared is counted, never printed.
    ("returned", "returned"),
    ("literal_secret", "literal secret"),
    ("undeclared", "undeclared"),
    ("unreadable", "unreadable"),
    ("missing", "missing dependency"),
]

# Kinds that make --check exit non-zero. returned and literal_secret fail
# from the day they land; undeclared stays informational.
_FAILING_KINDS = ("orphan", "retired", "dangling", "returned", "literal_secret")

# A runtime that keeps a per-hook trust record, which a prune of its hook
# registrations leaves stale.
_HOOK_TRUST_RUNTIMES = ("codex",)

# Printed names for the inventory's kinds.
_KIND_LABELS = {
    "mcp_server": "mcp server",
    "connector": "account connector",
    "plugin": "plugin",
    "marketplace": "marketplace",
    "extension": "extension",
    "service": "service",
    "dependency": "dependency",
}


@dataclasses.dataclass
class Finding:
    """One line the reverse pass reports, under one of its headings.

    path is set for a directory-owned finding (orphan, retired, excluded,
    and a returned skill) and names the file or, for a per-item owner such
    as skills, the directory to delete. registry_path/event/command are set
    for a hook-registration finding (foreign_hook_group, dangling,
    mislocated, and a returned hook group) and name where and which
    registration to remove. A returned finding with neither lives in a file
    this source root does not own (an MCP server, a plugin, a service) and
    is never pruned; its label names the tool to remove it through.
    """

    runtime: str
    kind: str
    label: str
    path: pathlib.Path | None = None
    registry_path: pathlib.Path | None = None
    event: str | None = None
    command: str | None = None


def _load_manifest(root: pathlib.Path) -> tuple[dict, str | None]:
    """components.json at root, or an empty manifest and the reason it could not be read."""
    from stratarc.components import ManifestError, load

    try:
        return load(root / "components.json"), None
    except ManifestError as error:
        return {"version": 1}, str(error)


def _owner_note(entry: dict) -> str:
    """The trailing (installed by <owner>) a returned finding carries, and
    the entry's removal line when it declares one."""
    note = f" (installed by {entry.get('owner') or 'an unnamed tool'})"
    if isinstance(entry.get("remove"), str) and entry["remove"]:
        note += f"; remove with: {entry['remove']}"
    return note


def _literal_secret_keys(item) -> list[str]:
    """Key paths of an item's values the token-shaped detector matches.

    A secret:// reference is the sanctioned form and is never scanned. The
    detector sees key=value, as the component renderer passes it, so a long
    run adjacent to a key or token name is caught; the value itself never
    leaves this function.
    """
    from stratarc.components import _token_shaped

    found = []
    for key_path, value in item.env:
        if not value or value.startswith("secret://"):
            continue
        if _token_shaped(f"{key_path.rsplit('.', 1)[-1]}={value}"):
            found.append(key_path)
    return found


def _inventory_findings(runtime: str, items: list, document: dict) -> list[Finding]:
    """Classify each inventoried item against the manifest, and scan it for literal secrets."""
    from stratarc.adapters._inventory import classify

    out: list[Finding] = []
    for item in items:
        what = _KIND_LABELS.get(item.kind, item.kind)
        location = short(pathlib.Path(item.location))
        state, entry = classify(document, item)
        if state == "unreadable":
            out.append(Finding(runtime, "unreadable", f"{what} {location}: {item.error}"))
            continue
        label = f"{what} {item.name} ({location})"
        if state == "returned":
            out.append(Finding(runtime, "returned", label + _owner_note(entry or {})))
        elif state == "missing":
            install = (entry or {}).get("install")
            out.append(Finding(runtime, "missing", f"{what} {item.name}" + (f"; install with: {install}" if install else "")))
        elif state in ("declared", "undeclared"):
            out.append(Finding(runtime, state, label))
        for key_path in _literal_secret_keys(item):
            out.append(Finding(runtime, "literal_secret", f"{location}: {key_path}"))
    return out


def _retired_rule_names(root: pathlib.Path) -> set[str]:
    """Names in rules/retired.json, applied to runtime rule directories
    exactly as the project delivery applies it to project checkouts."""
    path = root / "rules" / "retired.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    retired = data.get("retired") if isinstance(data, dict) else None
    return set(retired) if isinstance(retired, list) else set()


def _read_registry(path: pathlib.Path) -> dict:
    """The {event: [group, ...]} mapping inside one hook registry file."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, dict) else {}


def _registration_hooks(entry: object) -> list:
    """The hook dicts one registry entry carries.

    A Claude-shaped group holds them under "hooks"; a flat entry, the only
    shape Cursor honors, is a hook itself with its own "command".
    """
    if not isinstance(entry, dict):
        return []
    hooks = entry.get("hooks")
    if isinstance(hooks, list):
        return hooks
    return [entry] if "command" in entry else []


def reverse_pass(
    root: pathlib.Path,
    stage: pathlib.Path,
    live: dict[str, tuple[str, pathlib.Path]],
    *,
    home: pathlib.Path | None = None,
) -> dict[str, list[Finding]]:
    """Classify everything present in every owned directory and hook
    registry of every live runtime, and, given a home, everything external.

    For each detected runtime this compares adapter.owned_outputs(stage,
    target) against what sync() itself would produce (mirroring the
    stage sync() was called with, so a control-plane-disabled item is
    never mistaken for an orphan), and separately classifies every hook
    registration in that runtime's registry file, if it has one.

    Both are joined to components.json at root: a skill directory
    or foreign hook group declared wanted is declared and silent, one
    declared wanted: false is returned, fails the check, names the tool
    that installs it and is pruned by --prune.

    With home set, the external inventory runs too: each
    adapter's external_inventory(target) and the machine rows
    (~/Library/LaunchAgents under home, declared dependencies on PATH),
    classified declared, undeclared, returned or unreadable, with every
    environment and header value scanned for literal secrets. home is
    explicit so a fixture never reads the real machine; the sync passes
    the resolved home.
    """
    from stratarc.adapters._common import classify_registration, owned_dir_entries, source_hook_names
    from stratarc.adapters._inventory import hook_entry, machine_inventory, skill_entry

    retired = _retired_rule_names(root)
    source_names = source_hook_names(stage)
    document, manifest_error = _load_manifest(root)
    registries = hook_registries()
    findings: dict[str, list[Finding]] = {}

    for name, (mod, target) in live.items():
        items: list[Finding] = []
        try:
            adapter = importlib.import_module(mod)
        except ModuleNotFoundError:
            findings[name] = items
            continue

        owner = getattr(adapter, "owned_outputs", None)
        if owner is not None:
            for od in owner(stage, target):
                present = owned_dir_entries(od.path, per_item=od.per_item)
                # The manifest outranks skills/sync-exclude.json: a skill
                # components.json declares is judged by its entry even when
                # the legacy exclusion also names it.
                declared_skills = (
                    {e for e in present - od.names if skill_entry(document, name, e) is not None}
                    if od.kind == "skill"
                    else set()
                )
                for entry in sorted((present & od.exclude) - declared_skills):
                    p = od.path / entry
                    items.append(Finding(name, "excluded", str(p), path=p))
                for entry in sorted(present - od.names - (od.exclude - declared_skills)):
                    p = od.path / entry
                    declared = skill_entry(document, name, entry) if od.kind == "skill" else None
                    if declared is not None:
                        if declared.get("wanted") is True:
                            items.append(Finding(name, "declared", f"skill {p}"))
                        else:
                            items.append(
                                Finding(name, "returned", f"skill {p}{_owner_note(declared)}", path=p)
                            )
                        continue
                    kind = "retired" if od.kind == "rule" and entry in retired else "orphan"
                    items.append(Finding(name, kind, str(p), path=p))

        registry_name = registries.get(name)
        if registry_name is not None:
            registry_path = target / registry_name
            events = _read_registry(registry_path)
            hooks_dir = target / "hooks"
            seen_foreign: set[tuple[str, str]] = set()
            for event, groups in sorted(events.items()):
                if not isinstance(groups, list):
                    continue
                for group in groups:
                    for hook in _registration_hooks(group):
                        if not isinstance(hook, dict):
                            continue
                        command = str(hook.get("command", "") or "")
                        if not command:
                            continue
                        verdict = classify_registration(
                            command, source_names=source_names, hooks_dir=hooks_dir
                        )
                        label = f"{event}: {command}"
                        if verdict in ("mislocated", "dangling"):
                            items.append(
                                Finding(
                                    name,
                                    verdict,
                                    label,
                                    registry_path=registry_path,
                                    event=event,
                                    command=command,
                                )
                            )
                        elif verdict == "foreign":
                            key = (event, command)
                            if key not in seen_foreign:
                                seen_foreign.add(key)
                                declared = hook_entry(document, name, command)
                                if declared is not None and declared.get("wanted") is True:
                                    items.append(Finding(name, "declared", f"foreign hook group {label}"))
                                    continue
                                returned = declared is not None
                                items.append(
                                    Finding(
                                        name,
                                        "returned" if returned else "foreign_hook_group",
                                        (
                                            f"foreign hook group {label}{_owner_note(declared)}"
                                            if returned
                                            else label
                                        ),
                                        registry_path=registry_path,
                                        event=event,
                                        command=command,
                                    )
                                )

        if home is not None:
            inventory = getattr(adapter, "external_inventory", None)
            if inventory is not None:
                items.extend(_inventory_findings(name, inventory(target), document))

        findings[name] = items

    if home is not None:
        machine = _inventory_findings("machine", machine_inventory(home, document), document)
        if machine:
            findings.setdefault("machine", []).extend(machine)
    if manifest_error:
        findings.setdefault("machine", []).append(
            Finding("machine", "unreadable", f"components.json {short(root / 'components.json')}: {manifest_error}")
        )

    return findings


def print_reverse_findings(findings: dict[str, list[Finding]]) -> bool:
    """Print the reverse pass under its headings.

    Returns True when --check should fail: any orphan, retired, dangling
    registration, returned or literal secret line was printed. foreign
    hook group (an undeclared hook group), excluded, mislocated,
    undeclared, unreadable and missing dependency are informational.
    declared items are counted in the per-runtime inventory line and not
    listed one by one.
    """
    bad = False
    for runtime, items in findings.items():
        by_kind: dict[str, list[Finding]] = {}
        for f in items:
            by_kind.setdefault(f.kind, []).append(f)
        counts = {
            "declared": len(by_kind.get("declared", [])),
            "undeclared": len(by_kind.get("undeclared", [])) + len(by_kind.get("foreign_hook_group", [])),
            "returned": len(by_kind.get("returned", [])),
            "unreadable": len(by_kind.get("unreadable", [])),
        }
        if any(counts.values()):
            print(
                f"sync: {runtime}: inventory: {counts['declared']} declared, "
                f"{counts['undeclared']} undeclared, {counts['returned']} returned, "
                f"{counts['unreadable']} unreadable"
            )
        for kind, heading in _REVERSE_PASS_HEADINGS:
            entries = sorted(by_kind.get(kind, []), key=lambda f: f.label)
            for f in entries:
                print(f"sync: {runtime}: {heading}: {f.label}")
                if kind in _FAILING_KINDS:
                    bad = True
    return bad


def _remove_dangling_registrations(
    registry_path: pathlib.Path, findings: list[Finding]
) -> None:
    """Remove exactly the (event, command) hook entries *findings* named.

    Mutates the registry's own parsed dict and rewrites it whole, so
    every other key (a runtime's permissions, autoMode, or the "version"
    Cursor carries) survives untouched. A group left with no hooks is
    dropped; an event left with no groups is dropped. A flat entry (Cursor's
    shape) whose own command is named is dropped the same way.
    """
    if not registry_path.is_file():
        return
    data = json.loads(registry_path.read_text())
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    targets = {(f.event, f.command) for f in findings}
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            hooks_list = group.get("hooks")
            if not isinstance(hooks_list, list):
                if (event, str(group.get("command", "") or "")) not in targets:
                    new_groups.append(group)
                continue
            kept = [
                h
                for h in hooks_list
                if not (
                    isinstance(h, dict)
                    and (event, str(h.get("command", "") or "")) in targets
                )
            ]
            if kept:
                new_groups.append({**group, "hooks": kept})
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    data["hooks"] = hooks
    registry_path.write_text(json.dumps(data, indent=2) + "\n")


def run_prune(root: pathlib.Path, live: dict[str, tuple[str, pathlib.Path]], *, dry_run: bool) -> int:
    """Delete every orphan, retired and dangling-registration finding.

    Reverse-pass only: no forward sync, no project or permission sweep.
    A real run appends the list it is about to delete to
    ~/.agent-hooks/telemetry/prune-<date>.txt before deleting anything,
    so every run that day stays recoverable from the runtime's own history. A dry run
    prints "would delete" lines and writes nothing.
    """
    from stratarc.control_plane import ControlPlane
    from stratarc.staging import build_stage, cleanup_all

    # Without the manifest a declared wanted skill reads as an orphan, so a
    # prune would delete what the operator chose to keep. An unreadable
    # manifest is not permission to delete. A manifest that loads but fails
    # its schema is refused the same way: a wanted value that is not true or
    # false would otherwise read as unwanted. A manifest that does not exist
    # declares nothing, and prune then behaves exactly as the ownership
    # record specifies without one.
    document, manifest_error = _load_manifest(root)
    if manifest_error:
        print(
            f"sync: prune: refusing; components.json cannot be read ({manifest_error})",
            file=sys.stderr,
        )
        return 2
    from stratarc.components import validate as validate_manifest

    problems = validate_manifest(document)
    if problems:
        for problem in problems:
            print(f"sync: prune: refusing; components.json: {problem}", file=sys.stderr)
        return 2

    cp = ControlPlane.load(root / "control-plane.md")
    if cp.rows:
        live = {k: v for k, v in live.items() if cp.enabled("global", f"runtime:{k}")}

    stage, _notes = build_stage(root, cp, "global")
    try:
        findings = reverse_pass(root, stage, live, home=home())
    finally:
        cleanup_all()

    # A returned skill or hook group is pruned like an orphan: a wanted: false
    # group is removed. A returned item in a runtime-native file this source
    # root does not own is named with its owner and left in place.
    to_delete = [
        f
        for items in findings.values()
        for f in items
        if f.kind in ("orphan", "retired", "dangling")
        or (f.kind == "returned" and (f.path is not None or f.registry_path is not None))
    ]
    for f in sorted(
        (
            f
            for items in findings.values()
            for f in items
            if f.kind == "returned" and f.path is None and f.registry_path is None
        ),
        key=lambda f: (f.runtime, f.label),
    ):
        print(f"sync: prune: not pruned {f.runtime}: returned: {f.label}")

    if not to_delete:
        print("sync: prune: nothing to delete")
        return 0

    if dry_run:
        for f in sorted(to_delete, key=lambda f: (f.runtime, f.kind, f.label)):
            print(f"sync: would delete {f.runtime}: {f.kind}: {f.label}")
        return 0

    telemetry_dir = home() / ".agent-hooks" / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    telemetry_path = telemetry_dir / f"prune-{stamp}.txt"
    # Appended, never replaced: a second prune on the same day must not
    # erase the list that makes the first one recoverable. Each run opens
    # with its own timestamped header line.
    run_at = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
    with telemetry_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"# prune run {run_at}\n"
            + "".join(
                f"{f.runtime} {f.kind} {f.label}\n"
                for f in sorted(to_delete, key=lambda f: (f.runtime, f.kind, f.label))
            )
        )
    print(f"sync: prune: appended to {short(telemetry_path)}")

    for f in to_delete:
        if f.kind in ("orphan", "retired", "returned") and f.path is not None:
            if f.path.is_dir() and not f.path.is_symlink():
                shutil.rmtree(f.path)
            elif f.path.exists() or f.path.is_symlink():
                f.path.unlink()
            print(f"sync: {f.runtime}: deleted {f.label}")

    by_registry: dict[pathlib.Path, list[Finding]] = {}
    for f in to_delete:
        if f.kind in ("dangling", "returned") and f.registry_path is not None:
            by_registry.setdefault(f.registry_path, []).append(f)
    for registry_path, group_findings in by_registry.items():
        _remove_dangling_registrations(registry_path, group_findings)
        dangling = sum(1 for f in group_findings if f.kind == "dangling")
        returned = len(group_findings) - dangling
        print(
            f"sync: removed {dangling} dangling and {returned} returned registration(s) "
            f"from {short(registry_path)}"
        )
        runtime = group_findings[0].runtime
        if returned and runtime in _HOOK_TRUST_RUNTIMES:
            print(f"sync: {runtime}: renumber hook trust by hand")

    return 0


# ---------------------------------------------------------------------------
# The steps a full run delegates. Each is its own function so a test replaces
# one step without replacing the module behind it.
# ---------------------------------------------------------------------------


def refuse_on_invalid_source(root: pathlib.Path) -> int | None:
    """Run the generic validation, strict, against root and return its exit code
    when it finds an error, or None when the tree is clean and deployment may
    proceed. A deliberately invalid tree refuses before any target write; a
    valid tree returns None and sync continues.
    """
    from stratarc.config import ConfigError, load_config

    try:
        gate_private = load_config(root).gate_private
    except ConfigError as error:
        print(f"sync: refusing to deploy; {error}", file=sys.stderr)
        return 2
    checks = "all" if gate_private else "generic"
    code = validate.main(["--root", str(root), "--strict", "--checks", checks])
    if code:
        detail = (
            " (private checks in scripts/private/validate_checks.py are gated by [validate] gate_private in stratarc.toml)"
            if gate_private
            else ""
        )
        print(
            f"sync: refusing to deploy; stratarc.validate --strict --checks {checks} found an error{detail}",
            file=sys.stderr,
        )
        return code
    return None


def run_reconcile(root: pathlib.Path, check: bool) -> int:
    """Run the control plane reconciler against root as a child and forward its output."""
    result = subprocess.run(
        [sys.executable, "-m", "stratarc.reconcile", "--root", str(root)] + (["--check"] if check else []),
        capture_output=True,
        text=True,
        env=child_environment(root),
    )
    emit_process_output(result)
    if not check and result.returncode == 0:
        # The reconciler rewrites control-plane.md and the snapshot in each active project.
        after = _digest(root / "control-plane.md")
        names = _project_names(root)
        for name in names or [""]:
            _record_change(
                {
                    "file": "control-plane.md",
                    "layer": "project" if name else "base",
                    "key": "reconcile",
                    "after_digest": after,
                    "status": "written",
                    "projects": [name] if name else [],
                }
            )
    return result.returncode


def run_projects(root: pathlib.Path, check: bool) -> int:
    """Deliver projects-root/<name>/ into each project checkout, in process."""
    from stratarc import projects

    argv = ["--skip-reconcile", "--root", str(root)] + (["--check"] if check else [])
    code = _call(projects.main, argv)
    if not check and code == 0:
        for name in _project_names(root):
            _record_change({"file": f"projects-root/{name}", "layer": "project", "key": "delivery", "status": "written", "projects": [name]})
    return code


def run_permission_sweep(root: pathlib.Path, live, *, dry_run: bool) -> tuple[int, list[str]]:
    """The permission sweep: permissions.json to every project-scope runtime config
    under the projects root, for the runtimes that received the global column. A
    public target is left alone; an unreadable public-target list refuses the sweep,
    the way the project delivery refuses. Returns (status, actions).
    """
    from stratarc import project_permissions

    stale = project_permissions.refuse_if_stale(root)
    if stale is not None:
        print(f"sync: permissions: {stale}", file=sys.stderr)
        return 2, []
    try:
        acts = project_permissions.sweep(root, project_permissions.projects_dir(root), dry_run=dry_run, runtimes=live.keys())
    except RuntimeError as error:
        print(f"sync: permissions: {error}", file=sys.stderr)
        return 2, []
    if acts and not dry_run:
        _record_change({"file": "permissions.json", "layer": "project", "key": "permissions", "after_digest": _digest(root / "permissions.json"), "status": "written"})
    return 0, list(acts)


def plugin_ingest():
    """The project plugin ingest module, or None when this install does not carry it."""
    try:
        return importlib.import_module("stratarc.project_plugins_ingest")
    except ModuleNotFoundError as error:
        if error.name != "stratarc.project_plugins_ingest":
            raise
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="stratarc sync", description="Sync the shared configuration to every installed runtime.")
    ap.add_argument("--check", action="store_true", help="report what is stale, write nothing, exit 1 if any")
    ap.add_argument("--diff", action="store_true", help="print what a sync would change, write nothing, exit 0")
    ap.add_argument("--only", metavar="RUNTIME")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="with --prune, list what would be deleted; alone, the same as --diff")
    ap.add_argument(
        "--allow-branch",
        action="append",
        default=[],
        metavar="BRANCH",
        help="deploy from this branch in addition to the environment's ref; repeatable",
    )
    ap.add_argument("--verify", action="store_true", help="after the write, verify this run's change against what is deployed; exit 6 on drift")
    ap.add_argument("--rollback-on-drift", action="store_true", help="when verification finds drift, restore the runtime files from the home backups (implies --verify)")
    ap.add_argument(
        "--root",
        metavar="PATH",
        help=f"the source root to stage from (default: ${SOURCE_VARIABLE}, then stratarc.toml, then the current directory)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = source_root(pathlib.Path(args.root) if args.root else None)
    with bound_source(root):
        return run(args, root)


def run(args: argparse.Namespace, root: pathlib.Path) -> int:
    rollback = bool(getattr(args, "rollback_on_drift", False))
    state = DeployState(verify=bool(getattr(args, "verify", False)) or rollback, rollback=rollback)
    code = _run(args, root, state)
    if code == 0 and state.verify and state.deployed:
        return _verify_deployment(root, state)
    return code


def _run(args: argparse.Namespace, root: pathlib.Path, state: DeployState) -> int:
    from stratarc.config import ConfigError, load_config

    diff = args.diff or (args.dry_run and not args.prune)
    if diff and args.check:
        print("sync: --check and --diff are mutually exclusive", file=sys.stderr)
        return 2
    if diff and args.prune:
        print("sync: --diff cannot be combined with --prune; use --prune --dry-run", file=sys.stderr)
        return 2
    # A read-only run: --check reports drift and exits 1 on it, --diff prints
    # what would change and exits 0. Neither writes anything.
    read_only = args.check or diff
    if state.verify and (read_only or args.prune or args.list):
        print("sync: --verify and --rollback-on-drift apply to a write run, not --check, --diff, --prune or --list", file=sys.stderr)
        return 2
    if state.verify and args.only:
        print("sync: --verify and --rollback-on-drift check the whole deployment, so they cannot be combined with --only", file=sys.stderr)
        return 2

    try:
        runtimes = load_runtimes(root=root)
        disabled = {
            name for name, runtime in load_config(root).runtimes.items() if not runtime.enabled
        }
    except ConfigError as error:
        print(f"sync: {error}", file=sys.stderr)
        return 2

    ref: str | None = None
    explicit_branch_override = False
    if not args.list:
        # The environment's ref, only when components.json declares
        # environments; a source root that declares none has no branch guard.
        from stratarc.deploy_guard import (
            UnknownEnvironment,
            branch_refusal,
            deploy_ref,
            source_branch,
            unpromoted_refusal,
        )

        try:
            ref = deploy_ref(root)
        except UnknownEnvironment as error:
            print(f"sync: {error}", file=sys.stderr)
            _refused(read_only, "base", "environment", file="components.json")
            return 2
        if ref is not None:
            branch_bad = branch_refusal(root, (ref, *args.allow_branch))
            if branch_bad is not None:
                print(f"sync: {branch_bad}", file=sys.stderr)
                _refused(read_only, "base", "branch-guard", file="components.json")
                return 2
            if ref == "stable":
                branch = source_branch(root)
                explicit_branch_override = branch != ref and branch in args.allow_branch
            if ref == "stable" and not explicit_branch_override and not read_only and not args.prune:
                promoted_bad = unpromoted_refusal(root, ref)
                if promoted_bad is not None:
                    print(f"sync: {promoted_bad}", file=sys.stderr)
                    _refused(read_only, "base", "unpromoted", file="components.json")
                    return 2

    live ={k: v for k, v in detected(runtimes).items() if k not in disabled}
    if args.only:
        if args.only not in runtimes:
            print(
                f"sync: unknown runtime {args.only}; known: {', '.join(runtimes)}",
                file=sys.stderr,
            )
            return 2
        live = {args.only: runtimes[args.only]}

    if args.list:
        for name, (mod, target) in runtimes.items():
            presence = "installed" if target.is_dir() else "absent"
            print(f"{name:10s} {short(target):28s} {presence}")
        return 0

    # The first writing command lays out the .stratarc home and stores the bundled adapter manifests; a read-only run touches nothing.
    if not read_only and not args.dry_run:
        unusable = _ensure_home()
        if unusable is not None:
            return unusable

    if args.prune:
        return run_prune(root, live, dry_run=args.dry_run)

    # An invalid tree is refused before any target write. The reconciler below
    # writes control-plane.md and every active project snapshot, so the
    # refusal has to come first. The gate only reads, so it runs for --check
    # and --diff too.
    refusal = refuse_on_invalid_source(root)
    if refusal is not None:
        _refused(read_only, "base", "validate", file="(source root)")
        return refusal

    # A plugin the operator added by hand to one project is declared in
    # components.json before the reconcile (which then adds its row, off) and
    # selected for that project's column after it, so every project is scanned
    # before anything is written and the plugin keeps working where it was
    # added without reaching another project or the global column. --check,
    # --diff and a linked worktree report and write nothing.
    ingest = plugin_ingest()
    ingest_plan = ingest.scan(root) if ingest is not None else None
    ingest_stale = False
    ingest_writes = False
    if ingest_plan is not None:
        for note in ingest_plan.notes:
            print(f"sync: plugins: note: {note}", file=sys.stderr)
        ingest_stale = bool(args.check and ingest_plan.candidates)
        ingest_writes = bool(ingest_plan.candidates) and not read_only
        # A stable deploy requires a clean tree at the promoted commit, so an
        # ingest write there would block every deploy that follows: it reports,
        # and the operator declares on main.
        ingest_on_stable = ref == "stable" and not explicit_branch_override
        if ingest_plan.candidates and (read_only or ingest_on_stable or ingest.is_linked_worktree(root)):
            ingest_writes = False
            for line in ingest_plan.would_declare():
                print(f"sync: {line}")
            if ingest_on_stable and not read_only:
                print(f"sync: {ingest.STABLE_NOTE}", file=sys.stderr)
        if ingest_writes:
            components_before = _digest(root / "components.json")
            try:
                for line in ingest.declare(root, ingest_plan):
                    print(f"sync: {line}")
            except ValueError as error:
                print(f"sync: plugins: {error}", file=sys.stderr)
                _refused(read_only, "base", "plugins", file="components.json")
                return 2
            _record_change(
                {
                    "command": "stratarc sync (plugin declare)",
                    "file": "components.json",
                    "layer": "base",
                    "key": "plugins",
                    "before_digest": components_before,
                    "after_digest": _digest(root / "components.json"),
                    "status": "written",
                    "projects": sorted({project for candidate in ingest_plan.candidates for project in candidate.projects}),
                }
            )

    reconciled = run_reconcile(root, read_only)
    if reconciled and not (diff and reconciled == 1):
        _refused(read_only, "project", "reconcile", file="control-plane.md")
        return reconciled

    if ingest_writes:
        # Declared with no cell is the one state the project render turns
        # into a removal (the key is owned and no column selects it), so a
        # failure to set a cell stops the run before any project is touched.
        failures = ingest.opt_in(root, ingest_plan)
        for line in failures:
            print(f"sync: {line}", file=sys.stderr)
        if failures:
            print("sync: plugins: refusing to deploy projects; fix the above and run sync again", file=sys.stderr)
            _refused(read_only, "base", "plugins", file="components.json")
            return 2
        _record_change(
            {
                "command": "stratarc sync (plugin opt-in)",
                "file": "components.json",
                "layer": "base",
                "key": "plugins",
                "after_digest": _digest(root / "components.json"),
                "status": "written",
                "projects": sorted({project for candidate in ingest_plan.candidates for project in candidate.projects}),
            }
        )

    if not read_only:
        agents_before = _digest(root / "AGENTS.md")
        try:
            for line in regenerate_derived(root):
                print("sync:", line)
        except RuntimeError as exc:
            print(f"sync: {exc}", file=sys.stderr)
            _refused(read_only, "base", "rules-digest", file="AGENTS.md")
            return 2
        # The rules digest is the base change every runtime and project write that follows propagates, so it is the cause they share and the change a verification of this run walks from.
        state.cause_id = _record_change(
            {
                "command": "stratarc sync (derived files)",
                "file": "AGENTS.md",
                "layer": "base",
                "key": "rules-digest",
                "before_digest": agents_before,
                "after_digest": _digest(root / "AGENTS.md"),
                "status": "written",
                "projects": [] if args.only or not _logging_on() else _reached_projects(root),
            }
        )
        if ref == "stable" and not explicit_branch_override:
            from stratarc.deploy_guard import unpromoted_refusal

            promoted_bad = unpromoted_refusal(root, ref)
            if promoted_bad is not None:
                print(f"sync: {promoted_bad}", file=sys.stderr)
                _refused(read_only, "base", "unpromoted", file="components.json")
                return 2

    from stratarc.control_plane import ControlPlane
    from stratarc.permissions import load as load_permissions
    from stratarc.permissions import policy_version as _policy_version_of
    from stratarc.staging import build_stage, cleanup_all

    cp = ControlPlane.load(root / "control-plane.md")

    # runtime:<name> rows in the global column gate which runtimes receive anything
    if cp.rows:
        live = {k: v for k, v in live.items() if cp.enabled("global", f"runtime:{k}")}

    try:
        canonical_policy = load_permissions(root)
    except ValueError as error:
        print(f"sync: {error}", file=sys.stderr)
        _refused(read_only, "base", "permissions", file="permissions.json")
        return 2
    canonical_version = _policy_version_of(canonical_policy)
    missing_roots = unmanaged_runtime_targets(canonical_policy, live)
    if missing_roots:
        for name, target in missing_roots.items():
            print(
                f"sync: {name}: missing runtimeDirectories entry for {short(target)}",
                file=sys.stderr,
            )
            _refused(read_only, "runtime", "runtime-directories", runtime=name, file="permissions.json")
        return 2

    stage, notes = build_stage(root, cp, "global")
    for n in notes:
        print(f"sync: control-plane: {n}", file=sys.stderr)

    stale_any = False
    unsupported = False
    gated = False
    reverse_findings: dict[str, list[Finding]] | None = None
    try:
        for name, (mod, target) in live.items():
            gaps = unmapped_hook_events(stage, name)
            if gaps:
                print(
                    f"sync: {name}: missing hook mappings for {', '.join(sorted(gaps))}",
                    file=sys.stderr,
                )
                _refused(read_only, "runtime", "hook-mappings", runtime=name)
                unsupported = True
                continue
            try:
                adapter = importlib.import_module(mod)
            except ModuleNotFoundError as e:
                print(
                    f"sync: {name}: adapter missing ({e.name}); skipped",
                    file=sys.stderr,
                )
                _refused(read_only, "runtime", "adapter-missing", runtime=name)
                unsupported = True
                continue
            from stratarc.deploy_guard import refusal as _deploy_refusal, stamp_name, write_stamp

            stale_bad = _deploy_refusal(target, canonical_version, root)
            if stale_bad is not None:
                print(f"sync: {name}: {stale_bad}", file=sys.stderr)
                _refused(read_only, "runtime", "stale-deploy", runtime=name)
                unsupported = True
                continue
            if not read_only:
                allowed, shown = _adapter_gate(name, state)
                for line in shown:
                    print(line, file=sys.stderr)
                if not allowed:
                    _refused(read_only, "runtime", "adapter-unsupported", runtime=name)
                    gated = True
                    continue
            from stratarc.adapters._components import RenderRefused, notes as component_notes

            try:
                if state.rollback and not read_only:
                    _back_up_planned(adapter, name, stage, target, root, state)
                if name == "claude":
                    from stratarc.deploy_guard import selected_environment

                    actions = adapter.sync(stage, target, dry_run=read_only, environment=selected_environment(root))
                else:
                    actions = adapter.sync(stage, target, dry_run=read_only)
            except RenderRefused as refused:
                # A runtime file the component renderer cannot rewrite
                # safely: this runtime fails and stays unstamped, the
                # others still sync.
                print(f"sync: {name}: {refused}", file=sys.stderr)
                _refused(read_only, "runtime", "render-refused", runtime=name)
                unsupported = True
                continue
            # A selected component this runtime's adapter cannot render is
            # named, never silently dropped. Informational: it does not make
            # --check stale.
            for line in component_notes(stage, name):
                print(f"sync: {name}: {line}")
            if not actions:
                print(f"sync: {name}: current")
                if not read_only:
                    write_stamp(target, canonical_version, root)
                continue
            stale_any = True
            head = "would" if read_only else "did"
            if not read_only:
                stamp_before = _digest(target / stamp_name(root))
                stamp = write_stamp(target, canonical_version, root)
                _record_change(
                    {
                        "file": stamp_name(root),
                        "layer": "runtime",
                        "key": name,
                        "before_digest": stamp_before,
                        "after_digest": _digest(stamp),
                        "status": "propagated",
                        "targets": [{"runtime": name, "path": str(target)}],
                        "cause_id": state.cause_id,
                    }
                )
            print(f"sync: {name} ({short(target)}) {head}:")
            for a in actions:
                print(f"  {present_action(a, stage)}")

        # --check's reverse pass, after the forward comparison above.
        # Computed from the same stage the forward pass just used, so a
        # control-plane-disabled item is never mistaken for an orphan.
        if args.check:
            reverse_findings = reverse_pass(root, stage, live, home=home())
    finally:
        cleanup_all()

    if gated:
        return 5
    if unsupported:
        return 2

    reverse_bad = False
    if reverse_findings is not None:
        reverse_bad = print_reverse_findings(reverse_findings)

    if args.check and (stale_any or reverse_bad or ingest_stale):
        return 1

    # project scope: projects-root/<name>/ -> <projects root>/<name>/
    if not args.only:
        delivered = run_projects(root, read_only)
        if delivered and not (diff and delivered == 1):
            _refused(read_only, "project", "delivery", file="projects-root")
            return delivered

    status, acts = run_permission_sweep(root, live, dry_run=read_only)
    if status:
        _refused(read_only, "project", "permissions", file="permissions.json")
        return status
    if not acts:
        print("sync: permissions: current")
    else:
        from stratarc.project_permissions import projects_dir

        print(f"sync: permissions ({short(projects_dir(root))}) {'would' if read_only else 'did'}:")
        for a in acts:
            print(f"  {a}")
        if args.check:
            return 1
    # Only a full deploy that reached here succeeded; --check and --diff wrote
    # nothing and --only left runtimes behind, so none of them is recorded.
    if not read_only and not args.only:
        from stratarc.deploy_guard import selected_environment, write_deploy_record

        if state.rollback:
            from stratarc.deploy_guard import deploy_record_path

            _remember(deploy_record_path(home(), root), state)
        recorded = write_deploy_record(home(), root, selected_environment(root))
        if recorded is not None:
            print(f"sync: deployed commit recorded in {short(recorded)}")
            from stratarc.deploy_guard import source_commit

            _record_change(
                {
                    "file": short(recorded),
                    "layer": "base",
                    "key": "deploy-record",
                    "after_digest": _digest(recorded),
                    "status": "written",
                    "source_ref": (source_commit(root) if _logging_on() else "") or "uncommitted",
                    "cause_id": state.cause_id,
                }
            )
    state.deployed = not read_only
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
