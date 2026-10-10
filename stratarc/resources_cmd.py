"""`stratarc source|project|runtime|agent|account`: list, show and change the resources of a source root.

Usage:
  source init PATH [--name N] [--use]     scaffold a source root, register it, make it active when none is
  source show                             the effective source root and the registered ones
  source use NAME-or-PATH                 make a registered (or existing) source root the active one
  source list                             the registered source roots
  source move NEW_PATH [--name N] --yes   move a registered source root and update sources.toml
  project add|list|show|edit|remove|enable|disable
  runtime list|show|enable|disable|target
  agent list|show|add|edit|remove|explain
  account list|show|add|edit|remove      `account edit NAME --set KEY=VALUE --unset KEY` changes values without an editor

Every verb accepts `--root PATH` (the source root) and `--json`. With `--json` the output is one envelope `{ok, data, error}` where `error` carries `code`, `message`, `param` and `hint`. Every write verb accepts `--dry-run`, which validates and reports the change and writes nothing. A verb that deletes needs `--yes`.

Rules every write follows:

- The new content is validated before anything is saved: JSON and TOML must parse, list modes must be valid, `permissions.json` must satisfy the bundled schema and `stratarc.toml` must load. An invalid result is refused and the file is left as it was.
- The file is replaced through `home_layout.safe_write`: the old version is copied into the home's `backups/` first, and a file that declares a newer schema is never rewritten.
- `edit` opens `$VISUAL` or `$EDITOR` on a temporary copy of the owning file and saves it only when the result validates. The `main` function accepts an `editor` callable for tests.
- The control-plane opt-in cells are changed through `control_plane.set_cell`; the runtime switches in `stratarc.toml` are changed by a line editor that keeps comments and layout and then checks that nothing but the intended value changed.

Active source root. `--root`, then `STRATARC_SOURCE`, then the nearest `stratarc.toml` above the current directory, then the active entry of the home's `sources.toml`, then the current directory.

The error codes below are stable strings; the coordinating command line maps them to catalog ids:
`unknown-project`, `unknown-runtime`, `unknown-agent`, `unknown-account`, `unknown-key`, `project-exists`, `account-exists`, `agent-exists`, `source-exists`, `path-exists`, `invalid-edit`, `invalid-name`, `invalid-value`, `invalid-path`, `invalid-config`, `no-editor`, `editor-failed`, `needs-yes`, `source-not-found`, `source-root-missing`, `not-reconciled`, plus the layer codes (`parse-error`, `list-mode-missing`, `mode-invalid`, `type-mismatch`) and the home codes (`newer-schema`, `home-unwritable`).

Exit codes: 0 ok; 2 invalid input, an unknown name or a missing flag; 3 the home cannot be written; 4 a name or path that already exists; 5 a newer schema, or a control plane that has not been reconciled; 1 an editor that failed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from stratarc import home_layout as layout
from stratarc import layers
from stratarc.config import ConfigError, load_config, validate_name
from stratarc.control_plane import CONTROL_PLANE_NAME, ControlPlane, set_cell
from stratarc.home_layout import ToolError
from stratarc.layers import LayerError
from stratarc.paths import CONFIG_NAME, SOURCE_VARIABLE, source_root
from stratarc.resources import data_dir

OK = 0
FAILURE = 1
INVALID_INPUT = 2
CONFLICT = 4
UNAVAILABLE = 5

TEMPLATE = "templates/source-root"
SOURCES_FILE = "sources.toml"
BUNDLED_RUNTIMES = {"claude": "~/.claude", "codex": "~/.codex", "gemini": "~/.gemini", "cursor": "~/.cursor", "opencode": "~/.config/opencode"}
AGENT_SUFFIXES = (".toml", ".json", ".md")
ACCOUNT_SUFFIXES = (".toml", ".json")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KEY_PART = re.compile(r"^[A-Za-z0-9_-]+$")

Editor = Callable[[Path], None]

_EXITS = {
    "path-exists": CONFLICT,
    "project-exists": CONFLICT,
    "account-exists": CONFLICT,
    "agent-exists": CONFLICT,
    "source-exists": CONFLICT,
    "not-reconciled": UNAVAILABLE,
    "editor-failed": FAILURE,
}


class ResourceError(ToolError):
    """A failure with a stable string code; the exit status follows from the code."""

    def __init__(self, code: str, message: str, *, hint: str | None = None, param: str | None = None) -> None:
        super().__init__(message, hint=hint, param=param, code=code, exit=_EXITS.get(code, INVALID_INPUT))


# ---- shared helpers -------------------------------------------------------------------------


def _tidy(value: Any) -> Any:
    if isinstance(value, str):
        return layers.tilde(value)
    if isinstance(value, (list, tuple)):
        return [_tidy(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _tidy(v) for k, v in value.items()}
    return value


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _where(path: Path, root: Path) -> str:
    return layers.display_path(path, root)


def _layer_error(exc: LayerError, root: Path) -> ResourceError:
    where = layers.display_path(exc.file, root) + (f":{exc.line}" if exc.line else "") + ": " if exc.file else ""
    return ResourceError(exc.code, where + exc.message, hint=exc.hint or None, param=exc.key)


def _nearest_config_root() -> Path | None:
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    return None


def _env_editor(environ: Mapping[str, str]) -> Editor:
    command = (environ.get("VISUAL") or environ.get("EDITOR") or "").strip()
    if not command:
        raise ResourceError("no-editor", "No editor is configured to edit this file.", hint="Set $EDITOR (or $VISUAL), then run the command again.")

    def run(path: Path) -> None:
        try:
            done = subprocess.run([*shlex.split(command), str(path)], check=False)
        except OSError as error:
            raise ResourceError("editor-failed", f"The editor could not start: {error.strerror or error}.", hint="Check $EDITOR.") from None
        if done.returncode != 0:
            raise ResourceError("editor-failed", f"The editor exited with status {done.returncode}; nothing was saved.", hint="Run the command again.")

    return run


# ---- validation -----------------------------------------------------------------------------


def _check_permissions(doc: Any) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        problems = []
        if not isinstance(doc, dict) or doc.get("schemaVersion") != 1:
            problems.append("schemaVersion must be 1")
        for key in ("allow", "deny", "ask"):
            if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
                problems.append(f"{key} must be a list")
        return problems
    schema = json.loads(data_dir("schema/permissions.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or 'top level'}: {e.message}" for e in sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])]


def _check_sources(doc: Mapping[str, Any]) -> list[str]:
    problems = []
    sources = doc.get("sources", {})
    if not isinstance(sources, dict):
        return ["sources must be a table of tables"]
    for name, table in sources.items():
        if not isinstance(table, dict) or not isinstance(table.get("path"), str) or not table["path"]:
            problems.append(f"sources.{name}.path must be a non-empty string")
    active = doc.get("active")
    if active is not None and (not isinstance(active, str) or active not in sources):
        problems.append("active must name a registered source")
    return problems


def problems_in(path: Path, text: str, role: str | None = None) -> list[str]:
    """What would be wrong with `text` as the content of `path`; an empty list means it can be saved.

    `role` is `config` for the source root's `stratarc.toml` (it must load), `permissions` for the root's `permissions.json` (it must satisfy the bundled schema) and `sources` for the home's `sources.toml`. A `permissions.json` that declares `schemaVersion` is checked against the schema wherever it sits; a partial project overlay without it is only parsed.
    """
    if path.suffix == ".md":
        if text.startswith("---") and not re.search(r"^---[ \t]*$", text[3:], re.M):
            return ["the frontmatter is opened with --- but never closed"]
        return []
    if path.suffix not in layers.PARSERS:
        return []
    with tempfile.TemporaryDirectory(prefix="stratarc-validate-") as tmp:
        probe = Path(tmp) / (CONFIG_NAME if role == "config" else path.name)
        probe.write_text(text, encoding="utf-8", newline="")
        try:
            doc, lines = layers.PARSERS[path.suffix](probe)
        except LayerError as exc:
            return [exc.message + (f" (line {exc.line})" if exc.line else "")]
        problems: list[str] = []
        try:
            layers.flatten(doc, lines, probe)
        except LayerError as exc:
            problems.append(exc.message + (f" (line {exc.line})" if exc.line else ""))
        if path.name == "permissions.json" and (role == "permissions" or "schemaVersion" in doc):
            problems += _check_permissions(doc)
        if role == "config":
            try:
                load_config(Path(tmp))
            except ConfigError as exc:
                problems.append(str(exc).split(": ", 1)[-1])
        if role == "sources":
            problems += _check_sources(doc)
    return problems


def _save(path: Path, text: str, *, root: Path, dry: bool, role: str | None = None) -> dict[str, Any]:
    """Validate `text`, then replace `path` with it through `safe_write` unless `dry`."""
    shown = _where(path, root)
    problems = problems_in(path, text, role)
    if problems:
        raise ResourceError("invalid-edit", f"{shown} would be invalid: " + "; ".join(problems) + ".", hint="Fix the listed problems. The file was left unchanged.", param=shown)
    existed = path.is_file()
    changed = (not existed) or _read(path) != text
    result: dict[str, Any] = {"file": shown, "changed": changed, "created": not existed, "backup": None, "dry_run": dry}
    if changed and not dry:
        backup = layout.safe_write(path, text, private=False)
        result["backup"] = str(backup) if backup else None
    return result


def _edit_file(path: Path, *, root: Path, dry: bool, editor: Editor | None, environ: Mapping[str, str], role: str | None = None) -> dict[str, Any]:
    """Run the editor on a temporary copy of `path` and save the result only when it validates."""
    runner = editor or _env_editor(environ)
    original = _read(path) if path.is_file() else ""
    with tempfile.TemporaryDirectory(prefix="stratarc-edit-") as tmp:
        work = Path(tmp) / path.name
        work.write_bytes(original.encode("utf-8"))
        runner(work)
        try:
            edited = _read(work)
        except (OSError, UnicodeDecodeError) as error:
            raise ResourceError("invalid-edit", f"The edited file cannot be read: {error}.", hint="Save it as UTF-8 text. The file was left unchanged.") from None
    return _save(path, edited, root=root, dry=dry, role=role)


def _describe_save(result: Mapping[str, Any]) -> str:
    if not result["changed"]:
        return f"{result['file']}: unchanged"
    verb = "create" if result["created"] else "change"
    if result["dry_run"]:
        return f"{result['file']}: would {verb} (dry run, nothing written)"
    done = "created" if result["created"] else "changed"
    return f"{result['file']}: {done}" + (f" (backup: {layers.tilde(result['backup'])})" if result["backup"] else "")


def _backup_file(path: Path) -> str | None:
    """Copy a file into the home's backups before it is deleted; the same copy `safe_write` makes."""
    made = layout._backup(path, None)
    return str(made) if made else None


# ---- the surgical TOML line editor ----------------------------------------------------------


def _header_pattern(table: tuple[str, ...]) -> re.Pattern[str]:
    parts = [rf"(?:{re.escape(p)}|\"{re.escape(p)}\"|'{re.escape(p)}')" for p in table]
    return re.compile(r"^\s*\[\s*" + r"\s*\.\s*".join(parts) + r"\s*\]\s*(?:#.*)?$")


def _section(lines: list[str], table: tuple[str, ...]) -> tuple[int, int] | None:
    """The line range of one table: `(header index, end)`. The top level (an empty `table`) starts at -1 and ends at the first header."""
    if not table:
        return -1, next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    pattern = _header_pattern(table)
    for index, line in enumerate(lines):
        if pattern.match(line.rstrip("\r\n")):
            end = len(lines)
            for later in range(index + 1, len(lines)):
                if lines[later].lstrip().startswith("["):
                    end = later
                    break
            return index, end
    return None


def _value_length(rest: str) -> int:
    """The length of the value at the start of `rest`, excluding any trailing space and comment."""
    if not rest:
        return 0
    if rest[0] == '"':
        if rest.startswith('"""'):
            return len(rest)
        index = 1
        while index < len(rest):
            if rest[index] == "\\":
                index += 2
            elif rest[index] == '"':
                return index + 1
            else:
                index += 1
        return len(rest)
    if rest[0] == "'":
        close = rest.find("'", 1)
        return close + 1 if close >= 0 else len(rest)
    hash_at = rest.find("#")
    return len((rest if hash_at < 0 else rest[:hash_at]).rstrip())


def set_toml_values(text: str, table: tuple[str, ...], values: Mapping[str, str]) -> str:
    """Set keys in one `[a.b]` table of a TOML text, leaving every other byte as it was.

    `values` maps a key to its TOML literal. An existing key keeps its position, spacing and trailing comment; a missing key is added after the table's last key; a missing table is appended at the end. Only plain `[a.b]` tables are understood: the caller checks the parsed result, so a layout this editor cannot follow is refused rather than guessed at.
    """
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    found = _section(lines, table)
    if found is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.append("[" + ".".join(table) + "]" + newline)
        lines += [f"{key} = {literal}{newline}" for key, literal in values.items()]
        return "".join(lines)
    for key, literal in values.items():
        start, end = _section(lines, table) or found
        key_pattern = re.compile(rf"^(\s*(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')\s*=\s*)(.*)$")
        for index in range(start + 1, end):
            body = lines[index].rstrip("\r\n")
            match = key_pattern.match(body)
            if match:
                rest = match.group(2)
                size = _value_length(rest)
                lines[index] = match.group(1) + literal + rest[size:] + lines[index][len(body) :]
                break
        else:
            last = start
            for index in range(start + 1, end):
                stripped = lines[index].strip()
                if stripped and not stripped.startswith("#"):
                    last = index
            if last < 0:
                # A top level with no keys yet: the new key goes after any leading comments, ahead of the first table.
                position = end
                while position > 0 and not lines[position - 1].strip():
                    position -= 1
                if position > 0 and not lines[position - 1].endswith("\n"):
                    lines[position - 1] += newline
                lines.insert(position, f"{key} = {literal}{newline}")
                if position + 1 < len(lines) and lines[position + 1].strip():
                    lines.insert(position + 1, newline)
                continue
            if not lines[last].endswith("\n"):
                lines[last] += newline
            lines.insert(last + 1, f"{key} = {literal}{newline}")
    return "".join(lines)


def remove_toml_key(text: str, table: tuple[str, ...], key: str) -> str | None:
    """Delete one key line from a `[a.b]` table (or the top level), leaving every other byte as it was; None when the key is not there."""
    lines = text.splitlines(keepends=True)
    found = _section(lines, table)
    if found is None:
        return None
    start, end = found
    key_pattern = re.compile(rf"^\s*(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')\s*=")
    for index in range(start + 1, end):
        if key_pattern.match(lines[index]):
            del lines[index]
            return "".join(lines)
    return None


def _literal(value: bool | str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


# ---- context --------------------------------------------------------------------------------


class Context:
    def __init__(self, args: argparse.Namespace, editor: Editor | None, environ: Mapping[str, str]) -> None:
        self.args = args
        self.editor = editor
        self.environ = environ
        self.dry = bool(getattr(args, "dry_run", False))
        self.yes = bool(getattr(args, "yes", False))
        self._root: Path | None = None

    @property
    def candidate_root(self) -> Path:
        """The source root as resolved, whether or not it exists."""
        if self._root is None:
            if self.args.root:
                self._root = source_root(Path(self.args.root))
            elif (self.environ.get(SOURCE_VARIABLE) or "").strip():
                self._root = source_root()
            else:
                nearest = _nearest_config_root()
                active = _active_path()
                self._root = nearest or active or source_root()
        return self._root

    @property
    def root(self) -> Path:
        root = self.candidate_root
        if not root.is_dir():
            raise ResourceError("source-root-missing", f"The source root {root} does not exist.", hint="Pass an existing directory with --root, or create one with `stratarc source init`.")
        return root

    def need_yes(self, action: str) -> None:
        if not self.yes:
            raise ResourceError("needs-yes", f"{action} deletes files and needs confirmation.", hint="Pass --yes to confirm, or --dry-run to preview.")


# ---- sources.toml ---------------------------------------------------------------------------


def _sources_path() -> Path:
    return layout.layout_root() / SOURCES_FILE


def _read_sources() -> dict[str, Any]:
    data = layout.read_toml(_sources_path())
    tables = data.get("sources", {})
    sources = {name: dict(table) for name, table in tables.items() if isinstance(table, dict) and isinstance(table.get("path"), str)} if isinstance(tables, dict) else {}
    active = data.get("active")
    return {"active": active if isinstance(active, str) and active in sources else None, "sources": sources}


def _active_path() -> Path | None:
    try:
        state = _read_sources()
    except ToolError:
        return None
    if state["active"]:
        path = Path(state["sources"][state["active"]]["path"])
        if path.is_dir():
            return path
    return None


def _save_sources(state: Mapping[str, Any], dry: bool) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    if state["active"]:
        doc["active"] = state["active"]
    doc["sources"] = {name: {"path": entry["path"]} for name, entry in sorted(state["sources"].items())}
    problems = _check_sources(doc)
    if problems:
        raise ResourceError("invalid-edit", "sources.toml would be invalid: " + "; ".join(problems) + ".", hint="The file was left unchanged.")
    result: dict[str, Any] = {"file": f"~/{layout.LAYOUT_NAME}/{SOURCES_FILE}", "dry_run": dry, "backup": None}
    if not dry:
        layout.ensure_layout()
        backup = layout.write_toml(_sources_path(), doc)
        result["backup"] = str(backup) if backup else None
    return result


def _source_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    if not name or not name[0].isalpha():
        name = "source-" + name if name else "source"
    return name


def _unique_name(base: str, taken: Mapping[str, Any]) -> str:
    name, number = base, 2
    while name in taken:
        name = f"{base}-{number}"
        number += 1
    return name


def _walk(node: Any, prefix: str = ""):
    for child in sorted(node.iterdir(), key=lambda n: n.name):
        if child.name == "__pycache__":
            continue
        if child.is_dir():
            yield from _walk(child, f"{prefix}{child.name}/")
        else:
            yield f"{prefix}{child.name}", child


def source_init(ctx: Context) -> tuple[dict, str]:
    args = ctx.args
    target = Path(args.path).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ResourceError("path-exists", f"{layers.tilde(str(target))} already exists and is not empty.", hint="Pass a new or empty directory.", param="path")
    state = _read_sources()
    if args.name:
        if not validate_name(args.name):
            raise ResourceError("invalid-name", f'The source name "{args.name}" is not valid.', hint="Use lowercase letters, digits and hyphens, starting with a letter.", param="name")
        if args.name in state["sources"]:
            raise ResourceError("source-exists", f'A source named "{args.name}" is already registered.', hint="Choose another --name, or use `stratarc source use`.", param="name")
        name = args.name
    else:
        name = _unique_name(_source_name(target.name), state["sources"])
    files = list(_walk(data_dir(TEMPLATE)))
    activate = args.use or not state["active"]
    state["sources"][name] = {"path": str(target)}
    if activate:
        state["active"] = name
    saved = _save_sources(state, ctx.dry)
    if not ctx.dry:
        for rel, node in files:
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(node.read_bytes())
    data = {"name": name, "path": str(target), "files": len(files), "active": activate, "sources_file": saved, "dry_run": ctx.dry}
    verb = "would write" if ctx.dry else "wrote"
    return data, f"source init: {verb} {len(files)} files to {layers.tilde(str(target))} as \"{name}\"" + (" (active)" if activate else "") + ("; dry run, nothing written" if ctx.dry else "")


def source_show(ctx: Context) -> tuple[dict, str]:
    state = _read_sources()
    root = ctx.candidate_root
    registered = next((n for n, e in state["sources"].items() if Path(e["path"]) == root), None)
    projects_dir = root / "projects-root"
    projects = sorted(p.name for p in projects_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))) if projects_dir.is_dir() else []
    data = {"root": str(root), "exists": root.is_dir(), "has_config": (root / CONFIG_NAME).is_file(), "projects": len(projects), "registered_as": registered, "active": state["active"], "registered": len(state["sources"])}
    text = "\n".join(
        [
            f"root: {layers.tilde(str(root))}" + ("" if data["exists"] else " (missing)"),
            f"registered as: {registered or '(not registered)'}",
            f"active: {state['active'] or '(none)'}",
            f"stratarc.toml: {'present' if data['has_config'] else 'absent'}",
            f"projects: {len(projects)}",
        ]
    )
    return data, text


def source_list(ctx: Context) -> tuple[dict, str]:
    state = _read_sources()
    rows = [{"name": n, "path": e["path"], "active": n == state["active"], "exists": Path(e["path"]).is_dir()} for n, e in sorted(state["sources"].items())]
    text = "\n".join(f"{'*' if r['active'] else ' '} {r['name']}  {layers.tilde(r['path'])}" + ("" if r["exists"] else "  (missing)") for r in rows) or "no source roots are registered"
    return {"sources": rows, "active": state["active"]}, text


def source_use(ctx: Context) -> tuple[dict, str]:
    state = _read_sources()
    target = ctx.args.target
    registered = False
    if target in state["sources"]:
        name = target
        path = Path(state["sources"][name]["path"])
        if not path.is_dir():
            raise ResourceError("source-not-found", f'The source "{name}" points at {layers.tilde(str(path))}, which no longer exists.', hint="Move it back, or register a new one with `stratarc source init`.", param="target")
    else:
        path = Path(target).expanduser().resolve()
        if not path.is_dir():
            raise ResourceError("source-not-found", f'"{target}" is neither a registered source nor an existing directory.', hint="Run `stratarc source list` for the registered ones.", param="target")
        name = next((n for n, e in state["sources"].items() if Path(e["path"]) == path), None)
        if name is None:
            name = _unique_name(_source_name(path.name), state["sources"])
            state["sources"][name] = {"path": str(path)}
            registered = True
    changed = state["active"] != name or registered
    state["active"] = name
    saved = _save_sources(state, ctx.dry) if changed else {"file": f"~/{layout.LAYOUT_NAME}/{SOURCES_FILE}", "dry_run": ctx.dry, "backup": None}
    data = {"name": name, "path": str(path), "registered": registered, "changed": changed, "sources_file": saved, "dry_run": ctx.dry}
    if not changed:
        return data, f"source use: \"{name}\" is already active"
    return data, f"source use: {'would make' if ctx.dry else 'made'} \"{name}\" active" + (" and registered it" if registered else "") + ("; dry run, nothing written" if ctx.dry else "")


def source_move(ctx: Context) -> tuple[dict, str]:
    state = _read_sources()
    name = ctx.args.name or state["active"]
    if not name or name not in state["sources"]:
        raise ResourceError("source-not-found", "No registered source root to move." if not ctx.args.name else f'No source named "{name}" is registered.', hint="Run `stratarc source list`, or pass --name.", param="name")
    old = Path(state["sources"][name]["path"])
    if not old.is_dir():
        raise ResourceError("source-not-found", f'The source "{name}" points at {layers.tilde(str(old))}, which no longer exists.', hint="Register the new location with `stratarc source use PATH`.", param="name")
    new = Path(ctx.args.path).expanduser().resolve()
    if new == old or old in new.parents:
        raise ResourceError("invalid-path", "The new location is the source root itself or inside it.", hint="Choose a location outside the source root.", param="path")
    if new.exists() and (not new.is_dir() or any(new.iterdir())):
        raise ResourceError("path-exists", f"{layers.tilde(str(new))} already exists and is not empty.", hint="Choose a new or empty directory.", param="path")
    data = {"name": name, "from": str(old), "to": str(new), "dry_run": ctx.dry}
    if ctx.dry:
        return data, f"source move: would move \"{name}\" from {layers.tilde(str(old))} to {layers.tilde(str(new))}; dry run, nothing moved"
    ctx.need_yes("Moving a source root")
    new.parent.mkdir(parents=True, exist_ok=True)
    if new.is_dir():
        new.rmdir()
    shutil.move(str(old), str(new))
    state["sources"][name]["path"] = str(new)
    data["sources_file"] = _save_sources(state, False)
    return data, f"source move: moved \"{name}\" from {layers.tilde(str(old))} to {layers.tilde(str(new))}"


# ---- projects -------------------------------------------------------------------------------


def _project_names(root: Path) -> list[str]:
    base = root / "projects-root"
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))) if base.is_dir() else []


def _project_dir(root: Path, name: str) -> Path:
    directory = root / "projects-root" / name
    if not _SAFE_NAME.match(name) or not directory.is_dir():
        raise ResourceError("unknown-project", f'The project "{name}" has no folder under projects-root.', hint="Run `stratarc project list` for the projects, or add one with `stratarc project add`.", param="name")
    return directory


def _files_under(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file())


def _set_manifest_status(text: str, name: str, value: str) -> tuple[str, bool] | None:
    """Set the `status` cell of `name` in the manifest table (the one headed `project`), keeping every other byte.

    Returns `(new text, changed)`, or None when no manifest table has a status column and a row for the project. `ControlPlane` reads the manifest but has no writer for it, so the cell is edited here and then read back through `ControlPlane.load`.
    """
    lines = text.splitlines(keepends=True)
    position: int | None = None
    for index, raw in enumerate(lines):
        if not raw.lstrip().startswith("|"):
            position = None
            continue
        body = raw.rstrip("\r\n")
        cells = [c.strip() for c in body.strip().strip("|").split("|")]
        if cells and cells[0].lower() == "project":
            lowered = [c.lower() for c in cells]
            position = lowered.index("status") if "status" in lowered else None
            continue
        if position is None or cells[0] != name or position >= len(cells):
            continue
        parts = body.split("|")
        cell = parts[position + 1]
        if cell.strip() == value:
            return text, False
        parts[position + 1] = cell.replace(cell.strip(), value, 1) if cell.strip() else f" {value} "
        lines[index] = "|".join(parts) + raw[len(body) :]
        return "".join(lines), True
    return None


def _toggle_cells(root: Path, name: str, on: bool, dry: bool, strict: bool, status: str | None = None) -> dict[str, Any]:
    """Opt the project's `project:` rows in or out of its control-plane column, through `set_cell`.

    With `status`, the project's manifest status cell is set to it in the same write; `result["status"]` is `{from, to, changed}`, or None when the manifest has no status cell to set (the reconciler adds it).
    """
    path = root / CONTROL_PLANE_NAME
    result: dict[str, Any] = {"changed": [], "reconciled": True, "backup": None, "status": None}
    plane = ControlPlane.load(path) if path.is_file() else None
    if plane is None or name not in plane.columns:
        if strict:
            raise ResourceError("not-reconciled", f'control-plane.md has no column for "{name}".', hint="Run `stratarc reconcile` to add it, then run the command again.", param="name")
        result["reconciled"] = False
        return result
    rows = [row for row in plane.rows if row.startswith("project:")]
    status_changed = False
    with tempfile.TemporaryDirectory(prefix="stratarc-cp-") as tmp:
        probe = Path(tmp) / CONTROL_PLANE_NAME
        probe.write_bytes(path.read_bytes())
        for row in rows:
            try:
                if set_cell(probe, row, name, on):
                    result["changed"].append(row)
            except KeyError:
                continue
        if status is not None:
            edited = _set_manifest_status(probe.read_bytes().decode("utf-8"), name, status)
            if edited is not None:
                previous = plane.status(name)
                status_changed = edited[1]
                result["status"] = {"from": previous, "to": status, "changed": status_changed}
                if status_changed:
                    probe.write_bytes(edited[0].encode("utf-8"))
        updated = probe.read_bytes()
        if result["changed"] or status_changed:
            check = ControlPlane.load(probe)
            if any(check.enabled(name, row) != on for row in result["changed"]) or (status_changed and check.status(name) != status):
                raise ResourceError("invalid-edit", "control-plane.md did not take the change.", hint="The file was left unchanged.")
    if (result["changed"] or status_changed) and not dry:
        backup = layout.safe_write(path, updated, private=False)
        result["backup"] = str(backup) if backup else None
    return result


def project_list(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    path = root / CONTROL_PLANE_NAME
    plane = ControlPlane.load(path) if path.is_file() else ControlPlane()
    rows = [{"name": n, "path": f"projects-root/{n}", "files": len(_files_under(root / "projects-root" / n)), "status": plane.manifest.get(n, {}).get("status") or None} for n in _project_names(root)]
    text = "\n".join(f"{r['name']}  {r['status'] or 'not in control plane'}  {r['files']} files" for r in rows) or "no projects"
    return {"projects": rows}, text


def project_show(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name = ctx.args.name
    directory = _project_dir(root, name)
    path = root / CONTROL_PLANE_NAME
    plane = ControlPlane.load(path) if path.is_file() else ControlPlane()
    try:
        sources = layers.load_sources(layers.Request(root, project=name))
    except LayerError as exc:
        raise _layer_error(exc, root) from None
    values = {key: entry.value for source in sources if source.layer == "project" for key, entry in source.entries.items()}
    enabled = [row for row in plane.rows if row.startswith("project:") and plane.enabled(name, row)]
    data = {
        "name": name,
        "path": f"projects-root/{name}",
        "files": [p.relative_to(directory).as_posix() for p in _files_under(directory)],
        "manifest": plane.manifest.get(name, {}),
        "in_control_plane": name in plane.columns,
        "enabled_rows": enabled,
        "values": values,
    }
    lines = [f"project: {name}", f"folder: projects-root/{name}", "files:", *[f"  {f}" for f in data["files"]]]
    if data["manifest"]:
        lines.append("manifest: " + ", ".join(f"{k}={v or '-'}" for k, v in data["manifest"].items()))
    lines.append("control plane: " + (f"{len(enabled)} project rows enabled" if data["in_control_plane"] else "no column yet (run `stratarc reconcile`)"))
    if values:
        lines += ["values:", *[f"  {k} = {json.dumps(v, ensure_ascii=False)}" for k, v in values.items()]]
    return data, "\n".join(lines)


def project_add(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name = ctx.args.name
    if not validate_name(name):
        raise ResourceError("invalid-name", f'The project name "{name}" is not valid.', hint="Use lowercase letters, digits and hyphens, starting with a letter.", param="name")
    directory = root / "projects-root" / name
    if directory.exists():
        raise ResourceError("project-exists", f'The project "{name}" already exists under projects-root.', hint="Change it with `stratarc project edit`, or pick another name.", param="name")
    stub = directory / "AGENTS.md"
    saved = _save(stub, f"# {name}\n", root=root, dry=ctx.dry)
    cells = _toggle_cells(root, name, True, ctx.dry, strict=False)
    data = {"name": name, "file": saved, "control_plane": cells, "reconcile_needed": not cells["reconciled"], "dry_run": ctx.dry}
    text = f"project add: {'would create' if ctx.dry else 'created'} projects-root/{name}/AGENTS.md" + ("; dry run, nothing written" if ctx.dry else "")
    if cells["changed"]:
        text += f"\n  opted in {len(cells['changed'])} control-plane rows"
    if not cells["reconciled"]:
        text += "\n  run `stratarc reconcile` to add the project to the control plane"
    return data, text


def project_edit(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    directory = _project_dir(root, ctx.args.name)
    if ctx.args.file:
        rel = Path(ctx.args.file)
        if rel.is_absolute() or ".." in rel.parts:
            raise ResourceError("invalid-path", "The file must be a relative path inside the project folder.", hint="Pass --file with a path under projects-root/<project>/.", param="file")
        target = directory / rel
    else:
        target = next((directory / n for n in (CONFIG_NAME, "permissions.json", "AGENTS.md") if (directory / n).is_file()), directory / "AGENTS.md")
    saved = _edit_file(target, root=root, dry=ctx.dry, editor=ctx.editor, environ=ctx.environ)
    return {"name": ctx.args.name, "file": saved}, _describe_save(saved)


def project_remove(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name = ctx.args.name
    directory = _project_dir(root, name)
    files = _files_under(directory)
    shown = [p.relative_to(root).as_posix() for p in files]
    if not ctx.dry:
        ctx.need_yes(f'Removing the project "{name}"')
    backups: list[str] = []
    cells: dict[str, Any] = {"changed": [], "reconciled": True, "backup": None}
    if not ctx.dry:
        for path in files:
            made = _backup_file(path)
            if made:
                backups.append(made)
        shutil.rmtree(directory)
    cells = _toggle_cells(root, name, False, ctx.dry, strict=False)
    data = {"name": name, "removed": shown, "backups": len(backups), "control_plane": cells, "dry_run": ctx.dry}
    text = f"project remove: {'would remove' if ctx.dry else 'removed'} projects-root/{name} ({len(shown)} files)" + ("; dry run, nothing removed" if ctx.dry else f"; {len(backups)} backed up")
    return data, text


def _project_toggle(ctx: Context, on: bool) -> tuple[dict, str]:
    root = ctx.root
    name = ctx.args.name
    _project_dir(root, name)
    cells = _toggle_cells(root, name, on, ctx.dry, strict=True, status="active" if on else "inactive")
    word = "enable" if on else "disable"
    data = {"name": name, "enabled": on, "control_plane": cells, "reconcile_needed": cells["status"] is None, "dry_run": ctx.dry}
    status = cells["status"]
    moved = bool(status and status["changed"])
    if not cells["changed"] and not moved:
        text = f"project {word}: no control-plane cells needed to change for {name}"
    else:
        text = f"project {word}: {'would change' if ctx.dry else 'changed'} {len(cells['changed'])} control-plane cells for {name}"
        if moved:
            text += f" and set its status to {status['to']}"
        text += "; dry run, nothing written" if ctx.dry else ""
    if status is None:
        text += "\n  the manifest has no status cell for this project; run `stratarc reconcile` to add it"
    return data, text


def project_enable(ctx: Context) -> tuple[dict, str]:
    return _project_toggle(ctx, True)


def project_disable(ctx: Context) -> tuple[dict, str]:
    return _project_toggle(ctx, False)


# ---- runtimes -------------------------------------------------------------------------------


def _config_text(root: Path) -> tuple[Path, str, dict[str, Any]]:
    path = root / CONFIG_NAME
    if not path.is_file():
        return path, "", {}
    try:
        load_config(root)
    except ConfigError as exc:
        raise ResourceError("invalid-config", str(exc), hint="Fix stratarc.toml first.") from None
    text = _read(path)
    return path, text, tomllib.loads(text)


def _runtime_names(root: Path, doc: Mapping[str, Any]) -> list[str]:
    names = set(BUNDLED_RUNTIMES) | set(doc.get("runtimes", {}))
    directory = root / "runtimes"
    if directory.is_dir():
        names |= {p.stem for p in directory.iterdir() if p.suffix in layers.PARSERS}
    return sorted(names)


def _runtime_row(root: Path, doc: Mapping[str, Any], name: str) -> dict[str, Any]:
    table = doc.get("runtimes", {}).get(name)
    overlay = next((f"runtimes/{name}{s}" for s in (".toml", ".json") if (root / "runtimes" / f"{name}{s}").is_file()), None)
    return {
        "name": name,
        "configured": isinstance(table, dict),
        "enabled": bool(table.get("enabled", False)) if isinstance(table, dict) else False,
        "target": table.get("target") if isinstance(table, dict) else None,
        "default_target": BUNDLED_RUNTIMES.get(name),
        "overlay": overlay,
    }


def _known_runtime(root: Path, doc: Mapping[str, Any], name: str) -> None:
    if name not in _runtime_names(root, doc):
        raise ResourceError("unknown-runtime", f'The runtime "{name}" is not one stratarc knows.', hint="Run `stratarc runtime list` for the runtimes.", param="name")


def runtime_list(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    _, _, doc = _config_text(root)
    rows = [_runtime_row(root, doc, n) for n in _runtime_names(root, doc)]
    text = "\n".join(f"{r['name']:<9}{'enabled ' if r['enabled'] else 'disabled'}  {r['target'] or '(no target set)'}" for r in rows)
    return {"runtimes": rows}, text


def runtime_show(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    path, _, doc = _config_text(root)
    name = ctx.args.name
    _known_runtime(root, doc, name)
    row = _runtime_row(root, doc, name)
    line = None
    if row["configured"]:
        line = layers._toml_lines(_read(path)).get(("runtimes", name))
    row["file"] = _where(path, root) + (f":{line}" if line else "") if row["configured"] else None
    lines = [f"runtime: {name}", f"enabled: {'yes' if row['enabled'] else 'no'}", f"target: {row['target'] or '(none)'}" + ("" if row["target"] else f" (default {row['default_target']})" if row["default_target"] else "")]
    lines.append(f"defined in: {row['file'] or 'not in stratarc.toml'}")
    if row["overlay"]:
        lines.append(f"overlay: {row['overlay']}")
    return row, "\n".join(lines)


def _runtime_change(ctx: Context, name: str, changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    root = ctx.root
    path, text, doc = _config_text(root)
    _known_runtime(root, doc, name)
    current = doc.get("runtimes", {}).get(name)
    current = current if isinstance(current, dict) else None
    want = dict(changes)
    if current is None and name in BUNDLED_RUNTIMES:
        want.setdefault("target", BUNDLED_RUNTIMES[name])
    if current is not None and all(current.get(k) == v for k, v in want.items()):
        return _runtime_row(root, doc, name), {"file": _where(path, root), "changed": False, "created": False, "backup": None, "dry_run": ctx.dry}
    new_text = set_toml_values(text, ("runtimes", name), {k: _literal(v) for k, v in want.items()})
    expected = copy.deepcopy(doc)
    expected.setdefault("runtimes", {}).setdefault(name, {}).update(want)
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ResourceError("invalid-edit", f"stratarc.toml could not be edited safely: {exc}.", hint="Edit the file by hand. It was left unchanged.") from None
    if after != expected:
        raise ResourceError("invalid-edit", "stratarc.toml could not be edited safely: the layout is not one the editor follows.", hint="Edit the file by hand. It was left unchanged.")
    saved = _save(path, new_text, root=root, dry=ctx.dry, role="config")
    return _runtime_row(root, after, name), saved


def _runtime_switch(ctx: Context, on: bool) -> tuple[dict, str]:
    name = ctx.args.name
    row, saved = _runtime_change(ctx, name, {"enabled": on})
    word = "enable" if on else "disable"
    data = {"runtime": row, "file": saved, "dry_run": ctx.dry}
    if not saved["changed"]:
        return data, f"runtime {word}: {name} is already {'enabled' if on else 'disabled'}"
    return data, f"runtime {word}: {name}\n  " + _describe_save(saved)


def runtime_enable(ctx: Context) -> tuple[dict, str]:
    return _runtime_switch(ctx, True)


def runtime_disable(ctx: Context) -> tuple[dict, str]:
    return _runtime_switch(ctx, False)


def runtime_target(ctx: Context) -> tuple[dict, str]:
    name, target = ctx.args.name, ctx.args.path
    if not target.strip():
        raise ResourceError("invalid-value", "The target must be a non-empty path.", hint="Pass the directory the runtime reads its configuration from.", param="path")
    row, saved = _runtime_change(ctx, name, {"target": target})
    data = {"runtime": row, "file": saved, "dry_run": ctx.dry}
    if not saved["changed"]:
        return data, f"runtime target: {name} already deploys to {target}"
    return data, f"runtime target: {name} -> {target}\n  " + _describe_save(saved)


# ---- agents ---------------------------------------------------------------------------------


def _agent_files(root: Path, name: str, project: str | None) -> list[Path]:
    directories = ([root / "projects-root" / project / "agents"] if project else []) + [root / "agents"]
    return [d / f"{name}{s}" for d in directories for s in AGENT_SUFFIXES if (d / f"{name}{s}").is_file()]


def _check_agent(root: Path, name: str, project: str | None) -> list[Path]:
    if project:
        _project_dir(root, project)
    files = _agent_files(root, name, project) if _SAFE_NAME.match(name) else []
    if not files:
        raise ResourceError("unknown-agent", f'The agent "{name}" has no file under agents/.', hint="Check the spelling, or pass --project when the agent belongs to a project.", param="name")
    return files


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    block = text[3:].split("\n---", 1)[0]
    out = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith(" "):
            out[key.strip()] = value.strip()
    return out


def agent_list(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    wanted = ctx.args.project
    if wanted:
        _project_dir(root, wanted)
    scopes: list[tuple[str | None, Path]] = [(None, root / "agents")]
    for project in [wanted] if wanted else _project_names(root):
        scopes.append((project, root / "projects-root" / project / "agents"))
    rows = []
    for project, directory in scopes:
        if not directory.is_dir():
            continue
        grouped: dict[str, list[str]] = {}
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix in AGENT_SUFFIXES:
                grouped.setdefault(path.stem, []).append(path.relative_to(root).as_posix())
        rows += [{"name": n, "project": project, "files": f} for n, f in grouped.items()]
    text = "\n".join(f"{r['name']}  ({r['project'] or 'source root'})  {', '.join(r['files'])}" for r in rows) or "no agents"
    return {"agents": rows}, text


def agent_show(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name, project = ctx.args.name, ctx.args.project
    files = _check_agent(root, name, project)
    definition: dict[str, str] = {}
    for path in files:
        if path.suffix == ".md":
            definition = _frontmatter(_read(path))
            break
    try:
        sources = layers.load_sources(layers.Request(root, project=project, agent=name))
    except LayerError as exc:
        raise _layer_error(exc, root) from None
    values = {key: entry.value for source in sources if source.layer == "agent" for key, entry in source.entries.items()}
    data = {"name": name, "project": project, "files": [{"file": _where(p, root), "kind": "definition" if p.suffix == ".md" else "settings"} for p in files], "definition": definition, "values": values}
    lines = [f"agent: {name}" + (f" (project: {project})" if project else ""), "files:", *[f"  {f['file']}  {f['kind']}" for f in data["files"]]]
    if definition.get("description"):
        lines.append(f"description: {definition['description']}")
    if values:
        lines += ["values:", *[f"  {k} = {json.dumps(v, ensure_ascii=False)}" for k, v in values.items()]]
    return data, "\n".join(lines)


def agent_edit(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name, project = ctx.args.name, ctx.args.project
    files = _check_agent(root, name, project)
    if ctx.args.definition:
        files = [p for p in files if p.suffix == ".md"]
        if not files:
            raise ResourceError("unknown-agent", f'The agent "{name}" has no definition file (.md).', hint="Drop --definition to edit its settings file.", param="name")
    saved = _edit_file(files[0], root=root, dry=ctx.dry, editor=ctx.editor, environ=ctx.environ)
    return {"name": name, "project": project, "file": saved}, _describe_save(saved)


DEFAULT_AGENT_TOOLS = ("Read", "Grep", "Glob")


def _agent_tools(raw: list[str] | None) -> list[str]:
    from stratarc.validate import KNOWN_TOOLS, MCP_TOOL

    names = [name for item in raw or [] for name in item.split(",") if name.strip()]
    names = [name.strip() for name in names]
    for name in names:
        if name not in KNOWN_TOOLS and not MCP_TOOL.match(name):
            raise ResourceError("invalid-value", f'"{name}" is not a tool an agent can declare.', hint="Use names such as Read, Grep, Glob, Edit, Bash, or an mcp__server__tool name.", param="tools")
    return list(dict.fromkeys(names))


def agent_add(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    args = ctx.args
    name = args.name
    if not validate_name(name):
        raise ResourceError("invalid-name", f'The agent name "{name}" is not valid.', hint="Use lowercase letters, digits and hyphens, starting with a letter.", param="name")
    if _agent_files(root, name, None):
        raise ResourceError("agent-exists", f'The agent "{name}" already exists.', hint="Change it with `stratarc agent edit`, or pick another name.", param="name")
    inherited: dict[str, str] = {}
    if args.parent:
        parent_files = [p for p in _check_agent(root, args.parent, None) if p.suffix == ".md"]
        if not parent_files:
            raise ResourceError("unknown-agent", f'The agent "{args.parent}" has no definition file (.md) to inherit from.', hint="Pick a parent that has a definition, or drop --parent.", param="parent")
        inherited = _frontmatter(_read(parent_files[0]))
    tools = _agent_tools(args.tools) or [t.strip() for t in inherited.get("tools", "").split(",") if t.strip()] or list(DEFAULT_AGENT_TOOLS)
    description = args.description if args.description is not None else inherited.get("description", "").strip("\"'") or f"The {name} agent."
    if not description.strip() or "\n" in description or "\r" in description:
        raise ResourceError("invalid-value", "The description must be one non-empty line.", hint="Pass --description with a single line of text.", param="description")
    model = inherited.get("model") or "inherit"
    text = f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\ntools: {', '.join(tools)}\nmodel: {model}\n---\n\n# {name}\n\nDescribe what this agent does and how it should work.\n"
    saved = _save(root / "agents" / f"{name}.md", text, root=root, dry=ctx.dry)
    return {"name": name, "parent": args.parent, "tools": tools, "file": saved}, "agent add: " + _describe_save(saved)


def agent_remove(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name, project = ctx.args.name, ctx.args.project
    _check_agent(root, name, project)
    directory = root / "projects-root" / project / "agents" if project else root / "agents"
    files = [directory / f"{name}{s}" for s in AGENT_SUFFIXES if (directory / f"{name}{s}").is_file()]
    if not files:
        raise ResourceError("unknown-agent", f'The agent "{name}" has no file in {_where(directory, root)}.', hint="Pass --project when the agent belongs to a project, or check the spelling.", param="name")
    shown = [_where(p, root) for p in files]
    backups_made: list[str] = []
    if not ctx.dry:
        ctx.need_yes(f'Removing the agent "{name}"')
        for path in files:
            made = _backup_file(path)
            if made:
                backups_made.append(made)
            path.unlink()
    data = {"name": name, "project": project, "removed": shown, "backups": len(backups_made), "dry_run": ctx.dry}
    return data, f"agent remove: {'would remove' if ctx.dry else 'removed'} {', '.join(shown)}" + ("; dry run, nothing removed" if ctx.dry else f"; {len(backups_made)} backed up")


def agent_explain(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    args = ctx.args
    _check_agent(root, args.name, args.project)
    try:
        layered = layers.load(root, project=args.project, agent=args.name, account=args.account, runtime=args.runtime, environ=ctx.environ)
    except LayerError as exc:
        raise _layer_error(exc, root) from None
    at_agent = sorted({key for source in layered.sources if source.layer == "agent" for key in source.entries})
    if args.key:
        at_agent = [k for k in at_agent if k == args.key or k.startswith(args.key + ".")]
    entries, errors = [], []
    for key in at_agent:
        try:
            res = layered.resolve(key)
        except LayerError as exc:
            errors.append(_layer_error(exc, root))
            continue
        entries.append(
            {
                "key": key,
                "value": res.value,
                "decided_by": {"layer": res.decided_by.layer, "op": res.decided_by.op},
                "steps": [{"layer": s.layer, "file": layers.display_path(s.file, root) if s.file else s.label, "line": s.line, "op": s.op, "value": s.value} for s in res.steps],
            }
        )
    if errors:
        raise errors[0]
    blocks = []
    for e in entries:
        block = [f"{e['key']} = {json.dumps(e['value'], ensure_ascii=False)}   decided by: {e['decided_by']['layer']} ({e['decided_by']['op']})"]
        block += [f"  {s['layer']:<9}{s['file']}{':' + str(s['line']) if s['line'] else ''}  {s['op']}  {json.dumps(s['value'], ensure_ascii=False)}" for s in e["steps"]]
        blocks.append("\n".join(block))
    return {"name": args.name, "project": args.project, "keys": entries}, "\n\n".join(blocks) or f"{args.name} sets no values of its own"


# ---- accounts -------------------------------------------------------------------------------


def _account_files(root: Path, name: str) -> list[Path]:
    return [root / "accounts" / f"{name}{s}" for s in ACCOUNT_SUFFIXES if (root / "accounts" / f"{name}{s}").is_file()] if _SAFE_NAME.match(name) else []


def _check_account(root: Path, name: str) -> list[Path]:
    files = _account_files(root, name)
    if not files:
        raise ResourceError("unknown-account", f'The account "{name}" has no file under accounts/.', hint="Run `stratarc account list`, or create one with `stratarc account add`.", param="name")
    return files


def account_list(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    directory = root / "accounts"
    grouped: dict[str, list[str]] = {}
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix in ACCOUNT_SUFFIXES:
                grouped.setdefault(path.stem, []).append(path.relative_to(root).as_posix())
    rows = [{"name": n, "files": f} for n, f in grouped.items()]
    return {"accounts": rows}, "\n".join(f"{r['name']}  {', '.join(r['files'])}" for r in rows) or "no accounts"


def account_show(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name = ctx.args.name
    files = _check_account(root, name)
    values = {}
    for path in files:
        try:
            doc, lines = layers.PARSERS[path.suffix](path)
            entries = layers.flatten(doc, lines, path)
        except LayerError as exc:
            raise _layer_error(exc, root) from None
        values.update({k: {"value": e.value, "line": e.line, "file": _where(path, root)} for k, e in entries.items()})
    data = {"name": name, "files": [_where(p, root) for p in files], "values": values}
    lines_out = [f"account: {name}", *[f"file: {f}" for f in data["files"]]]
    lines_out += [f"  {k} = {json.dumps(v['value'], ensure_ascii=False)}   ({v['file']}{':' + str(v['line']) if v['line'] else ''})" for k, v in values.items()]
    return data, "\n".join(lines_out)


def _parse_assignment(item: str) -> tuple[list[str], Any]:
    key, sep, raw = item.partition("=")
    key = key.strip()
    parts = key.split(".")
    if not sep or not key or not all(_KEY_PART.match(p) for p in parts):
        raise ResourceError("invalid-value", f'"{item}" is not KEY=VALUE.', hint="Write it like permissions.timeout=60.", param="set")
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    if isinstance(value, (dict, float, type(None))):
        raise ResourceError("invalid-value", f'"{key}" cannot take that value.', hint="Use strings, integers, booleans or lists of strings.", param="set")
    return parts, value


def _parse_assignments(items: list[str]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for item in items:
        parts, value = _parse_assignment(item)
        key = ".".join(parts)
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ResourceError("invalid-value", f'"{key}" conflicts with another value.', param="set")
        if isinstance(node.get(parts[-1]), dict) or isinstance(value, (dict, float, type(None))):
            raise ResourceError("invalid-value", f'"{key}" cannot take that value.', hint="Use strings, integers, booleans or lists of strings.", param="set")
        node[parts[-1]] = value
    return tree


def account_add(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name = ctx.args.name
    if not validate_name(name):
        raise ResourceError("invalid-name", f'The account name "{name}" is not valid.', hint="Use lowercase letters, digits and hyphens, starting with a letter.", param="name")
    if _account_files(root, name):
        raise ResourceError("account-exists", f'The account "{name}" already exists.', hint="Change it with `stratarc account edit`.", param="name")
    tree = _parse_assignments(ctx.args.set or [])
    try:
        body = layout.dump_toml(tree) if tree else ""
    except TypeError as exc:
        raise ResourceError("invalid-value", str(exc), hint="Use strings, integers, booleans or lists of strings.", param="set") from None
    text = f"# Values tied to the account {name}.\n" + (("\n" + body) if body else "")
    saved = _save(root / "accounts" / f"{name}.toml", text, root=root, dry=ctx.dry)
    return {"name": name, "file": saved}, "account add: " + _describe_save(saved)


def account_edit(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    files = _check_account(root, ctx.args.name)
    sets, unsets = ctx.args.set or [], ctx.args.unset or []
    if sets or unsets:
        saved = _change_account_values(files[0], sets, unsets, root=root, dry=ctx.dry)
    else:
        saved = _edit_file(files[0], root=root, dry=ctx.dry, editor=ctx.editor, environ=ctx.environ)
    return {"name": ctx.args.name, "file": saved}, _describe_save(saved)


def _change_account_values(path: Path, sets: list[str], unsets: list[str], *, root: Path, dry: bool) -> dict[str, Any]:
    """Apply `--set KEY=VALUE` and `--unset KEY` to an account file, then save it like any other edit.

    A TOML file goes through the line editor, which keeps comments and layout. A JSON file is parsed, changed and written back with two-space indentation; its key order is kept and a new key goes last.
    """
    shown = _where(path, root)
    if path.suffix == ".json":
        return _change_json_values(path, sets, unsets, root=root, dry=dry)
    text = _read(path)
    try:
        expected = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ResourceError("invalid-edit", f"{shown} cannot be edited safely: {exc}.", hint="Fix the file by hand first.", param=shown) from None
    unset_keys = []
    for item in unsets:
        parts = item.strip().split(".")
        if not all(_KEY_PART.match(p) for p in parts):
            raise ResourceError("invalid-value", f'"{item}" is not a key.', hint="Write it like permissions.timeout.", param="unset")
        unset_keys.append(parts)
    assignments = [_parse_assignment(item) for item in sets]
    overlap = {".".join(p) for p, _ in assignments} & {".".join(p) for p in unset_keys}
    if overlap:
        raise ResourceError("invalid-value", f'"{sorted(overlap)[0]}" is both set and unset.', hint="Use one of --set and --unset for each key.", param="set")
    new_text = text
    for parts, value in assignments:
        table, key = tuple(parts[:-1]), parts[-1]
        node = expected
        for part in table:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ResourceError("invalid-edit", f'"{".".join(parts)}" conflicts with an existing value in {shown}.', hint="The file was left unchanged.", param="set")
        if isinstance(node.get(key), dict):
            raise ResourceError("invalid-edit", f'"{".".join(parts)}" is a table in {shown}, not a value.', hint="Set one of its keys instead. The file was left unchanged.", param="set")
        try:
            literal = layout._toml_value(value)
        except TypeError as exc:
            raise ResourceError("invalid-value", str(exc), hint="Use strings, integers, booleans or lists of strings.", param="set") from None
        node[key] = value
        new_text = set_toml_values(new_text, table, {key: literal})
    for parts in unset_keys:
        table, key = tuple(parts[:-1]), parts[-1]
        node = expected
        for part in table:
            node = node.get(part) if isinstance(node, dict) else None
        removed = remove_toml_key(new_text, table, key) if isinstance(node, dict) and key in node and not isinstance(node[key], dict) else None
        if removed is None:
            raise ResourceError("unknown-key", f'The key "{".".join(parts)}" is not set in {shown}.', hint="Run `stratarc account show NAME` for the keys.", param="unset")
        del node[key]
        new_text = removed
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ResourceError("invalid-edit", f"{shown} could not be edited safely: {exc}.", hint="Edit the file by hand. It was left unchanged.", param=shown) from None
    if after != expected:
        raise ResourceError("invalid-edit", f"{shown} could not be edited safely: the layout is not one the editor follows.", hint="Edit the file by hand. It was left unchanged.", param=shown)
    return _save(path, new_text, root=root, dry=dry)


def _change_json_values(path: Path, sets: list[str], unsets: list[str], *, root: Path, dry: bool) -> dict[str, Any]:
    shown = _where(path, root)
    try:
        doc = json.loads(_read(path))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResourceError("invalid-edit", f"{shown} cannot be edited safely: {exc}.", hint="Fix the file by hand first.", param=shown) from None
    if not isinstance(doc, dict):
        raise ResourceError("invalid-edit", f"{shown} cannot be edited safely: the top level is not an object.", hint="Fix the file by hand first.", param=shown)
    unset_keys = []
    for item in unsets:
        parts = item.strip().split(".")
        if not all(_KEY_PART.match(p) for p in parts):
            raise ResourceError("invalid-value", f'"{item}" is not a key.', hint="Write it like permissions.timeout.", param="unset")
        unset_keys.append(parts)
    assignments = [_parse_assignment(item) for item in sets]
    overlap = {".".join(p) for p, _ in assignments} & {".".join(p) for p in unset_keys}
    if overlap:
        raise ResourceError("invalid-value", f'"{sorted(overlap)[0]}" is both set and unset.', hint="Use one of --set and --unset for each key.", param="set")
    for parts, value in assignments:
        node = doc
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ResourceError("invalid-edit", f'"{".".join(parts)}" conflicts with an existing value in {shown}.', hint="The file was left unchanged.", param="set")
        if isinstance(node.get(parts[-1]), dict):
            raise ResourceError("invalid-edit", f'"{".".join(parts)}" is an object in {shown}, not a value.', hint="Set one of its keys instead. The file was left unchanged.", param="set")
        node[parts[-1]] = value
    for parts in unset_keys:
        node = doc
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict) or parts[-1] not in node or isinstance(node[parts[-1]], dict):
            raise ResourceError("unknown-key", f'The key "{".".join(parts)}" is not set in {shown}.', hint="Run `stratarc account show NAME` for the keys.", param="unset")
        del node[parts[-1]]
    return _save(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", root=root, dry=dry)


def account_remove(ctx: Context) -> tuple[dict, str]:
    root = ctx.root
    name = ctx.args.name
    files = _check_account(root, name)
    shown = [_where(p, root) for p in files]
    backups: list[str] = []
    if not ctx.dry:
        ctx.need_yes(f'Removing the account "{name}"')
        for path in files:
            made = _backup_file(path)
            if made:
                backups.append(made)
            path.unlink()
    data = {"name": name, "removed": shown, "backups": len(backups), "dry_run": ctx.dry}
    return data, f"account remove: {'would remove' if ctx.dry else 'removed'} {', '.join(shown)}" + ("; dry run, nothing removed" if ctx.dry else f"; {len(backups)} backed up")


# ---- command line ---------------------------------------------------------------------------

HANDLERS: dict[tuple[str, str], Callable[[Context], tuple[dict, str]]] = {
    ("source", "init"): source_init,
    ("source", "show"): source_show,
    ("source", "use"): source_use,
    ("source", "list"): source_list,
    ("source", "move"): source_move,
    ("project", "add"): project_add,
    ("project", "list"): project_list,
    ("project", "show"): project_show,
    ("project", "edit"): project_edit,
    ("project", "remove"): project_remove,
    ("project", "enable"): project_enable,
    ("project", "disable"): project_disable,
    ("runtime", "list"): runtime_list,
    ("runtime", "show"): runtime_show,
    ("runtime", "enable"): runtime_enable,
    ("runtime", "disable"): runtime_disable,
    ("runtime", "target"): runtime_target,
    ("agent", "list"): agent_list,
    ("agent", "show"): agent_show,
    ("agent", "add"): agent_add,
    ("agent", "edit"): agent_edit,
    ("agent", "remove"): agent_remove,
    ("agent", "explain"): agent_explain,
    ("account", "list"): account_list,
    ("account", "show"): account_show,
    ("account", "add"): account_add,
    ("account", "edit"): account_edit,
    ("account", "remove"): account_remove,
}


def _parser() -> argparse.ArgumentParser:
    def options(suppress: bool) -> argparse.ArgumentParser:
        extra: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress else {}
        p = argparse.ArgumentParser(add_help=False)
        p.add_argument("--root", metavar="PATH", help="the source root", **extra)
        p.add_argument("--json", action="store_true", help="print one JSON envelope", **extra)
        return p

    common = options(True)
    parser = argparse.ArgumentParser(prog="stratarc", description="List, show and change the resources of a source root.", parents=[options(False)])
    resources = parser.add_subparsers(dest="resource", required=True)

    def verbs(resource: str, help_text: str) -> Any:
        return resources.add_parser(resource, help=help_text, parents=[common]).add_subparsers(dest="verb", required=True)

    def leaf(group: Any, verb: str, help_text: str, *, write: bool = False, yes: bool = False) -> argparse.ArgumentParser:
        p = group.add_parser(verb, parents=[common], help=help_text)
        if write:
            p.add_argument("--dry-run", action="store_true", dest="dry_run", help="validate and report the change, write nothing")
        if yes:
            p.add_argument("--yes", action="store_true", help="confirm the deletion")
        return p

    source = verbs("source", "the source root: where it lives, which one is active")
    p = leaf(source, "init", "scaffold a source root and register it", write=True)
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--use", action="store_true", help="make it the active source root")
    leaf(source, "show", "the effective source root")
    leaf(source, "list", "the registered source roots")
    leaf(source, "use", "make a source root the active one", write=True).add_argument("target", metavar="NAME-or-PATH")
    p = leaf(source, "move", "move a registered source root", write=True, yes=True)
    p.add_argument("path", metavar="NEW_PATH")
    p.add_argument("--name")

    project = verbs("project", "managed projects and their overrides")
    leaf(project, "add", "create a project folder", write=True).add_argument("name")
    leaf(project, "list", "the projects")
    leaf(project, "show", "one project").add_argument("name")
    p = leaf(project, "edit", "open the project's owning file in $EDITOR", write=True)
    p.add_argument("name")
    p.add_argument("--file", metavar="REL", help="a file inside the project folder")
    leaf(project, "remove", "delete a project folder", write=True, yes=True).add_argument("name")
    leaf(project, "enable", "opt the project's rows in", write=True).add_argument("name")
    leaf(project, "disable", "opt the project's rows out", write=True).add_argument("name")

    runtime = verbs("runtime", "the supported agent runtimes and where each deploys")
    leaf(runtime, "list", "the runtimes")
    leaf(runtime, "show", "one runtime").add_argument("name")
    leaf(runtime, "enable", "build and deploy this runtime", write=True).add_argument("name")
    leaf(runtime, "disable", "stop building this runtime", write=True).add_argument("name")
    p = leaf(runtime, "target", "set the directory a runtime deploys to", write=True)
    p.add_argument("name")
    p.add_argument("path")

    agent = verbs("agent", "per-agent settings inside a project or the source root")
    leaf(agent, "list", "the agents").add_argument("--project", metavar="P")
    p = leaf(agent, "show", "one agent")
    p.add_argument("name")
    p.add_argument("--project", metavar="P")
    p = leaf(agent, "add", "create an agent definition in the source root", write=True)
    p.add_argument("name")
    p.add_argument("--parent", metavar="P", help="an existing agent whose tools and description are the defaults")
    p.add_argument("--tools", nargs="+", metavar="TOOL", help="the tools the agent may use (space or comma separated)")
    p.add_argument("--description", metavar="TEXT", help="one line saying when to use the agent")
    p = leaf(agent, "remove", "delete an agent's files, keeping a backup", write=True, yes=True)
    p.add_argument("name")
    p.add_argument("--project", metavar="P")
    p = leaf(agent, "edit", "open the agent's file in $EDITOR", write=True)
    p.add_argument("name")
    p.add_argument("--project", metavar="P")
    p.add_argument("--definition", action="store_true", help="edit the .md definition instead of the settings file")
    p = leaf(agent, "explain", "where each of the agent's values comes from")
    p.add_argument("name")
    p.add_argument("--project", metavar="P")
    p.add_argument("--account", metavar="X")
    p.add_argument("--runtime", metavar="R")
    p.add_argument("--key", metavar="KEY")

    account = verbs("account", "named accounts and the settings tied to them")
    leaf(account, "list", "the accounts")
    leaf(account, "show", "one account").add_argument("name")
    p = leaf(account, "add", "create an account file", write=True)
    p.add_argument("name")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="a value to write (repeatable)")
    p = leaf(account, "edit", "open the account's file in $EDITOR, or change values with --set and --unset", write=True)
    p.add_argument("name")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="set a value, keeping the file's comments (repeatable)")
    p.add_argument("--unset", action="append", metavar="KEY", help="remove a value (repeatable)")
    leaf(account, "remove", "delete an account file", write=True, yes=True).add_argument("name")
    return parser


def main(argv: list[str] | None = None, *, editor: Editor | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Run one resource verb and return the exit status. `editor` replaces $EDITOR for `edit` verbs."""
    args = _parser().parse_args(argv)
    ctx = Context(args, editor, os.environ if environ is None else environ)
    try:
        data, text = HANDLERS[(args.resource, args.verb)](ctx)
    except LayerError as exc:
        return layout.emit(args.json, error=_layer_error(exc, ctx.candidate_root))
    except ConfigError as exc:
        return layout.emit(args.json, error=ResourceError("invalid-config", str(exc), hint="Fix stratarc.toml first."))
    except ToolError as exc:
        return layout.emit(args.json, error=exc)
    return layout.emit(args.json, data=_tidy(data), text=layers.tilde(text))


if __name__ == "__main__":
    sys.exit(main())
