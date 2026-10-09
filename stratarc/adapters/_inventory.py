"""The external inventory: what each runtime and the machine hold that
the shared config did not write, and how each item stands against components.json.

The design is each-adapter-declares-where-foreign-content-lives. Each adapter
module exposes external_inventory(target) beside owned_outputs, built from
the readers here, and returns one Item per installed external component:
kind, name, the file it was read from, and the environment and header
values it carries (read so the check can scan them for literal secrets,
never printed). A location that exists and cannot be read is an Item with
an error, reported as unreadable, never as empty; a location that does not
exist holds nothing and yields nothing.

classify() joins one Item to the manifest and returns declared, undeclared,
returned (present although declared wanted: false) or unreadable. The
reverse pass in the sync turns those into findings; returned and a
literal secret fail sync.py --check (a-returned-component-fails-the-check,
the-check-reads-secrets-by-location-only).

Two rows of the record's table are unconfirmed and ship as unreadable until
live checks confirm where they can be read: Claude Code account
connectors and Cursor plugins.
"""

from __future__ import annotations

import dataclasses
import json
import plistlib
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.parsers.expat import ExpatError

try:  # Python 3.11 and later
    import tomllib
except ModuleNotFoundError:  # pragma: no cover, older interpreters only
    tomllib = None  # type: ignore[assignment]


# Keys under an MCP server entry whose string values reach the server's
# process or its requests, and so may carry a credential. Every runtime's
# spelling: env (Claude Code, Codex, Gemini CLI, Cursor), environment
# (OpenCode), headers (Claude Code, Gemini CLI, Cursor, OpenCode) and
# http_headers (Codex).
SECRET_BEARING_KEYS = ("env", "environment", "headers", "http_headers")

# Manifest section each inventoried kind is declared in.
SECTION_FOR_KIND = {
    "mcp_server": "mcp_servers",
    "connector": "mcp_servers",
    "plugin": "plugins",
    "extension": "plugins",
    "service": "services",
    "dependency": "dependencies",
    "skill": "third_party_skills",
}


@dataclasses.dataclass(frozen=True)
class Item:
    """One installed external component, or one location that could not be read.

    location is the file or directory it was read from. env holds (key path,
    value) pairs to scan for literal secrets; the value is never printed.
    match_text is what a manifest entry's match substring is tested against
    (a service's label and program). present is False only for a declared
    dependency whose command is not on PATH. error, when set, makes the item
    unreadable and name is the location's kind label.
    """

    runtime: str
    kind: str
    name: str
    location: str
    env: Tuple[Tuple[str, str], ...] = ()
    match_text: str = ""
    present: bool = True
    error: Optional[str] = None


def unreadable(runtime: str, kind: str, location: Path | str, reason: str) -> Item:
    return Item(runtime, kind, "", str(location), error=reason)


# ---------------------------------------------------------------------------
# Readers. Each returns (data, error): (None, None) for a missing file, which
# holds nothing; (None, message) for a file that exists and cannot be read.
# ---------------------------------------------------------------------------


def read_json(path: Path) -> Tuple[Any, Optional[str]]:
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as error:
        return None, f"cannot be read as JSON: {type(error).__name__}"


def strip_jsonc(text: str) -> str:
    """Drop // line comments and /* */ block comments outside strings, then
    trailing commas. A block comment is replaced by nothing and its line
    breaks are kept, so a parse error still names the right line."""
    out: List[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            comment = text[i : n if end == -1 else end + 2]
            out.append("\n" * comment.count("\n"))
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return _drop_trailing_commas("".join(out))


_CLOSING_NEXT = re.compile(r"\s*[}\]]")


def _drop_trailing_commas(text: str) -> str:
    """Remove a comma followed only by whitespace and a closing bracket,
    outside strings only, so a value such as "first, ] last" is kept whole."""
    out: List[str] = []
    in_string = escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "," and _CLOSING_NEXT.match(text, i + 1):
            continue
        out.append(ch)
    return "".join(out)


def read_jsonc(path: Path) -> Tuple[Any, Optional[str]]:
    if not path.exists():
        return None, None
    try:
        return json.loads(strip_jsonc(path.read_text(encoding="utf-8"))), None
    except (OSError, ValueError) as error:
        return None, f"cannot be read as JSONC: {type(error).__name__}"


def read_toml(path: Path) -> Tuple[Any, Optional[str]]:
    if not path.exists():
        return None, None
    if tomllib is None:
        return None, "needs Python 3.11 or later for tomllib"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as error:
        return None, f"cannot be read as TOML: {type(error).__name__}"


def read_plist(path: Path) -> Tuple[Any, Optional[str]]:
    if not path.exists():
        return None, None
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle), None
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
        # plistlib raises ExpatError, not InvalidFileException, for
        # truncated or malformed XML (Codex review of PR 85).
        return None, f"cannot be read as a property list: {type(error).__name__}"


def list_dir(path: Path, *, dirs_only: bool = False, skip: Iterable[str] = ()) -> Tuple[List[str], Optional[str]]:
    """Entry names under path, hidden names and skip left out."""
    if not path.exists():
        return [], None
    try:
        entries = sorted(path.iterdir())
    except OSError as error:
        return [], f"cannot be listed: {type(error).__name__}"
    skipped = set(skip)
    return [
        p.name
        for p in entries
        if not p.name.startswith(".") and p.name not in skipped and (p.is_dir() or not dirs_only)
    ], None


# ---------------------------------------------------------------------------
# Builders shared by the adapters.
# ---------------------------------------------------------------------------


def secret_pairs(prefix: str, entry: Any) -> Tuple[Tuple[str, str], ...]:
    """(key path, value) for every string an entry carries under a secret-bearing key."""
    if not isinstance(entry, dict):
        return ()
    pairs: List[Tuple[str, str]] = []
    for key in SECRET_BEARING_KEYS:
        values = entry.get(key)
        if isinstance(values, dict):
            for name, value in sorted(values.items()):
                if isinstance(value, str):
                    pairs.append((f"{prefix}.{key}.{name}", value))
    return tuple(pairs)


def mcp_items(runtime: str, servers: Any, location: Path, prefix: str) -> List[Item]:
    """One mcp_server Item per entry of a name-to-definition mapping."""
    if servers is None:
        return []
    if not isinstance(servers, dict):
        return [unreadable(runtime, "mcp_server", location, f"{prefix} is not an object")]
    return [
        Item(runtime, "mcp_server", str(name), str(location), env=secret_pairs(f"{prefix}.{name}", entry))
        for name, entry in sorted(servers.items())
    ]


def mcp_from_json_file(runtime: str, path: Path, key: str, *, jsonc: bool = False) -> List[Item]:
    data, error = (read_jsonc if jsonc else read_json)(path)
    if error:
        return [unreadable(runtime, "mcp_server", path, error)]
    if not isinstance(data, dict):
        return [] if data is None else [unreadable(runtime, "mcp_server", path, "is not an object")]
    return mcp_items(runtime, data.get(key), path, key)


# ---------------------------------------------------------------------------
# The machine rows: services under ~/Library/LaunchAgents and declared
# command-line dependencies.
# ---------------------------------------------------------------------------


def launch_agent_items(home: Path) -> List[Item]:
    """One service Item per property list under ~/Library/LaunchAgents."""
    directory = home / "Library" / "LaunchAgents"
    names, error = list_dir(directory)
    if error:
        return [unreadable("machine", "service", directory, error)]
    items: List[Item] = []
    for name in names:
        if not name.endswith(".plist"):
            continue
        path = directory / name
        data, error = read_plist(path)
        if error:
            items.append(unreadable("machine", "service", path, error))
            continue
        data = data if isinstance(data, dict) else {}
        label = data.get("Label") if isinstance(data.get("Label"), str) else path.stem
        program = data.get("Program")
        arguments = data.get("ProgramArguments")
        if not isinstance(program, str) and isinstance(arguments, list) and arguments:
            program = arguments[0]
        env = data.get("EnvironmentVariables")
        if env is not None and not isinstance(env, dict):
            # A shape launchd would not read; unreadable, never a crash
            # (second Codex review of PR 85).
            items.append(unreadable("machine", "service", path, "EnvironmentVariables is not a dictionary"))
            continue
        pairs = tuple(
            (f"EnvironmentVariables.{key}", value)
            for key, value in sorted((env or {}).items())
            if isinstance(value, str)
        )
        items.append(
            Item(
                "machine",
                "service",
                label,
                str(path),
                env=pairs,
                match_text=" ".join(t for t in (label, program if isinstance(program, str) else "") if t),
            )
        )
    return items


def dependency_items(document: Dict[str, Any], path: Optional[str] = None, host_platform: Optional[str] = None) -> List[Item]:
    """One dependency Item per declared dependency, present or not.

    A dependency whose platform key (components.py PLATFORMS) names a
    platform other than this one is skipped entirely: dbus-daemon and
    gnome-keyring, container-only (Linux) additions for the gemini runtime's
    keyring persistence, are never expected on the Mac's own PATH, and
    reporting them missing there would be noise about a tool the Mac does
    not need. host_platform defaults to sys.platform ("darwin" or "linux",
    the same spelling components.json's platform key uses) and is a
    parameter only so a test can hold it fixed regardless of the host it
    runs on.
    """
    if host_platform is None:
        host_platform = sys.platform
    items: List[Item] = []
    for entry in document.get("dependencies") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            continue
        entry_platform = entry.get("platform")
        if isinstance(entry_platform, str) and entry_platform != host_platform:
            continue
        found = shutil.which(entry["command"], path=path)
        items.append(
            Item(
                "machine",
                "dependency",
                str(entry.get("name") or entry["command"]),
                found or entry["command"],
                present=found is not None,
            )
        )
    return items


def machine_inventory(home: Path, document: Dict[str, Any], path: Optional[str] = None, host_platform: Optional[str] = None) -> List[Item]:
    return launch_agent_items(home) + dependency_items(document, path, host_platform)


# ---------------------------------------------------------------------------
# Classification against components.json.
# ---------------------------------------------------------------------------


def _entries(document: Dict[str, Any], section: str, runtime: str) -> List[Dict[str, Any]]:
    return [
        entry
        for entry in document.get(section) or []
        if isinstance(entry, dict)
        and (runtime == "machine" or runtime in (entry.get("runtimes") or []))
    ]


def _plugin_names(entry: Dict[str, Any]) -> set:
    names = {entry.get("name")}
    if isinstance(entry.get("marketplace"), str):
        names.add(f"{entry.get('name')}@{entry['marketplace']}")
    if isinstance(entry.get("path"), str):
        names.update({entry["path"], Path(entry["path"]).name})
    return {n for n in names if isinstance(n, str) and n}


def match_entry(document: Dict[str, Any], item: Item) -> Optional[Dict[str, Any]]:
    """The manifest entry that declares this item, or None."""
    section = SECTION_FOR_KIND.get(item.kind)
    if section is None:
        return None
    if section == "third_party_skills":
        # skill_entry() already implements the "name or skills list" match a
        # foreign skill copy needs: one installer can create several
        # directories under one entry (one installer creates ten), so a foreign
        # copy under one of those names is declared by that entry too, not
        # only by the entry's own name (#241 correctness follow-up). Reused
        # here rather than duplicated.
        return skill_entry(document, item.runtime, item.name)
    for entry in _entries(document, section, item.runtime):
        if section == "plugins":
            if item.name in _plugin_names(entry):
                return entry
        elif section == "services":
            if item.name == entry.get("label") or item.name == entry.get("name"):
                return entry
            match = entry.get("match")
            matches = match if isinstance(match, list) else [match]
            if any(isinstance(value, str) and value and value in item.match_text for value in matches):
                return entry
        elif item.name == entry.get("name"):
            return entry
    return None


def classify(document: Dict[str, Any], item: Item) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(state, entry): declared, undeclared, returned, unreadable, missing or absent.

    missing is a wanted dependency whose command is not on PATH; absent is an
    unwanted one that is gone, which is the state the operator asked for.
    """
    if item.error:
        return "unreadable", None
    if item.kind == "marketplace":
        named = any(
            entry.get("marketplace") == item.name for entry in _entries(document, "plugins", item.runtime)
        )
        return ("declared" if named else "undeclared"), None
    entry = match_entry(document, item)
    if entry is None:
        return "undeclared", None
    wanted = entry.get("wanted") is True
    if not item.present:
        return ("missing" if wanted else "absent"), entry
    return ("declared" if wanted else "returned"), entry


def skill_entry(document: Dict[str, Any], runtime: str, name: str) -> Optional[Dict[str, Any]]:
    """The third_party_skills entry naming this skill directory for this runtime.

    An entry whose own name is the directory matches, and so does one whose
    skills list names it: a single installer may create several directories
    (one installer creates ten), and one
    entry per directory would declare the same tool ten times.
    """
    for entry in _entries(document, "third_party_skills", runtime):
        if entry.get("name") == name:
            return entry
        skills = entry.get("skills")
        if isinstance(skills, list) and name in skills:
            return entry
    return None


def hook_entry(document: Dict[str, Any], runtime: str, command: str) -> Optional[Dict[str, Any]]:
    """The foreign_hooks entry whose match is a substring of this command."""
    for entry in _entries(document, "foreign_hooks", runtime):
        match = entry.get("match")
        if isinstance(match, str) and match and match in command:
            return entry
    return None
