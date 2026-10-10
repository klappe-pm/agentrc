"""`stratarc config`: read-only views of the resolved layers.

Usage:
  config get <key>                      the resolved value of one key (a table prefix returns the nested table)
  config list                           every resolved key
  config explain <key>                  the resolution chain for a key, with file and line for every layer
  config explain --tree --project P     the pruned inheritance outline for a project, with the relay edges of its agents

Every command accepts `--project`, `--agent`, `--account`, `--runtime`, `--root PATH` and `--json`, and the flags layer: `--set KEY=VALUE` (a literal string), `--set-json KEY=JSON` and `--set-mode KEY=replace|extend` (needed when a flag list redefines a list), each repeatable. For a sub-agent with a `parent`, `explain` prints the relay steps after the agent layer. With `--json` the output is one envelope `{ok, data, error}` where `error` carries `code`, `message`, `param` and `hint`. Exit codes: 0 ok, 2 invalid input or an unresolvable key. The command never writes.

The error codes below are stable strings; the coordinating command line maps them to catalog ids: `list-mode-missing`, `mode-invalid`, `type-mismatch`, `parse-error`, `unknown-key`, `unknown-project`, `unknown-agent`, `unknown-account`, `unknown-runtime`, `relay-invalid`, `relay-cycle`, `relay-depth`, `flag-invalid`, `source-root-missing`, `project-required`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from stratarc import layers
from stratarc.layers import LAYERS, Layers, LayerError, Relay, Resolution, Step
from stratarc.paths import source_root

OK = 0
INVALID_INPUT = 2


def _tidy(value: Any) -> Any:
    """Show the home directory as `~` in every string of a value."""
    if isinstance(value, str):
        return layers.tilde(value)
    if isinstance(value, list):
        return [_tidy(v) for v in value]
    if isinstance(value, dict):
        return {k: _tidy(v) for k, v in value.items()}
    return value


def _dump(value: Any) -> str:
    return json.dumps(_tidy(value), ensure_ascii=False)


def _parser() -> argparse.ArgumentParser:
    def options(suppress: bool) -> argparse.ArgumentParser:
        # The same options are accepted before and after the action; the copy on the action must not overwrite a value given before it.
        extra: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress else {}
        p = argparse.ArgumentParser(add_help=False)
        p.add_argument("--root", metavar="PATH", help="the source root", **extra)
        p.add_argument("--json", action="store_true", help="print one JSON envelope", **extra)
        p.add_argument("--project", metavar="P", **extra)
        p.add_argument("--agent", metavar="A", **extra)
        p.add_argument("--account", metavar="X", **extra)
        p.add_argument("--runtime", metavar="R", **extra)
        # Repeatable flags accumulate, so the copy on the action gets its own destination and `_flags` joins the two.
        late = "_late" if suppress else ""
        p.add_argument("--set", dest="sets" + late, action="append", metavar="KEY=VALUE", help="set a key to a literal string in the flags layer (repeatable)", **extra)
        p.add_argument("--set-json", dest="set_json" + late, action="append", metavar="KEY=JSON", help="set a key to a JSON value in the flags layer (repeatable)", **extra)
        p.add_argument("--set-mode", dest="set_mode" + late, action="append", metavar="KEY=MODE", help="replace or extend, for a --set-json list that redefines a list", **extra)
        return p

    common = options(True)
    parser = argparse.ArgumentParser(prog="stratarc config", description="Read the resolved configuration layers.", parents=[options(False)])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("get", parents=[common], help="print the resolved value of a key").add_argument("key")
    sub.add_parser("list", parents=[common], help="print every resolved key")
    explain = sub.add_parser("explain", parents=[common], help="print the resolution chain for a key")
    explain.add_argument("key", nargs="?")
    explain.add_argument("--tree", action="store_true", help="print the inheritance outline for --project")
    return parser


# ---- data shapes ----------------------------------------------------------------------------


def _step_data(step: Step, root: Path) -> dict[str, Any]:
    return {
        "layer": step.layer,
        "file": layers.display_path(step.file, root) if step.file else step.label,
        "line": step.line,
        "op": step.op,
        "mode": step.mode,
        "value": _tidy(step.value),
        "overrode": [{"layer": layer, "value": _tidy(value)} for layer, value in step.overrode],
        "via": step.via,
    }


def _key_relay(res: Resolution, relay: Relay) -> dict[str, Any]:
    """What the relay did for one key: the parent that supplied the deciding value and the parents whose value was left behind."""
    supplied = next((s.via for s in reversed(res.steps) if s.via), None)
    left = [{"parent": link.parent, "reason": dict(link.skipped)[res.key], "agent": link.agent} for link in relay.links if res.key in dict(link.skipped)]
    return {"supplied_by": supplied.split(" <- ")[0] if supplied else None, "not_inherited": [{k: v for k, v in item.items() if k != "agent"} for item in left]}


def _resolution_data(res: Resolution, root: Path, relay: Relay | None = None) -> dict[str, Any]:
    data = {
        "key": res.key,
        "value": _tidy(res.value),
        "decided_by": {"layer": res.decided_by.layer, "op": res.decided_by.op},
        "steps": [_step_data(s, root) for s in res.steps],
    }
    if relay and relay.links:
        data["relay"] = _key_relay(res, relay)
    return data


def _relay_data(relay: Relay, root: Path) -> dict[str, Any]:
    return {
        "chain": [{"agent": l.agent, "parent": l.parent, "inherit": list(l.inherit), "file": layers.display_path(l.file, root), "line": l.line} for l in relay.links],
        "account": {"name": relay.account, "from": relay.account_from or None, "agent": relay.account_agent},
    }


def _error_data(exc: LayerError, root: Path) -> dict[str, Any]:
    return {
        "code": exc.code,
        "message": _where(exc, root) + exc.message,
        "param": exc.key,
        "hint": exc.hint,
    }


def _where(exc: LayerError, root: Path) -> str:
    if exc.file is None:
        return ""
    return layers.display_path(exc.file, root) + (f":{exc.line}" if exc.line else "") + ": "


def _flags(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str]]:
    """The flags layer from `--set`, `--set-json` and `--set-mode`; the value is never echoed in an error."""

    def pairs(option: str, *raws: list[str] | None) -> list[tuple[str, str]]:
        out = []
        for raw in (item for group in raws for item in group or []):
            key, sep, value = raw.partition("=")
            key = key.strip()
            if not sep or not key:
                raise LayerError("flag-invalid", f"{option} expects KEY=VALUE.", key=key or None, hint=f"Write {option} permissions.timeout=30.")
            out.append((key, value))
        return out

    flags: dict[str, Any] = dict(pairs("--set", args.sets, getattr(args, "sets_late", None)))
    for key, text in pairs("--set-json", args.set_json, getattr(args, "set_json_late", None)):
        try:
            flags[key] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LayerError("flag-invalid", f'the --set-json value for "{key}" is not valid JSON: {exc.msg}.', key=key, hint="Quote the value for your shell, for example --set-json 'k=[\"a\"]'.") from exc
    modes = dict(pairs("--set-mode", args.set_mode, getattr(args, "set_mode_late", None)))
    return flags, modes


def _scope(args: argparse.Namespace) -> dict[str, str | None]:
    return {"project": args.project, "agent": args.agent, "account": args.account, "runtime": args.runtime}


# ---- human output ---------------------------------------------------------------------------


def _location(step: Step, root: Path) -> str:
    if step.file is None:
        return step.label
    return layers.display_path(step.file, root) + (f":{step.line}" if step.line else "")


def _detail(step: Step, first: bool) -> str:
    text = _dump(step.value)
    if step.op == "extend":
        text = "+ " + text
    if step.mode and not (first and step.op == "set"):
        text = f"mode={step.mode}  {text}"
    if step.via:
        text += f"   (via {step.via})"
    return text


def _relay_lines(res: Resolution, relay: Relay, root: Path) -> list[str]:
    """The relay steps printed under the agent layer: each edge, what this key took or left behind, and the account."""
    out = []
    for link in relay.links:
        inherit = ", ".join(link.inherit) or "(none)"
        out.append(f"  {'relay':<9}{link.agent} <- {link.parent}   inherit: {inherit}   ({layers.display_path(link.file, root)}:{link.line})")
    info = _key_relay(res, relay)
    if info["supplied_by"]:
        out.append(f"  {'relay':<9}{res.key} inherited from {info['supplied_by']}")
    for index, link in enumerate(relay.links):
        if res.key in dict(link.skipped):
            by = "" if index == 0 else f" by {link.agent}"
            out.append(f"  {'relay':<9}{link.parent} sets {res.key} but it is not inherited{by}: {dict(link.skipped)[res.key]}")
    if relay.account_from == "parent":
        out.append(f"  {'relay':<9}account: {relay.account} (from {relay.account_agent}; {relay.links[0].agent} sets none)")
    elif relay.account_from == "agent":
        out.append(f"  {'relay':<9}account: {relay.account} (from {relay.account_agent})")
    elif relay.account_from:
        out.append(f"  {'relay':<9}account: {relay.account} (from {relay.account_from})")
    else:
        out.append(f"  {'relay':<9}account: none")
    return out


def _header(key: str, scope: dict[str, str | None]) -> str:
    given = ", ".join(f"{name}: {value}" for name, value in scope.items() if value)
    return f"{key}   ({given})" if given else key


def _explain_text(res: Resolution, scope: dict[str, str | None], root: Path, relay: Relay | None = None) -> str:
    out = [_header(res.key, scope)]
    requested = {"project", "agent", "account", "runtime"} & {name for name, value in scope.items() if value}
    shown = [layer for layer in LAYERS if layer == "base" or layer in requested or any(s.layer == layer for s in res.steps)]
    width = max(len(_location(s, root)) for s in res.steps)
    for layer in shown:
        steps = [s for s in res.steps if s.layer == layer]
        if not steps:
            out.append(f"  {layer:<9}{'':<{width}}  (not set)")
        for step in steps:
            out.append(f"  {layer:<9}{_location(step, root):<{width}}  {_detail(step, step is res.steps[0])}")
        if layer == "agent" and relay and relay.links:
            out += _relay_lines(res, relay, root)
    out.append(f"result: {_dump(res.value)}   decided by: {res.decided_by.layer} ({res.decided_by.op})")
    return "\n".join(out)


def _tree_text(project: str, data: dict[str, Any], failed: dict[str, LayerError], root: Path) -> str:
    out = [f"{project}   (project: {project})"]
    out.append(f"  {data['unchanged']} keys inherited unchanged from base")
    for entry in data["keys"]:
        out.append(f"  {entry['key']}   {_dump(entry['value'])}   decided by: {entry['decided_by']['layer']} ({entry['decided_by']['op']})")
        for step in entry["steps"]:
            where = step["file"] + (f":{step['line']}" if step["line"] else "")
            out.append(f"    {step['layer']:<9}{where}  {step['op']}")
    for key, exc in failed.items():
        out.append(f"  {key}   ERROR {exc.code}: {_where(exc, root)}{exc.message}")
    for edge in data.get("relay", []):
        out.append(f"  relay   {edge['agent']} <- {edge['parent']}   inherit: {', '.join(edge['inherit']) or '(none)'}   {edge['file']}:{edge['line']}")
    return "\n".join(out)


# ---- commands -------------------------------------------------------------------------------


def _get(layered: Layers, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    keys = layered.expand(args.key)
    if not keys:
        layered.resolve(args.key)  # raises unknown-key
    resolved = {k: layered.resolve(k) for k in keys}
    root = layered.request.root
    if keys == [args.key]:
        res = resolved[args.key]
        text = res.value if isinstance(res.value, str) else _dump(res.value)
        return _resolution_data(res, root), _tidy(text) if isinstance(text, str) else text
    value = layers.nest({k[len(args.key) + 1 :]: r.value for k, r in resolved.items()})
    return {"key": args.key, "value": _tidy(value)}, _dump(value)


def _list(layered: Layers, args: argparse.Namespace) -> tuple[dict[str, Any], str, list[LayerError]]:
    done, failed = layered.resolve_all()
    root = layered.request.root
    lines = [f"{k} = {_dump(r.value)}" for k, r in done.items()]
    lines += [f"{k}   ERROR {e.code}: {_where(e, root)}{e.message}" for k, e in failed.items()]
    data = {
        "scope": _scope(args),
        "values": {k: _tidy(r.value) for k, r in done.items()},
        "errors": [_error_data(e, root) for e in failed.values()],
    }
    return data, "\n".join(lines), list(failed.values())


def _explain(layered: Layers, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    root = layered.request.root
    scope = _scope(args)
    if args.tree:
        if not args.project:
            raise LayerError("project-required", "`explain --tree` needs a project.", key="project", hint="Pass --project NAME.")
        done, failed = layered.resolve_all()
        changed = {k: r for k, r in done.items() if any(s.layer != "base" for s in r.steps)}
        data = {
            "project": args.project,
            "scope": scope,
            "unchanged": len(done) - len(changed),
            "keys": [_resolution_data(r, root) for r in changed.values()],
            "errors": [_error_data(e, root) for e in failed.values()],
            "relay": [
                {"agent": e.agent, "parent": e.parent, "inherit": list(e.inherit), "file": layers.display_path(e.file, root), "line": e.line}
                for e in layers.relay_edges(root, args.project)
            ],
        }
        return data, _tree_text(args.project, data, failed, root)
    if not args.key:
        raise LayerError("unknown-key", "`explain` needs a key or --tree.", key="key", hint="Run `stratarc config list` to see the keys.")
    keys = layered.expand(args.key)
    if not keys:
        layered.resolve(args.key)  # raises unknown-key
    results = [layered.resolve(k) for k in keys]
    relay = layered.relay
    data: dict[str, Any] = {"scope": scope, "keys": [_resolution_data(r, root, relay) for r in results]}
    if relay.links:
        data["relay"] = _relay_data(relay, root)
    return data, "\n\n".join(_explain_text(r, scope, root, relay) for r in results)


def _emit(as_json: bool, ok: bool, data: dict | None, error: dict | None, text: str) -> None:
    if as_json:
        print(layers.tilde(json.dumps({"ok": ok, "data": data, "error": error}, indent=2, ensure_ascii=False)))
    elif ok:
        print(text)
    else:
        if text:
            print(text)
        print(f"error {error['code']}  {error['message']}", file=sys.stderr)
        if error["hint"]:
            print(f"  {error['hint']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = source_root(Path(args.root) if args.root else None)
    try:
        if not root.is_dir():
            raise LayerError("source-root-missing", f"The source root {root} does not exist.", hint="Pass an existing directory with --root.")
        flags, flag_modes = _flags(args)
        layered = layers.load(root, project=args.project, agent=args.agent, account=args.account, runtime=args.runtime, flags=flags, flag_modes=flag_modes)
        if args.action == "get":
            data, text = _get(layered, args)
            _emit(args.json, True, data, None, text)
            return OK
        if args.action == "list":
            data, text, failures = _list(layered, args)
            if failures:
                error = {
                    "code": failures[0].code,
                    "message": f"{len(failures)} key(s) could not be resolved; the first is {failures[0].key}.",
                    "param": failures[0].key,
                    "hint": failures[0].hint,
                }
                _emit(args.json, False, data, error, text)
                return INVALID_INPUT
            _emit(args.json, True, data, None, text)
            return OK
        data, text = _explain(layered, args)
        failures = data.get("errors", [])
        if failures:
            _emit(args.json, False, data, failures[0], text)
            return INVALID_INPUT
        _emit(args.json, True, data, None, text)
        return OK
    except LayerError as exc:
        _emit(args.json, False, None, _error_data(exc, root), "")
        return INVALID_INPUT
