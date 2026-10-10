"""The data behind the terminal interface, with no terminal library in sight.

Everything the screen shows is built here from the same library layer the commands use: `stratarc.layers` for resolved values and where they came from, `stratarc.config_cmd` for the explain text, `stratarc.changelog` for log entries, `stratarc.sync` for the sync preview and `stratarc.verify` for verification. Nothing in this module writes to the source root or to a deployed file; the one write path is `open_in_editor`, which hands a file to the editor, and `begin_edit` and `finish_edit` wrap it so an invalid result is validated and undone.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stratarc import changelog, config_cmd, layers, log_cmd, resources_cmd, verify
from stratarc import home_layout as layout
from stratarc.layers import LayerError, Layers, Resolution

SOURCE = "source"
PROJECT = "project"
AGENT = "agent"
VALUE = "value"

Editor = Callable[[Path], int]


@dataclass(frozen=True)
class Node:
    """One row of the tree. A value node names a key; the other kinds name the scope its values resolve in."""

    id: str
    kind: str
    label: str
    project: str | None = None
    agent: str | None = None
    key: str | None = None
    children: tuple["Node", ...] = ()


@dataclass(frozen=True)
class Row:
    """One resolved value and the step that decided it."""

    key: str
    value: object
    layer: str
    file: str
    line: int | None
    op: str


# ---- the tree --------------------------------------------------------------------------------


def list_projects(root: Path) -> list[str]:
    base = Path(root) / "projects-root"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def list_agents(root: Path, project: str | None = None) -> list[str]:
    """Agent names defined for the source, or for one project when it is given."""
    base = Path(root) / "projects-root" / project / "agents" if project else Path(root) / "agents"
    if not base.is_dir():
        return []
    return sorted({p.stem for p in base.iterdir() if p.is_file() and p.suffix in (".json", ".toml")})


def _load(root: Path, project: str | None, agent: str | None) -> Layers:
    return layers.load(Path(root), project=project, agent=agent)


def scope_rows(root: Path, project: str | None = None, agent: str | None = None) -> list[Row]:
    """Every key that resolves in the scope, in key order. A key that cannot resolve is left out."""
    layered = _load(root, project, agent)
    done, _failed = layered.resolve_all()
    return [_row(res, Path(root)) for res in done.values()]


def _row(res: Resolution, root: Path) -> Row:
    step = res.decided_by
    where = layers.display_path(step.file, root) if step.file else step.label
    return Row(res.key, res.value, step.layer, where, step.line, step.op)


def _value_nodes(prefix: str, rows: list[Row], project: str | None, agent: str | None) -> tuple[Node, ...]:
    return tuple(Node(f"{prefix}/{r.key}", VALUE, r.key, project, agent, r.key) for r in rows)


def build_tree(root: Path) -> Node:
    """The source at the top, its projects below, each project's agents below that.

    The source lists every resolved key. A project or agent lists only the keys its own layers decide, the pruned outline `config explain --tree` prints.
    """
    root = Path(root)
    projects: list[Node] = []
    for name in list_projects(root):
        try:
            project_rows = [r for r in scope_rows(root, name) if r.layer != "base"]
        except LayerError:
            project_rows = []
        agents: list[Node] = []
        for agent in list_agents(root, name):
            try:
                agent_rows = [r for r in scope_rows(root, name, agent) if r.layer == "agent"]
            except LayerError:
                agent_rows = []
            prefix = f"project:{name}/agent:{agent}"
            agents.append(Node(prefix, AGENT, agent, name, agent, None, _value_nodes(prefix, agent_rows, name, agent)))
        prefix = f"project:{name}"
        projects.append(Node(prefix, PROJECT, name, name, None, None, _value_nodes(prefix, project_rows, name, None) + tuple(agents)))
    try:
        base_rows = scope_rows(root)
    except LayerError:
        base_rows = []
    return Node("source", SOURCE, root.name or "source", None, None, None, _value_nodes("source", base_rows, None, None) + tuple(projects))


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


def find(tree: Node, node_id: str) -> Node | None:
    return next((n for n in walk(tree) if n.id == node_id), None)


# ---- the detail pane -------------------------------------------------------------------------


def _dump(value: object) -> str:
    return layers.tilde(json.dumps(value, ensure_ascii=False))


def _where(row: Row) -> str:
    return row.file + (f":{row.line}" if row.line else "")


def scope_label(node: Node) -> str:
    parts = [f"{name}: {value}" for name, value in (("project", node.project), ("agent", node.agent)) if value]
    return ", ".join(parts) or "base"


def detail_text(root: Path, node: Node) -> str:
    """The resolved value and its provenance for a value node; the resolved values of the scope otherwise."""
    root = Path(root)
    if node.kind == VALUE:
        res = _load(root, node.project, node.agent).resolve(node.key or "")
        row = _row(res, root)
        return "\n".join([f"{row.key}   ({scope_label(node)})", f"value:  {_dump(res.value)}", f"from:   {row.layer}  {_where(row)}  ({row.op})"])
    rows = scope_rows(root, node.project, node.agent)
    lines = [f"{node.kind}: {node.label}   ({scope_label(node)})", f"{len(rows)} resolved value(s)"]
    for row in rows:
        lines.append(f"{row.key} = {_dump(row.value)}   {row.layer} {_where(row)}")
    return "\n".join(lines)


def explain_text(root: Path, node: Node) -> str:
    """The text `stratarc config explain` prints for a value; for a scope, its inheritance outline."""
    root = Path(root)
    layered = _load(root, node.project, node.agent)
    scope = {"project": node.project, "agent": node.agent, "account": None, "runtime": None}
    if node.kind == VALUE:
        return layers.tilde(config_cmd._explain_text(layered.resolve(node.key or ""), scope, root))
    if node.project is None:
        return "Select a value to explain it, or a project to see what it changes."
    done, failed = layered.resolve_all()
    changed = {k: r for k, r in done.items() if any(s.layer != "base" for s in r.steps)}
    data = {"unchanged": len(done) - len(changed), "keys": [config_cmd._resolution_data(r, root) for r in changed.values()]}
    return layers.tilde(config_cmd._tree_text(node.project, data, failed, root))


def log_entries(node: Node) -> list[dict]:
    """Change-log entries for the node: a project's, or a key's for a value."""
    filters: dict[str, object] = {}
    if node.project:
        filters["project"] = node.project
    if node.kind == VALUE and node.key:
        filters["key"] = node.key
    return changelog.query(filters, limit=20)


def log_text(node: Node) -> str:
    return log_cmd._table(log_entries(node))


# ---- the actions -----------------------------------------------------------------------------


def sync_preview(root: Path) -> tuple[int, str]:
    """What a sync would change, as `stratarc sync --diff` prints it. Writes nothing."""
    from stratarc import sync

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = sync.main(["--diff", "--root", str(root)])
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 1
    text = (out.getvalue() + err.getvalue()).strip()
    return code or 0, layers.tilde(text) or "sync preview: nothing would change"


def run_verify(root: Path) -> tuple[int, str]:
    """The text `stratarc verify run` prints and the status it exits with."""
    try:
        report = verify.run("all", root=Path(root))
    except verify.VerifyError as error:
        return 2, str(error)
    return report.exit_code, layers.tilde(verify.format_report(report.to_dict()))


def owning_file(root: Path, node: Node) -> Path | None:
    """The file `edit` opens for a node: the file that decided a value, or the scope's own file."""
    root = Path(root)
    if node.kind == VALUE:
        res = _load(root, node.project, node.agent).resolve(node.key or "")
        return res.decided_by.file
    if node.kind == AGENT and node.project:
        for suffix in (".json", ".toml"):
            candidate = root / "projects-root" / node.project / "agents" / f"{node.agent}{suffix}"
            if candidate.is_file():
                return candidate
        return None
    if node.kind == PROJECT and node.project:
        for name in ("stratarc.toml", "permissions.json"):
            candidate = root / "projects-root" / node.project / name
            if candidate.is_file():
                return candidate
        return None
    return root / "stratarc.toml"


def open_in_editor(path: Path) -> int:
    """Open a file in `$VISUAL` or `$EDITOR`, wait for it to close, and return the editor's status."""
    command = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return subprocess.call([*shlex.split(command), str(path)])


# ---- the edit guard --------------------------------------------------------------------------


@dataclass(frozen=True)
class EditOutcome:
    """What became of an edit: kept (valid), or restored to the original because it was invalid."""

    ok: bool
    changed: bool
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditSession:
    """The file as it was before the editor opened, so an invalid edit can be undone."""

    path: Path
    original: bytes | None


def edit_role(root: Path, path: Path) -> str | None:
    """The validation role `resources_cmd` gives a file: `config` or `permissions` for the root's own two files."""
    if Path(path).parent == Path(root):
        return {"stratarc.toml": "config", "permissions.json": "permissions"}.get(Path(path).name)
    return None


def begin_edit(path: Path) -> EditSession:
    """Remember the file and put a copy of it in the home's backups (an identical `safe_write`) before the editor opens."""
    path = Path(path)
    if not path.is_file():
        return EditSession(path, None)
    original = path.read_bytes()
    layout.safe_write(path, original)
    return EditSession(path, original)


def finish_edit(root: Path, session: EditSession) -> EditOutcome:
    """Validate the edited file with the validator `stratarc config edit` uses; on a problem put the original back through `safe_write`.

    The invalid text stays in the home's backups, so nothing the person typed is lost.
    """
    path = session.path
    try:
        edited = path.read_bytes().decode("utf-8") if path.is_file() else None
        problems = [] if edited is None else resources_cmd.problems_in(path, edited, edit_role(root, path))
    except (OSError, UnicodeDecodeError) as error:
        problems = [f"the edited file cannot be read as UTF-8 text: {error}"]
    if problems:
        if session.original is None:
            path.unlink(missing_ok=True)
        else:
            layout.safe_write(path, session.original)
        return EditOutcome(False, False, tuple(problems))
    now = path.read_bytes() if path.is_file() else None
    return EditOutcome(True, now != session.original)
