"""Render the MCP servers components.json declares into each runtime's own
configuration: the adapter half of the component manifest.

The design is adapters-render-what-the-runtime-supports, with MCP servers as
the first slice. The staging step writes a components.json into the global stage
that holds only the wanted MCP servers and plugins the global column selects
(the mcp: and plugin: rows of control-plane.md), so an adapter renders the
column's choice without reading the control plane.

An adapter renders a selected server when all of these hold: the entry names
its runtime, its owner is the shared config (a server another tool installs is that
tool's to write; it is declared so the inventory calls it declared), it is not
a hosted connector (the provider's account starts those), and no env value is
a secret:// reference (secrets resolve at launch, never at sync; the
environment references are rendered as written). Everything left out is named by notes(), which
sync.py prints, so nothing is silently dropped.

What is rendered is recorded in <name>-mcp-servers.json inside the target,
so a server that is deselected, marked unwanted or deleted from the manifest
is removed from the runtime file on the next sync. A server the operator
added by hand is never in that record and is never touched; the reverse pass
reports it undeclared.

Where each runtime keeps user-scope MCP servers:

    codex     [mcp_servers.<name>] in config.toml                rendered
    gemini    mcpServers in settings.json                        rendered
    opencode  mcp in opencode.jsonc                              rendered
    claude    mcpServers in ~/.claude.json, outside ~/.claude    not rendered; session-launch.py --mcp-config
    cursor    mcpServers in ~/.cursor/mcp.json                   not rendered until confirmed live (decision 5)

The per-runtime shapes are shared with the session launcher, which
renders the same servers per session, so the two never spell a server
differently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # Python 3.11 and later
    import tomllib
except ModuleNotFoundError:  # pragma: no cover, older interpreters only
    tomllib = None  # type: ignore[assignment]

from ._common import is_claude_model, read_frontmatter
from ._inventory import strip_jsonc
from stratarc.paths import engine_name

STAGED = "components.json"
RENDERED_RUNTIMES = ("codex", "gemini", "opencode")


def ledger_name(root: Optional[Path] = None) -> str:
    """The ledger file name, `<engine name>-mcp-servers.json`."""
    return f"{engine_name(root)}-mcp-servers.json"


def owner(root: Optional[Path] = None) -> str:
    """The owner value that marks a server the shared config renders: the engine name."""
    return engine_name(root)


class RenderRefused(RuntimeError):
    """A runtime file this module cannot rewrite without harming it.

    Raised, never returned as an action: sync.py turns it into a failed
    run for that runtime, prints it, and leaves the target unstamped
    (Codex review 3 of PR 92).
    """


_NOT_RENDERED = {
    "claude": (
        "is not rendered at user scope: Claude Code keeps user MCP servers in ~/.claude.json, "
        "outside ~/.claude; the session launcher passes it per session with --mcp-config"
    ),
    "cursor": (
        "is not rendered: Cursor's MCP configuration joins after it is confirmed on a live install "
        "(pending the component manifest's Cursor slice)"
    ),
}


# ----- the runtime shapes, shared with the session launcher -----


def neutral_server(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The runtime-neutral part of a components.json MCP server entry."""
    return {key: entry[key] for key in ("command", "args", "env", "cwd", "url", "transport") if entry.get(key)}


def without_secrets(server: Dict[str, Any]) -> Dict[str, Any]:
    """A server entry with every secret:// env value dropped, for a disabled entry.

    A server written only to switch it off never starts, so it needs no
    credential, and the URI must not reach the runtime unresolved.
    """
    env = {key: value for key, value in (server.get("env") or {}).items() if not (isinstance(value, str) and value.startswith("secret://"))}
    out = {key: value for key, value in server.items() if key != "env"}
    if env:
        out["env"] = env
    return out


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_value(value: Any) -> str:
    """One TOML value, inline, as Codex parses the right side of -c key=value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = [f"{key if _BARE_KEY.match(key) else json.dumps(key)} = {toml_value(item)}" for key, item in value.items()]
        return "{" + ", ".join(pairs) + "}"
    raise ValueError(f"cannot render {value!r} as TOML")


def codex_server(entry: Dict[str, Any]) -> Dict[str, Any]:
    server = neutral_server(entry)
    server.pop("transport", None)
    return server


def claude_server(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Claude Code's mcpServers shape: type stdio for a command, the transport (default http) for a url."""
    server = neutral_server(entry)
    transport = server.pop("transport", None)
    server["type"] = (transport or "http") if "url" in server else "stdio"
    return server


def gemini_server(entry: Dict[str, Any]) -> Dict[str, Any]:
    server = neutral_server(entry)
    transport = server.pop("transport", None)
    if "url" in server and transport != "sse":
        server["httpUrl"] = server.pop("url")
    return server


def opencode_server(entry: Dict[str, Any], enabled: bool) -> Dict[str, Any]:
    server = neutral_server(entry) if enabled else without_secrets(neutral_server(entry))
    if "url" in server:
        return {"type": "remote", "url": server["url"], "enabled": enabled}
    out: Dict[str, Any] = {"type": "local", "command": [server["command"]] + list(server.get("args") or [])}
    if server.get("env"):
        out["environment"] = server["env"]
    out["enabled"] = enabled
    return out


# ----- what the stage selects -----


def staged(source: Path) -> Dict[str, Any]:
    """The stage's components.json, or an empty selection when it has none."""
    path = source / STAGED
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _secret_keys(entry: Dict[str, Any]) -> List[str]:
    env = entry.get("env") or {}
    return sorted(key for key, value in env.items() if isinstance(value, str) and value.startswith("secret://")) if isinstance(env, dict) else []


def renders(entry: Any, runtime: str, root: Optional[Path] = None) -> bool:
    """True for a declared MCP server an adapter writes for this runtime:
    the shared config owns it and it is not a hosted connector. Also the test of which
    names a project's local scope owns."""
    return (
        isinstance(entry, dict)
        and bool(entry.get("name"))
        and runtime in (entry.get("runtimes") or [])
        and entry.get("owner") == owner(root)
        and entry.get("hosted") is not True
    )


def selected_servers(source: Path, runtime: str, project: bool = False) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """(name to entry for each server this runtime renders, a note for each one left out).

    project is True for a project column's local scope, which Claude Code
    has even though its user scope is not rendered.
    """
    rendered: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []
    for entry in staged(source).get("mcp_servers") or []:
        if not isinstance(entry, dict) or entry.get("wanted") is not True:
            continue
        if runtime not in (entry.get("runtimes") or []):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        label = f"mcp server {name}"
        if runtime in _NOT_RENDERED and not project:
            notes.append(f"{label} {_NOT_RENDERED[runtime]}")
        elif entry.get("owner") != owner(source):
            notes.append(f"{label} is not rendered: installed by {entry.get('owner')}, which writes it")
        elif entry.get("hosted") is True:
            notes.append(f"{label} is not rendered: a hosted connector is started by the provider's account")
        elif _secret_keys(entry):
            keys = ", ".join(_secret_keys(entry))
            notes.append(
                f"{label} is not rendered: env {keys} is a secret:// reference, resolved only at launch "
                "(the session launcher with a credentials profile)"
            )
        else:
            rendered[name] = entry
    return rendered, notes


def plugin_key(entry: Dict[str, Any]) -> str:
    """The enablement key a plugin entry names: name@marketplace, or its name alone.

    Shared with the session launcher, so the sync and the launcher never
    spell one plugin two ways.
    """
    name = str(entry.get("name") or "")
    return f"{name}@{entry['marketplace']}" if entry.get("marketplace") else name


def selected_plugins(source: Path, runtime: str) -> Dict[str, Dict[str, Any]]:
    """Enablement key to entry for each wanted plugin the stage selects for this runtime."""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in staged(source).get("plugins") or []:
        if isinstance(entry, dict) and entry.get("wanted") is True and entry.get("name") and runtime in (entry.get("runtimes") or []):
            out[plugin_key(entry)] = entry
    return out


def enablement(installed: List[str], selected: List[str]) -> Dict[str, bool]:
    """Every installed or selected plugin key, true only when selected.

    The adapter owns the enablement key: a plugin that is installed but not selected, declared
    or not, is written false, so it stops loading until it is declared and its
    cell is set.
    """
    chosen = set(selected)
    return {key: key in chosen for key in sorted(set(installed) | chosen)}


# Runtimes whose plugin enablement key an adapter writes today. The others
# wait for their key to be confirmed on a live install, and
# notes() names every selected plugin they leave out.
PLUGINS_RENDERED = ("claude",)
_PLUGINS_NOT_RENDERED = (
    "is selected but not enabled here: this runtime's plugin enablement key is not yet confirmed "
    "on a live install"
)


def runtime_settings(source: Path, runtime: str) -> Dict[str, Any]:
    """runtime_settings.<runtime> of the stage's components.json, or {}."""
    settings = staged(source).get("runtime_settings")
    value = settings.get(runtime) if isinstance(settings, dict) else None
    return value if isinstance(value, dict) else {}


def translate_model(value: Any, aliases: Any) -> Optional[str]:
    """A model name through a runtime's alias map; the name itself when it has no alias."""
    if not isinstance(value, str) or not value.strip():
        return None
    return alias_for(value, aliases) or value.strip()


# ----- models -----

# Runtimes that cannot read a Claude short name (opus, sonnet) and so need an
# alias for it. Claude Code reads its own names; Cursor copies agent files as
# they are and has no default model key an adapter writes.
ALIASED_RUNTIMES = ("codex", "gemini", "opencode")
# Runtimes with a runtime-wide subagent model key an adapter writes. Gemini CLI
# and OpenCode set a subagent's model per agent only.
SUBAGENT_MODEL_RUNTIMES = ("claude", "codex")
_INHERITS = frozenset({"inherit", "default"})


def alias_for(value: Any, aliases: Any) -> Optional[str]:
    """The alias a runtime's map gives this model name, or None."""
    name = str(value or "").strip()
    alias = aliases.get(name) if name and isinstance(aliases, dict) else None
    return alias if isinstance(alias, str) and alias else None


def _unaliased(value: Any, aliases: Any) -> bool:
    """A Claude model name this runtime cannot read and its map does not translate."""
    name = str(value or "").strip()
    return bool(name) and name.lower() not in _INHERITS and is_claude_model(name) and alias_for(name, aliases) is None


def default_models(source: Path, runtime: str) -> Dict[str, Optional[str]]:
    """model and subagent_model as this runtime's adapter writes them, or None for each left out.

    A name is translated through the runtime's aliases; on a runtime in
    ALIASED_RUNTIMES a Claude name with no alias is left out (notes() names it).
    """
    block = runtime_settings(source, runtime)
    aliases = block.get("aliases")
    out: Dict[str, Optional[str]] = {}
    for key in ("model", "subagent_model"):
        value = block.get(key)
        if runtime in ALIASED_RUNTIMES and (str(value or "").strip().lower() in _INHERITS or _unaliased(value, aliases)):
            out[key] = None
        else:
            out[key] = translate_model(value, aliases)
    if runtime not in SUBAGENT_MODEL_RUNTIMES:
        out["subagent_model"] = None
    return out


def agent_aliases(source: Path, runtime: str) -> Dict[str, str]:
    """This runtime's alias map from the stage, for translating agent files."""
    aliases = runtime_settings(source, runtime).get("aliases")
    return {k: v for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(aliases, dict) else {}


def _model_notes(source: Path, runtime: str) -> List[str]:
    block = runtime_settings(source, runtime)
    aliases = block.get("aliases")
    lines: List[str] = []
    if runtime not in ALIASED_RUNTIMES:
        return lines
    where = f"runtime_settings.{runtime}.aliases in components.json"
    for key in ("model", "subagent_model"):
        value = block.get(key)
        if _unaliased(value, aliases):
            lines.append(f"runtime_settings.{runtime}.{key} {value} is a Claude model name with no alias in {where}; not written")
    if block.get("subagent_model") and runtime not in SUBAGENT_MODEL_RUNTIMES:
        lines.append(
            f"runtime_settings.{runtime}.subagent_model is not written: {runtime} has no runtime-wide subagent model key"
        )
    by_model: Dict[str, List[str]] = {}
    agents = source / "agents"
    for path in sorted(agents.glob("*.md")) if agents.is_dir() else []:
        try:
            meta, _body = read_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        value = meta.get("model")
        if _unaliased(value, aliases):
            by_model.setdefault(str(value).strip(), []).append(path.stem)
    for name, stems in sorted(by_model.items()):
        lines.append(f"model {name} (agents {', '.join(stems)}) has no alias in {where}; each of those agents inherits the session model")
    return lines


def notes(source: Path, runtime: str) -> List[str]:
    """Every selected component this runtime's adapter leaves out, and why."""
    _rendered, lines = selected_servers(source, runtime)
    if runtime not in PLUGINS_RENDERED:
        for key in sorted(selected_plugins(source, runtime)):
            lines.append(f"plugin {key} {_PLUGINS_NOT_RENDERED}")
    lines.extend(_model_notes(source, runtime))
    return lines


# ----- the ledger of what an adapter rendered -----


def _read_ledger(target: Path, source: Optional[Path] = None) -> List[str]:
    path = target / ledger_name(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = data.get("mcp_servers") if isinstance(data, dict) else None
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _write_ledger(target: Path, names: List[str], dry_run: bool, source: Optional[Path] = None) -> None:
    if dry_run or sorted(names) == sorted(_read_ledger(target, source)):
        return
    path = target / ledger_name(source)
    if not names:
        if path.exists():
            path.unlink()
        return
    target.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcp_servers": sorted(names)}, indent=2) + "\n", encoding="utf-8")


# ----- one renderer per runtime -----


def _merge_mapping(existing: Any, desired: Dict[str, Any], previous: List[str]) -> Dict[str, Any]:
    """existing with every previously rendered name dropped and desired set."""
    merged = dict(existing) if isinstance(existing, dict) else {}
    for name in previous:
        if name not in desired:
            merged.pop(name, None)
    merged.update(desired)
    return merged


def _render_json(
    path: Path, key: str, desired: Dict[str, Any], target: Path, dry_run: bool, *, jsonc: bool = False,
    source: Optional[Path] = None,
) -> List[str]:
    previous = _read_ledger(target, source)
    if not desired and not previous:
        return []
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    document = json.loads(strip_jsonc(text) if jsonc else text) if text.strip() else {}
    merged = _merge_mapping(document.get(key), desired, previous)
    changed = merged != (document.get(key) or {})
    if changed:
        updated = dict(document)
        if merged:
            updated[key] = merged
        else:
            updated.pop(key, None)
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    _write_ledger(target, list(desired), dry_run, source)
    return [f"render components.json mcp servers into {path.name}"] if changed else []


# A TOML table or array-of-tables header line: optional indentation, the
# bracketed key, then optional whitespace and a comment (TOML 1.0, "Table").
_HEADER = re.compile(r"^[ \t]*(\[\[?)(?P<key>[^\[\]\n]*?)(\]\]?)[ \t]*(?:#.*)?$")
_KEY_PART = re.compile(r"""[ \t]*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([A-Za-z0-9_-]+))[ \t]*(?:\.|$)""")


def _header_path(line: str) -> Optional[Tuple[str, ...]]:
    """The dotted key a table header line names, or None for any other line."""
    match = _HEADER.match(line)
    if not match:
        return None
    key = match.group("key")
    parts: List[str] = []
    position = 0
    while position < len(key):
        part = _KEY_PART.match(key, position)
        if not part or part.end() == position:
            return ()  # a header, but one this reader cannot split: never managed
        if part.group(1) is not None:
            parts.append(json.loads(f'"{part.group(1)}"'))
        else:
            parts.append(part.group(2) if part.group(2) is not None else part.group(3))
        position = part.end()
    return tuple(parts)


_KEY_TOKEN = re.compile(r"""[ \t]*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([A-Za-z0-9_-]+))[ \t]*""")


def _line_key_path(line: str) -> Tuple[str, ...]:
    """The dotted key a key = value line assigns, or () for any other line."""
    parts: List[str] = []
    position = 0
    while True:
        token = _KEY_TOKEN.match(line, position)
        if not token or token.end() == position:
            return ()
        if token.group(1) is not None:
            parts.append(json.loads(f'"{token.group(1)}"'))
        else:
            parts.append(token.group(2) if token.group(2) is not None else token.group(3))
        position = token.end()
        if line.startswith(".", position):
            position += 1
            continue
        return tuple(parts) if line.startswith("=", position) else ()


def _remove_codex_server(text: str, name: str) -> str:
    """text without any definition of mcp_servers.<name>.

    Line based rather than one pattern, so every valid header spelling is
    recognized at both ends of a block: indentation, spaces around the dots,
    quoted keys and a trailing comment (Codex review 1 of PR 92). A block
    runs from its header to the next header line of any kind. A key line
    that defines the server inline or by dotted key, under [mcp_servers] or
    at the root, is removed too (Codex review 2 of PR 92). A value that
    continues past its line is not followed; render_codex verifies the
    result and refuses rather than write a file that shape would break.
    """
    out: List[str] = []
    skipping = False
    table: Tuple[str, ...] = ()
    target = ("mcp_servers", name)
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        path = _header_path(bare)
        if path is not None:
            table = path
            skipping = path[:2] == target
        elif not skipping and (table + _line_key_path(bare))[:2] == target and _line_key_path(bare):
            continue
        if not skipping:
            out.append(line)
    return "".join(out)


def _without_managed(document: Dict[str, Any], managed: set) -> Dict[str, Any]:
    """document with the managed mcp_servers entries taken out."""
    out = dict(document)
    servers = out.get("mcp_servers")
    if isinstance(servers, dict):
        kept = {name: value for name, value in servers.items() if name not in managed}
        if kept:
            out["mcp_servers"] = kept
        else:
            out.pop("mcp_servers")
    return out


def _verify_codex(before: str, after: str, managed: set, desired: Dict[str, Any]) -> None:
    """Raise RenderRefused unless after differs from before only in the managed servers.

    The text edits are line based, so a header-shaped line inside a
    multi-line string, or a value continued past its line, could otherwise
    be cut and still parse (Codex reviews 2 and 3 of PR 92). Comparing the
    parsed documents catches every such case, whatever its shape.
    """
    names = ", ".join(f"mcp_servers.{name}" for name in sorted(managed))
    try:
        old = tomllib.loads(before) if before.strip() else {}
    except tomllib.TOMLDecodeError as error:
        raise RenderRefused(f"config.toml does not parse ({error}); fix it by hand before {names} can be rendered") from None
    try:
        new = tomllib.loads(after)
    except tomllib.TOMLDecodeError as error:
        raise RenderRefused(
            f"config.toml would not parse after rendering {names} ({type(error).__name__}); edit their definitions by hand"
        ) from None
    servers = new.get("mcp_servers") if isinstance(new.get("mcp_servers"), dict) else {}
    wrong = sorted(name for name in managed if servers.get(name) != desired.get(name))
    if wrong:
        raise RenderRefused(
            "config.toml defines "
            + ", ".join(f"mcp_servers.{name}" for name in wrong)
            + " in a shape this adapter cannot replace; edit it by hand"
        )
    if _without_managed(old, managed) != _without_managed(new, managed):
        raise RenderRefused(
            f"rendering {names} would change config.toml outside those servers (a header-shaped line inside a "
            "multi-line string, or a value spanning lines); edit their definitions by hand"
        )


def _codex_table(name: str, server: Dict[str, Any]) -> str:
    return f"[mcp_servers.{name}]\n" + "".join(f"{key} = {toml_value(value)}\n" for key, value in server.items())


def render_codex(source: Path, target: Path, dry_run: bool) -> List[str]:
    """[mcp_servers.<name>] tables in config.toml.

    A table whose parsed value already equals the desired one is left where
    it is, so the adapter's managed permissions block stays last and a
    second sync is current. Any other copy of a managed name, including the
    sub-tables Codex writes when it re-serializes the file, is removed
    before the new table is appended.
    """
    servers, _notes = selected_servers(source, "codex")
    desired = {name: codex_server(entry) for name, entry in servers.items()}
    previous = _read_ledger(target, source)
    if not desired and not previous:
        return []
    path = target / "config.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    current: Dict[str, Any] = {}
    if tomllib is not None and text:
        try:
            current = tomllib.loads(text).get("mcp_servers") or {}
        except (tomllib.TOMLDecodeError, AttributeError):
            current = {}
    updated = text
    for name in sorted(set(previous) | set(desired)):
        if name in desired and current.get(name) == desired[name]:
            continue
        updated = _remove_codex_server(updated, name)
        if name in desired:
            if updated and not updated.endswith("\n"):
                updated += "\n"
            updated += ("\n" if updated else "") + _codex_table(name, desired[name])
    changed = updated != text
    if changed:
        if tomllib is None:
            # Without a parser the edit cannot be verified, and an unverified
            # line edit can leave Codex a file it cannot load (Codex review 4
            # of PR 92).
            raise RenderRefused(
                "rewriting config.toml mcp_servers needs Python 3.11 or later (tomllib) to verify the result; "
                "run sync.py with a newer python3"
            )
        _verify_codex(text, updated, set(previous) | set(desired), desired)
    if changed and not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    _write_ledger(target, list(desired), dry_run, source)
    return ["render components.json mcp servers into config.toml"] if changed else []


def render_gemini(source: Path, target: Path, dry_run: bool) -> List[str]:
    servers, _notes = selected_servers(source, "gemini")
    desired = {name: gemini_server(entry) for name, entry in servers.items()}
    return _render_json(target / "settings.json", "mcpServers", desired, target, dry_run, source=source)


def render_opencode(source: Path, target: Path, dry_run: bool) -> List[str]:
    servers, _notes = selected_servers(source, "opencode")
    desired = {name: opencode_server(entry, True) for name, entry in servers.items()}
    return _render_json(target / "opencode.jsonc", "mcp", desired, target, dry_run, jsonc=True, source=source)


def render(runtime: str, source: Path, target: Path, dry_run: bool) -> List[str]:
    """The actions rendering this runtime's selected MCP servers takes."""
    renderer: Optional[Any] = {"codex": render_codex, "gemini": render_gemini, "opencode": render_opencode}.get(runtime)
    return renderer(source, target, dry_run) if renderer else []
