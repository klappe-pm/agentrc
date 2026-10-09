"""The recursive write-back test: did what the source says land in what is deployed.

`run(scope)` re-resolves the source root, re-renders every affected runtime into a temporary stage through the same staging and adapters a sync uses, and compares the result with the deployed files. It then walks into each project the scope reaches and repeats the comparison there. Each file is recorded in the change log as `verified` or `drift`, and a report is written under `<state home>/state/`.

Nothing here writes to a deployed file. The comparison asks each adapter for a dry run against the real target (the adapters compare bytes themselves and report what they would change), and renders a second time into a throwaway directory only to learn which files the render owns. A report entry's `before_digest` is the digest of the deployed file and `after_digest` the digest of the same file in the throwaway render; for a file an adapter merges into (a settings file that also holds the operator's keys), that second digest is the render's, not what a sync would leave.

Scopes: `all`; `project:<name>`, one project and no runtimes; `change:<id>`, the runtimes plus the projects that change reached, whose status in the log is then set to `verified` or `drift`.

    from stratarc import verify
    report = verify.run("all")
    raise SystemExit(report.exit_code)   # 0, or 6 when anything drifted
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import hashlib
import importlib
import json
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from stratarc import changelog, paths
from stratarc.messages import DRIFT, OK

REPORT_SCHEMA = 1
VERIFIED = "verified"
DRIFTED = "drift"
SKIPPED = "skipped"

_PATH = re.compile(r"(/[^\s'\"`,;()]+)")


class VerifyError(Exception):
    """The scope cannot be verified (an unknown project, an unknown change, a scope that is not understood)."""


def digest(path: Path) -> str:
    """`sha256:<hex>` of a file, or an empty string when it is absent or unreadable."""
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


@dataclasses.dataclass
class Report:
    id: str
    ts: str
    scope: str
    root: str
    files: list[dict] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {VERIFIED: 0, DRIFTED: 0, SKIPPED: 0}
        for entry in self.files:
            out[entry["status"]] = out.get(entry["status"], 0) + 1
        return out

    @property
    def drifted(self) -> bool:
        return self.counts[DRIFTED] > 0

    @property
    def exit_code(self) -> int:
        return DRIFT if self.drifted else OK

    def to_dict(self) -> dict:
        return {
            "schema": REPORT_SCHEMA,
            "id": self.id,
            "ts": self.ts,
            "scope": self.scope,
            "root": self.root,
            "result": DRIFTED if self.drifted else VERIFIED,
            "exit": self.exit_code,
            "counts": self.counts,
            "files": self.files,
            "notes": self.notes,
        }


# ----- locations -----


def reports_dir() -> Path:
    return changelog.state_home() / "state" / "verify"


def last_report_path() -> Path:
    return changelog.state_home() / "state" / "verify-last.json"


# ----- comparison -----


def _live_runtimes(root: Path) -> dict[str, tuple[str, Path]]:
    """The runtimes a sync would deploy to: enabled, installed, and selected by the control plane."""
    from stratarc import sync
    from stratarc.config import load_config
    from stratarc.control_plane import ControlPlane

    runtimes = sync.load_runtimes(root=root)
    disabled = {name for name, runtime in load_config(root).runtimes.items() if not runtime.enabled}
    live = {name: value for name, value in sync.detected(runtimes).items() if name not in disabled}
    cp = ControlPlane.load(root / "control-plane.md")
    if cp.rows:
        live = {name: value for name, value in live.items() if cp.enabled("global", f"runtime:{name}")}
    return live


def _sync_call(adapter, name: str, stage: Path, target: Path, root: Path, *, dry_run: bool) -> list[str]:
    if name == "claude":
        from stratarc.deploy_guard import selected_environment

        return list(adapter.sync(stage, target, dry_run=dry_run, environment=selected_environment(root)))
    return list(adapter.sync(stage, target, dry_run=dry_run))


def _relative(path: str, target: Path) -> str | None:
    try:
        return Path(path).relative_to(target).as_posix()
    except ValueError:
        return None


def _drifted_paths(actions: list[str], target: Path) -> tuple[set[str], list[str]]:
    """The target-relative files the dry-run actions name, and the actions that name none."""
    named: set[str] = set()
    unplaced: list[str] = []
    for action in actions:
        found = {rel for raw in _PATH.findall(action) if (rel := _relative(raw.rstrip(".:"), target)) is not None}
        if found:
            named |= found
        else:
            unplaced.append(action)
    return named, unplaced


def _owned_files(adapter, name: str, stage: Path, root: Path, notes: list[str]) -> tuple[Path | None, set[str]]:
    """Render into a throwaway directory; return it and the files the render owns."""
    scratch = Path(tempfile.mkdtemp(prefix=f"stratarc-verify-{name}-"))
    try:
        _sync_call(adapter, name, stage, scratch, root, dry_run=False)
    except Exception as error:  # an adapter that cannot render into a scratch target still gets its dry run
        notes.append(f"{name}: the file inventory could not be rendered ({error}); only drift is reported")
        shutil.rmtree(scratch, ignore_errors=True)
        return None, set()
    from stratarc.deploy_guard import stamp_name

    stamp = stamp_name(root)
    owned = {p.relative_to(scratch).as_posix() for p in scratch.rglob("*") if p.is_file() and p.name != stamp}
    return scratch, owned


def _compare_runtime(name: str, mod: str, target: Path, stage: Path, root: Path, notes: list[str]) -> list[dict]:
    try:
        adapter = importlib.import_module(mod)
    except ModuleNotFoundError as error:
        notes.append(f"{name}: adapter missing ({error.name}); not verified")
        return [_entry("runtime", name, "(adapter)", SKIPPED, detail=f"adapter {mod} is missing")]
    actions = _sync_call(adapter, name, stage, target, root, dry_run=True)
    drifted, unplaced = _drifted_paths(actions, target)
    scratch, owned = _owned_files(adapter, name, stage, root, notes)
    entries: list[dict] = []
    try:
        for rel in sorted(owned | drifted):
            deployed = target / rel
            rendered = (scratch / rel) if scratch is not None else None
            after = digest(rendered) if rendered is not None else ""
            before = digest(deployed)
            if rel in drifted or not deployed.is_file():
                detail = "missing from the deployed tree" if not deployed.is_file() else "differs from the render"
                if rel in drifted and rendered is not None and not rendered.is_file():
                    detail = "deployed but not part of the render"
                entries.append(_entry("runtime", name, rel, DRIFTED, before, after, detail, str(target)))
            else:
                entries.append(_entry("runtime", name, rel, VERIFIED, before, after, "", str(target)))
        for action in unplaced:
            entries.append(_entry("runtime", name, "(runtime)", DRIFTED, detail=" ".join(action.split())[:300], target=str(target)))
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return entries


def _entry(area: str, name: str, file: str, status: str, before: str = "", after: str = "", detail: str = "", target: str = "") -> dict:
    return {"area": area, "name": name, "file": file, "status": status, "before_digest": before, "after_digest": after, "detail": detail, "target": target}


def _project_names(root: Path, cp) -> list[str]:
    if cp.rows:
        return list(cp.projects())
    source = root / "projects-root"
    return sorted(p.name for p in source.iterdir() if p.is_dir()) if source.is_dir() else []


def _compare_projects(root: Path, names: list[str], notes: list[str]) -> list[dict]:
    from stratarc import projects
    from stratarc.control_plane import ControlPlane
    from stratarc.staging import cleanup_all

    saved_root, saved_flag = projects.ROOT, projects._ROOT_CONFIGURED
    entries: list[dict] = []
    projects.configure_root(root)
    try:
        cp = ControlPlane.load(root / "control-plane.md")
        known = _project_names(root, cp)
        for name in names or known:
            if name not in known:
                raise VerifyError(f"the project {name} is not in the source root")
            actions = projects.sync_project(name, True, cp if cp.rows else None)
            if not actions:
                entries.append(_entry("project", name, "(delivered files)", VERIFIED))
                continue
            if len(actions) == 1 and actions[0].startswith("skip"):
                notes.append(f"{name}: {actions[0]}")
                entries.append(_entry("project", name, "(checkout)", SKIPPED, detail=actions[0][:300]))
                continue
            for action in actions:
                match = _PATH.findall(action)
                entries.append(_entry("project", name, match[-1] if match else "(project)", DRIFTED, detail=" ".join(action.split())[:300]))
    finally:
        cleanup_all()
        projects.ROOT = saved_root
        projects.configure_root(saved_root)
        projects._ROOT_CONFIGURED = saved_flag
    return entries


# ----- run -----


def _parse_scope(scope: str) -> tuple[str, str]:
    if scope == "all":
        return "all", ""
    kind, _, value = scope.partition(":")
    if kind in ("project", "change") and value:
        return kind, value
    raise VerifyError(f"the scope {scope!r} is not understood; use all, project:<name> or change:<id>")


def run(scope: str = "all", *, root: Path | None = None, record: bool = True, actor_kind: str = "human", actor: str = "") -> Report:
    """Verify the scope and return the report. Writes the report and the log entries, never a deployed file."""
    from stratarc.control_plane import ControlPlane
    from stratarc.staging import build_stage, cleanup_all
    from stratarc.validate import bound_source

    kind, value = _parse_scope(scope)
    root = paths.source_root(root)
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    ts = now.isoformat().replace("+00:00", "Z")
    report = Report(id=f"ver-{now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}", ts=ts, scope=scope, root=str(root))

    change: dict | None = None
    project_names: list[str] = []
    include_runtimes = kind != "project"
    if kind == "project":
        project_names = [value]
    elif kind == "change":
        change = changelog.get(value)
        if change is None:
            raise VerifyError(f"no change {value} in the log")
        project_names = list(change["projects"])

    with bound_source(root):
        if include_runtimes:
            cp = ControlPlane.load(root / "control-plane.md")
            live = _live_runtimes(root)
            if live:
                stage, stage_notes = build_stage(root, cp, "global")
                report.notes.extend(f"control-plane: {n}" for n in stage_notes)
                try:
                    for name, (mod, target) in live.items():
                        report.files.extend(_compare_runtime(name, mod, target, stage, root, report.notes))
                finally:
                    cleanup_all()
            else:
                report.notes.append("no installed runtime is enabled; nothing to compare")
        if kind == "project" or project_names:
            report.files.extend(_compare_projects(root, project_names, report.notes))

    _write_report(report)
    if record:
        _record(report, change, actor_kind, actor)
    return report


def _write_report(report: Report) -> None:
    body = json.dumps(report.to_dict(), indent=2) + "\n"
    changelog.ensure_directory(reports_dir())
    for path in (reports_dir() / f"{report.id}.json", last_report_path()):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(path)


def _record(report: Report, change: dict | None, actor_kind: str, actor: str) -> None:
    command = f"stratarc verify run --scope {report.scope}"
    events = []
    for entry in report.files:
        if entry["status"] == SKIPPED:
            continue
        events.append(
            {
                "ts": report.ts,
                "actor_kind": actor_kind,
                "actor": actor,
                "command": command,
                "file": entry["file"],
                "layer": entry["area"],
                "key": entry["name"],
                "before_digest": entry["before_digest"],
                "after_digest": entry["after_digest"],
                "status": entry["status"],
                "projects": [entry["name"]] if entry["area"] == "project" else [],
                "targets": [{"runtime": entry["name"], "path": entry["target"]}] if entry["area"] == "runtime" else [],
                "source_ref": report.id,
                "cause_id": change["id"] if change else "",
            }
        )
    changelog.record_many(events)
    if change is not None:
        changelog.update_status(change["id"], DRIFTED if report.drifted else VERIFIED)


# ----- reading reports -----


def last() -> dict | None:
    """The most recent report, or None when none was written."""
    return _read(last_report_path())


def show(report_id: str | None = None) -> dict | None:
    """The report with that id (the latest when none is given), or None."""
    if report_id is None:
        return last()
    if not re.fullmatch(r"ver-[0-9]{14}-[0-9a-f]+", report_id):
        raise VerifyError(f"{report_id!r} is not a verify report id")
    return _read(reports_dir() / f"{report_id}.json")


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def format_report(report: dict, *, verbose: bool = False) -> str:
    counts = report.get("counts", {})
    lines = [
        f"{report['id']}  scope {report['scope']}  {report['ts']}",
        f"result: {report['result']}  ({counts.get(VERIFIED, 0)} verified, {counts.get(DRIFTED, 0)} drift, {counts.get(SKIPPED, 0)} skipped)",
    ]
    for entry in report.get("files", []):
        if entry["status"] == VERIFIED and not verbose:
            continue
        suffix = f"  {entry['detail']}" if entry.get("detail") else ""
        lines.append(f"  {entry['status']:8s} {entry['area']}:{entry['name']} {entry['file']}{suffix}")
    lines.extend(f"note: {n}" for n in report.get("notes", []))
    return "\n".join(lines)
