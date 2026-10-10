"""Resolves a setting through the layers and records where every value came from.

Precedence, lowest to highest (see `developer-docs/explanation/cli-design.md`):

1. base: the source root's shared files
2. runtime: `runtimes/<runtime>.toml` or `.json`
3. account: `accounts/<account>.toml` or `.json`
4. project: the project's own files under `projects-root/<project>/`
5. agent: `agents/<agent>.*` in the source root, then `projects-root/<project>/agents/<agent>.*`
6. env: `STRATARC_<KEY>` variables, with `.` written as `__` (`permissions.allow` is `STRATARC_PERMISSIONS__ALLOW`)
7. flags: values passed by the caller of `resolve`

Every file contributes dotted keys. A file kind (`FileKind`) says which layer a file belongs to, where it lives and which key namespace its top level maps to: `permissions.json` is `permissions.*`, `stratarc.toml` is `settings.*`, and an overlay file with no namespace names its namespaces itself (`[permissions]` in `runtimes/codex.toml`). Adding a source file kind is one `register_file_kind` call.

A scalar is replaced by a higher layer and a table merges key by key. A list that a higher layer redefines needs an explicit mode, or resolution fails with `list-mode-missing`. The mode is written in the file, in a `_modes` table beside the list:

    "allow": ["Read(*)"],
    "_modes": {"allow": "extend"}

`replace` swaps the list and `extend` appends to it. The first layer that defines a list needs no mode.

An agent file may name a `parent` (a sub-agent started by another agent) and a `relay` table saying which keys the child takes from the parent's settings:

    "parent": "lead",
    "relay": {"inherit": ["permissions.*"]}

`inherit` is a list of dotted-key patterns (`*` matches any run of characters, dots included). The default is none: nothing is inherited implicitly. Inherited values enter the agent layer below the child's own, so the child's file wins. The chain is followed to its root (a parent's own relay applies to what it passes on), a cycle is an error, and the chain holds at most `RELAY_DEPTH_LIMIT` agents. An agent file may also set `account`; the child's account wins over its parent's, and a given account (`--account`) wins over both. `parent`, `relay` and `account` are read by the resolver and are not settings.

Values passed by the caller of `load` as `flags` form the last layer, with provenance file `--set` and line 0. A flag list needs a mode (`flag_modes`) like any layer that redefines a list.

Every step of a resolution carries its layer, file, line, operation (`set`, `merge`, `replace`, `extend`), the earlier values it overrode and, for a relayed value, the parent that passed it down. The module only reads; it never writes.
"""

from __future__ import annotations

import bisect
import fnmatch
import json
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from stratarc.paths import home

LAYERS = ("base", "runtime", "account", "project", "agent", "env", "flags")
MODES = ("replace", "extend")
MODES_KEY = "_modes"
ENV_PREFIX = "STRATARC_"
RELAY_DEPTH_LIMIT = 8
RELAY_KEYS = ("parent", "relay", "account")
FLAGS_LABEL = "--set"


class LayerError(Exception):
    """A layer file or a resolution cannot be used.

    `code` is stable (`parse-error`, `list-mode-missing`, `mode-invalid`, `type-mismatch`, `unknown-key`, `unknown-project`, `unknown-agent`, `unknown-account`, `unknown-runtime`, `relay-invalid`, `relay-cycle`, `relay-depth`, `flag-invalid`); `hint` says how to recover.
    """

    def __init__(self, code: str, message: str, *, file: Path | None = None, line: int | None = None, key: str | None = None, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.file = file
        self.line = line
        self.key = key
        self.hint = hint


# ---- parsing with line numbers --------------------------------------------------------------

Path_ = tuple[str, ...]


def _line_table(text: str) -> Callable[[int], int]:
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    return lambda pos: bisect.bisect_right(starts, pos)


def _json_lines(text: str) -> dict[Path_, int]:
    """Map the path of every object key to its line, by scanning the text of an already valid JSON file."""
    decoder = json.JSONDecoder()
    line_of = _line_table(text)
    lines: dict[Path_, int] = {}

    def skip(pos: int) -> int:
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        return pos

    def value(pos: int, path: Path_) -> int:
        pos = skip(pos)
        if text[pos] != "{":
            _, end = decoder.raw_decode(text, pos)
            return end
        pos = skip(pos + 1)
        while text[pos] != "}":
            key, after = decoder.raw_decode(text, pos)
            lines[path + (key,)] = line_of(pos)
            pos = skip(after)  # the colon
            pos = skip(value(skip(pos + 1), path + (key,)))
            if text[pos] == ",":
                pos = skip(pos + 1)
        return pos + 1

    value(0, ())
    return lines


def _split_toml_key(raw: str) -> Path_:
    parts: list[str] = []
    current = ""
    quote = ""
    for ch in raw.strip():
        if quote:
            if ch == quote:
                quote = ""
            else:
                current += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    return tuple(parts)


def _toml_lines(text: str) -> dict[Path_, int]:
    """Map the path of every key and table to its line, by scanning the text of an already valid TOML file."""
    lines: dict[Path_, int] = {}
    table: Path_ = ()
    depth = 0
    in_block = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if in_block:
            if in_block in stripped:
                in_block = ""
            continue
        if depth > 0:
            depth += _bracket_delta(stripped)
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            name = stripped.lstrip("[").split("]", 1)[0]
            table = _split_toml_key(name)
            lines.setdefault(table, number)
            continue
        key_text, sep, rest = stripped.partition("=")
        if not sep:
            continue
        path = table + _split_toml_key(key_text)
        for size in range(len(table) + 1, len(path) + 1):
            lines.setdefault(path[:size], number)
        rest = rest.strip()
        for marker in ('"""', "'''"):
            if rest.startswith(marker) and rest.count(marker) < 2:
                in_block = marker
        if not in_block:
            depth = max(0, _bracket_delta(rest))
    return lines


def _bracket_delta(text: str) -> int:
    delta = 0
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        elif ch in "[{":
            delta += 1
        elif ch in "]}":
            delta -= 1
    return delta


def parse_json(path: Path) -> tuple[dict[str, Any], dict[Path_, int]]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LayerError("parse-error", f"{exc.msg}", file=path, line=exc.lineno, hint="Fix the JSON syntax in that file.") from exc
    if not isinstance(data, dict):
        raise LayerError("parse-error", "the top level must be an object", file=path, line=1, hint="Wrap the settings in a JSON object.")
    return data, _json_lines(text)


def parse_toml(path: Path) -> tuple[dict[str, Any], dict[Path_, int]]:
    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        match = re.search(r"line (\d+)", str(exc))
        raise LayerError("parse-error", str(exc), file=path, line=int(match.group(1)) if match else None, hint="Fix the TOML syntax in that file.") from exc
    return data, _toml_lines(text)


PARSERS: dict[str, Callable[[Path], tuple[dict[str, Any], dict[Path_, int]]]] = {".json": parse_json, ".toml": parse_toml}


# ---- the registry of file kinds -------------------------------------------------------------


@dataclass(frozen=True)
class FileKind:
    """One kind of source file.

    `template` is a path under the source root. Its placeholders (`{project}`, `{agent}`, `{account}`, `{runtime}`) are filled from the request; a kind whose placeholder has no value is skipped. `namespace` prefixes every key from the file, or is empty when the file's top-level tables name their own namespaces.
    """

    layer: str
    template: str
    namespace: str = ""


FILE_KINDS: list[FileKind] = []


def register_file_kind(kind: FileKind) -> FileKind:
    """Add a source file kind; the resolver, `list` and `explain` pick it up with no other change."""
    if kind.layer not in LAYERS:
        raise ValueError(f"unknown layer {kind.layer!r}")
    if Path(kind.template).suffix not in PARSERS:
        raise ValueError(f"no parser for {kind.template!r}")
    FILE_KINDS.append(kind)
    return kind


for _suffix in (".toml", ".json"):
    register_file_kind(FileKind("runtime", f"runtimes/{{runtime}}{_suffix}"))
    register_file_kind(FileKind("account", f"accounts/{{account}}{_suffix}"))
    register_file_kind(FileKind("agent", f"agents/{{agent}}{_suffix}"))
    register_file_kind(FileKind("agent", f"projects-root/{{project}}/agents/{{agent}}{_suffix}"))
register_file_kind(FileKind("base", "permissions.json", "permissions"))
register_file_kind(FileKind("base", "stratarc.toml", "settings"))
register_file_kind(FileKind("project", "projects-root/{project}/permissions.json", "permissions"))
register_file_kind(FileKind("project", "projects-root/{project}/stratarc.toml", "settings"))


# ---- entries and sources --------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    value: Any
    line: int | None
    mode: str | None = None


@dataclass
class Source:
    layer: str
    file: Path | None
    label: str
    entries: dict[str, Entry] = field(default_factory=dict)
    via: str | None = None  # for a relayed source, the parent that passed it down


def _line_for(lines: Mapping[Path_, int], path: Path_) -> int | None:
    for size in range(len(path), 0, -1):
        if path[:size] in lines:
            return lines[path[:size]]
    return None


def flatten(data: Mapping[str, Any], lines: Mapping[Path_, int], file: Path, namespace: str = "") -> dict[str, Entry]:
    """Turn parsed file data into dotted-key entries; tables recurse, lists and empty tables are leaves."""
    out: dict[str, Entry] = {}

    def walk(table: Mapping[str, Any], path: Path_, prefix: str) -> None:
        modes = table.get(MODES_KEY, {})
        if not isinstance(modes, dict):
            raise LayerError("mode-invalid", f"{MODES_KEY} must be a table of key to mode", file=file, line=_line_for(lines, path + (MODES_KEY,)), hint='Write it like "_modes": {"allow": "extend"}.')
        for name, mode in modes.items():
            line = _line_for(lines, path + (MODES_KEY, name))
            if mode not in MODES:
                raise LayerError("mode-invalid", f'mode "{mode}" for "{prefix}{name}" is not one of: {", ".join(MODES)}', file=file, line=line, key=prefix + name, hint="Use replace or extend.")
            if not isinstance(table.get(name), list):
                raise LayerError("mode-invalid", f'"{prefix}{name}" has a mode but is not a list', file=file, line=line, key=prefix + name, hint="Remove the mode, or make the value a list.")
        for name, child in table.items():
            if name == MODES_KEY:
                continue
            child_path = path + (name,)
            if isinstance(child, dict) and child:
                walk(child, child_path, f"{prefix}{name}.")
            else:
                out[prefix + name] = Entry(child, _line_for(lines, child_path), modes.get(name))

    walk(dict(data), (), f"{namespace}." if namespace else "")
    return out


def _find_named(root: Path, kinds: list[FileKind], layer: str, field_name: str, value: str) -> list[Path]:
    return [p for k in kinds if k.layer == layer and "{" + field_name + "}" in k.template for p in [root / k.template.format(**{field_name: value})] if p.is_file()]


@dataclass(frozen=True)
class Request:
    """Which layers to read: the source root plus the optional project, agent, account and runtime."""

    root: Path
    project: str | None = None
    agent: str | None = None
    account: str | None = None
    runtime: str | None = None


def _validate(request: Request, kinds: list[FileKind]) -> None:
    root = request.root
    if request.project and not (root / "projects-root" / request.project).is_dir():
        raise LayerError("unknown-project", f'The project "{request.project}" has no folder under projects-root.', hint="Run `stratarc projects` to list the projects, or check the spelling.")
    if request.runtime and not _find_named(root, kinds, "runtime", "runtime", request.runtime):
        from stratarc.config import ConfigError, load_config

        try:
            known = load_config(root).runtimes
        except ConfigError:
            known = {}
        if request.runtime not in known:
            raise LayerError("unknown-runtime", f'The runtime "{request.runtime}" is not in stratarc.toml and has no runtimes/ file.', hint="Use a runtime named under [runtimes] in stratarc.toml.")


# ---- the dispatch relay ---------------------------------------------------------------------


@dataclass
class _Agent:
    """One agent's files: its settings (without the relay keys) and what it declares about its parent and account."""

    name: str
    found: bool = False
    sources: list[Source] = field(default_factory=list)
    decl: dict[str, tuple[Any, Path, int | None]] = field(default_factory=dict)

    @property
    def parent(self) -> str | None:
        return self.decl["parent"][0] if "parent" in self.decl else None

    @property
    def inherit(self) -> tuple[str, ...]:
        relay = self.decl["relay"][0] if "relay" in self.decl else {}
        return tuple(relay.get("inherit", ()))

    @property
    def account(self) -> str | None:
        return self.decl["account"][0] if "account" in self.decl else None


@dataclass(frozen=True)
class RelayLink:
    """One child to parent edge of a chain: what the child's `relay.inherit` took from the parent and what it left behind, with the reason."""

    agent: str
    parent: str
    inherit: tuple[str, ...]
    file: Path | None
    line: int | None
    taken: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]  # (key, reason)


@dataclass(frozen=True)
class Relay:
    """The relay of a request: its links (child first) and where the account came from (`--account`, `agent`, `parent` or empty)."""

    links: list[RelayLink] = field(default_factory=list)
    account: str | None = None
    account_from: str = ""
    account_agent: str | None = None


@dataclass(frozen=True)
class RelayEdge:
    agent: str
    parent: str
    inherit: tuple[str, ...]
    file: Path
    line: int | None


def _check_declarations(agent: _Agent) -> None:
    def bad(key: str, message: str, hint: str) -> LayerError:
        _, file, line = agent.decl[key]
        return LayerError("relay-invalid", message, file=file, line=line, key=key, hint=hint)

    if "parent" in agent.decl and not (isinstance(agent.parent, str) and agent.parent):
        raise bad("parent", '"parent" must be the name of an agent', 'Write "parent": "<agent>".')
    if "account" in agent.decl and not (isinstance(agent.account, str) and agent.account):
        raise bad("account", '"account" must be the name of an account', 'Write "account": "<name>".')
    if "relay" in agent.decl:
        value = agent.decl["relay"][0]
        if "parent" not in agent.decl:
            raise bad("relay", '"relay" is set but there is no "parent" to relay from', 'Add "parent": "<agent>", or remove the relay table.')
        if not isinstance(value, dict):
            raise bad("relay", '"relay" must be a table', 'Write "relay": {"inherit": ["permissions.*"]}.')
        for name in value:
            if name != "inherit":
                raise bad("relay", f'"relay" has an unknown key "{name}"', 'The only key is "inherit".')
        inherit = value.get("inherit", [])
        if not isinstance(inherit, list) or not all(isinstance(p, str) and p for p in inherit):
            raise bad("relay", '"relay.inherit" must be a list of key patterns', 'Write "inherit": ["permissions.*"].')


def _read_agent(request: Request, kinds: list[FileKind], name: str) -> _Agent:
    values = {"project": request.project, "agent": name, "account": request.account, "runtime": request.runtime}
    agent = _Agent(name)
    for kind in kinds:
        if kind.layer != "agent" or any(not values.get(n) for n in re.findall(r"\{(\w+)\}", kind.template)):
            continue
        path = request.root / kind.template.format(**values)
        if not path.is_file():
            continue
        agent.found = True
        data, lines = PARSERS[path.suffix](path)
        data = dict(data)
        for key in RELAY_KEYS:
            if key in data:
                agent.decl[key] = (data.pop(key), path, _line_for(lines, (key,)))
        agent.sources.append(Source("agent", path, kind.template.format(**values), flatten(data, lines, path, kind.namespace)))
    _check_declarations(agent)
    return agent


def _agent_chain(request: Request, kinds: list[FileKind]) -> list[_Agent]:
    """The requested agent and its parents, child first. A cycle and a chain past `RELAY_DEPTH_LIMIT` agents are errors."""
    chain: list[_Agent] = []
    names: list[str] = []
    name: str | None = request.agent
    declared_by: _Agent | None = None
    while name:
        _, file, line = declared_by.decl["parent"] if declared_by else (None, None, None)
        if name in names:
            raise LayerError("relay-cycle", f"agent relay cycle: {' -> '.join([*names, name])}", file=file, line=line, key="parent", hint='Remove one "parent" so the chain ends at an agent with no parent.')
        if len(names) >= RELAY_DEPTH_LIMIT:
            raise LayerError("relay-depth", f"the relay chain of {request.agent} is longer than {RELAY_DEPTH_LIMIT} agents: {' -> '.join([*names, name])}", file=file, line=line, key="parent", hint="Shorten the chain, or flatten it by giving the child the settings directly.")
        agent = _read_agent(request, kinds, name)
        if not agent.found:
            if declared_by is None:
                raise LayerError("unknown-agent", f'The agent "{name}" has no file under agents/.', hint="Check the spelling, or pass --project when the agent belongs to a project.")
            raise LayerError("unknown-agent", f'The agent "{name}", named as the parent of "{declared_by.name}", has no file under agents/.', file=file, line=line, key="parent", hint="Check the spelling of the parent, or create its agent file.")
        names.append(name)
        chain.append(agent)
        declared_by, name = agent, agent.parent
    return chain


def _relay_sources(chain: list[_Agent]) -> tuple[list[Source], list[RelayLink]]:
    """The agent-layer sources for a chain (the root ancestor's first) and the links, child first.

    Walking from the root down, each child takes from the parent's effective settings only the keys its `relay.inherit` names.
    """
    effective = list(chain[-1].sources)
    links: list[RelayLink] = []
    for index in range(len(chain) - 2, -1, -1):
        child, parent = chain[index], chain[index + 1]
        patterns = child.inherit
        reason = f"no relay.inherit pattern matches (inherit: {', '.join(patterns)})" if patterns else "relay.inherit is empty, so nothing is inherited"
        taken: set[str] = set()
        skipped: dict[str, str] = {}
        inherited: list[Source] = []
        for source in effective:
            kept: dict[str, Entry] = {}
            for key, entry in source.entries.items():
                if any(fnmatch.fnmatchcase(key, p) for p in patterns):
                    kept[key] = entry
                    taken.add(key)
                else:
                    skipped.setdefault(key, reason)
            if kept:
                inherited.append(Source("agent", source.file, source.label, kept, parent.name if source.via is None else f"{parent.name} <- {source.via}"))
        _, file, line = child.decl["parent"]
        links.append(RelayLink(child.name, parent.name, patterns, file, line, tuple(sorted(taken)), tuple(sorted(skipped.items()))))
        effective = inherited + child.sources
    return effective, links[::-1]


def _relay_account(request: Request, chain: list[_Agent]) -> Relay:
    if request.account:
        return Relay(account=request.account, account_from="--account")
    for index, agent in enumerate(chain):
        if agent.account:
            return Relay(account=agent.account, account_from="agent" if index == 0 else "parent", account_agent=agent.name)
    return Relay()


def relay_edges(root: Path, project: str | None, kinds: list[FileKind] | None = None) -> list[RelayEdge]:
    """Every agent under the source root (and the project) that names a parent, sorted by agent."""
    kinds = FILE_KINDS if kinds is None else kinds
    request = Request(Path(root), project)
    dirs = [request.root / "agents", *([request.root / "projects-root" / project / "agents"] if project else [])]
    names = sorted({f.stem for d in dirs if d.is_dir() for f in d.iterdir() if f.suffix in PARSERS})
    edges: list[RelayEdge] = []
    for name in names:
        agent = _read_agent(request, kinds, name)
        if agent.parent:
            _, file, line = agent.decl["parent"]
            edges.append(RelayEdge(name, agent.parent, agent.inherit, file, line))
    return edges


def _load(request: Request, kinds: list[FileKind]) -> tuple[list[Source], Relay]:
    _validate(request, kinds)
    chain = _agent_chain(request, kinds) if request.agent else []
    agent_sources, links = _relay_sources(chain) if chain else ([], [])
    relay = replace(_relay_account(request, chain), links=links)
    if relay.account and not _find_named(request.root, kinds, "account", "account", relay.account):
        owner = next((a for a in chain if a.name == relay.account_agent), None)
        _, file, line = owner.decl["account"] if owner else (None, None, None)
        raise LayerError("unknown-account", f'The account "{relay.account}" has no file under accounts/.', file=file, line=line, key="account", hint="Create accounts/<name>.toml, or check the spelling.")
    values = {"project": request.project, "agent": request.agent, "account": relay.account, "runtime": request.runtime}
    sources: list[Source] = []
    for layer in LAYERS:
        if layer == "agent":
            sources += agent_sources
            continue
        for kind in kinds:
            if kind.layer != layer:
                continue
            needed = re.findall(r"\{(\w+)\}", kind.template)
            if any(not values.get(n) for n in needed):
                continue
            path = request.root / kind.template.format(**values)
            if not path.is_file():
                continue
            data, lines = PARSERS[path.suffix](path)
            sources.append(Source(layer, path, kind.template.format(**values), flatten(data, lines, path, kind.namespace)))
    return sources, relay


def load_sources(request: Request, kinds: list[FileKind] | None = None) -> list[Source]:
    """Read every layer file the request reaches, in precedence order (base first). Env and flags are added by `load`."""
    return _load(request, FILE_KINDS if kinds is None else kinds)[0]


def env_name(key: str) -> str:
    return ENV_PREFIX + re.sub(r"[^A-Za-z0-9_]", "_", key.replace(".", "__")).upper()


def _env_sources(keys: set[str], environ: Mapping[str, str]) -> list[Source]:
    entries: dict[str, Entry] = {}
    for key in sorted(keys):
        name = env_name(key)
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        mode = environ.get(name + "_MODE") or None
        entries[key] = Entry(value, None, mode)
    return [Source("env", None, "$" + ENV_PREFIX + "*", entries)] if entries else []


# ---- resolution -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One layer's contribution to a key."""

    layer: str
    file: Path | None
    label: str
    line: int | None
    op: str  # set, merge, replace, extend
    mode: str | None
    value: Any
    overrode: tuple[tuple[str, Any], ...] = ()  # (layer, value) of the earlier steps this one discarded
    via: str | None = None  # the parent that relayed this value to the requested agent


@dataclass(frozen=True)
class Resolution:
    key: str
    value: Any
    steps: tuple[Step, ...]

    @property
    def decided_by(self) -> Step:
        return self.steps[-1]


def _fold(key: str, contributions: list[tuple[Source, Entry]]) -> Resolution:
    steps: list[Step] = []
    current: Any = None
    for source, entry in contributions:
        value, mode, line = entry.value, entry.mode, entry.line
        if not steps:
            current = list(value) if isinstance(value, list) else value
            steps.append(Step(source.layer, source.file, source.label, line, mode or "set", mode, value, via=source.via))
            continue
        earlier = tuple((s.layer, s.value) for s in steps)
        if isinstance(value, list):
            if not isinstance(current, list):
                raise LayerError("type-mismatch", f'"{key}" is a list in {source.label} but not in a lower layer', file=source.file, line=line, key=key, hint="Give the key one type in every layer.")
            if mode is None:
                hint = f"Pass --set-mode {key}=replace (or extend) beside the list." if source.layer == "flags" else f'Add "{MODES_KEY}": {{"{key.rsplit(".", 1)[-1]}": "replace"}} (or "extend") beside the list.'
                raise LayerError(
                    "list-mode-missing",
                    f'"{key}" is a list that {source.label} redefines without a mode, and there is no implicit append.',
                    file=source.file,
                    line=line,
                    key=key,
                    hint=hint,
                )
            op = mode
            current = list(value) if mode == "replace" else current + value
            steps.append(Step(source.layer, source.file, source.label, line, op, mode, value, earlier if mode == "replace" else (), source.via))
        elif isinstance(value, dict):
            if not isinstance(current, dict):
                raise LayerError("type-mismatch", f'"{key}" is a table in {source.label} but not in a lower layer', file=source.file, line=line, key=key, hint="Give the key one type in every layer.")
            current = {**current, **value}
            steps.append(Step(source.layer, source.file, source.label, line, "merge", None, value, via=source.via))
        else:
            if isinstance(current, (list, dict)):
                raise LayerError("type-mismatch", f'"{key}" is a scalar in {source.label} but a list or table in a lower layer', file=source.file, line=line, key=key, hint="Give the key one type in every layer.")
            steps.append(Step(source.layer, source.file, source.label, line, "set", None, value, earlier, source.via))
            current = value
    return Resolution(key, current, tuple(steps))


@dataclass
class Layers:
    """The loaded layers of one request, ready to answer for any key."""

    request: Request
    sources: list[Source]
    relay: Relay = field(default_factory=Relay)

    def keys(self) -> list[str]:
        return sorted({k for s in self.sources for k in s.entries})

    def resolve(self, key: str) -> Resolution:
        contributions = [(s, s.entries[key]) for s in self.sources if key in s.entries]
        if not contributions:
            hint = "Run `stratarc config list` to see the keys."
            for link in self.relay.links:
                for skipped, reason in link.skipped:
                    if skipped == key:
                        hint = f'"{link.parent}" sets this key but "{link.agent}" does not inherit it: {reason}.'
            raise LayerError("unknown-key", f'No layer sets "{key}".', key=key, hint=hint)
        return _fold(key, contributions)

    def expand(self, key: str) -> list[str]:
        """The leaf keys a name stands for: itself, or every key under it when it is a table prefix."""
        keys = self.keys()
        if key in keys:
            return [key]
        return [k for k in keys if k.startswith(key + ".")]

    def resolve_all(self) -> tuple[dict[str, Resolution], dict[str, LayerError]]:
        """Resolve every key; a key that fails is reported in the second mapping and does not stop the others."""
        done: dict[str, Resolution] = {}
        failed: dict[str, LayerError] = {}
        for key in self.keys():
            try:
                done[key] = self.resolve(key)
            except LayerError as exc:
                failed[key] = exc
        return done, failed


def load(
    root: Path,
    *,
    project: str | None = None,
    agent: str | None = None,
    account: str | None = None,
    runtime: str | None = None,
    environ: Mapping[str, str] | None = None,
    flags: Mapping[str, Any] | None = None,
    flag_modes: Mapping[str, str] | None = None,
    kinds: list[FileKind] | None = None,
) -> Layers:
    """Load the layers for a request.

    `environ` defaults to the process environment. `flags` maps a key to any value and forms the last layer (file `--set`, line 0); a list in `flags` that redefines a lower list needs its mode in `flag_modes`.
    """
    request = Request(Path(root), project, agent, account, runtime)
    sources, relay = _load(request, FILE_KINDS if kinds is None else kinds)
    flag_source = _flags_source(flags or {}, flag_modes or {}) if flags or flag_modes else None
    known = {k for s in sources for k in s.entries} | set(flags or {})
    sources += _env_sources(known, os.environ if environ is None else environ)
    if flag_source and flag_source.entries:
        sources.append(flag_source)
    return Layers(request, sources, relay)


def _flags_source(flags: Mapping[str, Any], modes: Mapping[str, str]) -> Source:
    for key, mode in modes.items():
        if key not in flags:
            raise LayerError("flag-invalid", f'--set-mode names "{key}" but no --set or --set-json sets it', key=key, hint="Pass --set-json KEY=<list> beside --set-mode, or drop the mode.")
        if mode not in MODES:
            raise LayerError("mode-invalid", f'mode "{mode}" for "{key}" is not one of: {", ".join(MODES)}', key=key, hint="Use replace or extend.")
        if not isinstance(flags[key], list):
            raise LayerError("mode-invalid", f'"{key}" has a mode but is not a list', key=key, hint="Remove --set-mode, or pass the value with --set-json as a list.")
    return Source("flags", None, FLAGS_LABEL, {k: Entry(v, 0, modes.get(k)) for k, v in flags.items()})


def nest(values: Mapping[str, Any]) -> dict[str, Any]:
    """Turn dotted keys back into nested tables."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        node = out
        *parents, last = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    return out


def display_path(path: Path | None, root: Path) -> str:
    """A file path for output: relative to the source root, else with the home shown as `~`."""
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return tilde(str(path))


def tilde(text: str) -> str:
    """Replace the home directory in `text` with `~`."""
    base = str(home())
    return text.replace(base, "~") if base and base != "/" else text
