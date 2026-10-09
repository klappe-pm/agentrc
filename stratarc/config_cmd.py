"""`stratarc config`: read-only views of the resolved layers.

Usage:
  config get <key>                      the resolved value of one key (a table prefix returns the nested table)
  config list                           every resolved key
  config explain <key>                  the resolution chain for a key, with file and line for every layer
  config explain --tree --project P     the pruned inheritance outline for a project

Every command accepts `--project`, `--agent`, `--account`, `--runtime`, `--root PATH` and `--json`. With `--json` the output is one envelope `{ok, data, error}` where `error` carries `code`, `message`, `param` and `hint`. Exit codes: 0 ok, 2 invalid input or an unresolvable key. The command never writes.

The error codes below are stable strings; the coordinating command line maps them to catalog ids: `list-mode-missing`, `mode-invalid`, `type-mismatch`, `parse-error`, `unknown-key`, `unknown-project`, `unknown-agent`, `unknown-account`, `unknown-runtime`, `source-root-missing`, `project-required`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from stratarc import layers
from stratarc.layers import LAYERS, Layers, LayerError, Resolution, Step
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
    }


def _resolution_data(res: Resolution, root: Path) -> dict[str, Any]:
    return {
        "key": res.key,
        "value": _tidy(res.value),
        "decided_by": {"layer": res.decided_by.layer, "op": res.decided_by.op},
        "steps": [_step_data(s, root) for s in res.steps],
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
    return text


def _header(key: str, scope: dict[str, str | None]) -> str:
    given = ", ".join(f"{name}: {value}" for name, value in scope.items() if value)
    return f"{key}   ({given})" if given else key


def _explain_text(res: Resolution, scope: dict[str, str | None], root: Path) -> str:
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
        }
        return data, _tree_text(args.project, data, failed, root)
    if not args.key:
        raise LayerError("unknown-key", "`explain` needs a key or --tree.", key="key", hint="Run `stratarc config list` to see the keys.")
    keys = layered.expand(args.key)
    if not keys:
        layered.resolve(args.key)  # raises unknown-key
    results = [layered.resolve(k) for k in keys]
    data = {"scope": scope, "keys": [_resolution_data(r, root) for r in results]}
    return data, "\n\n".join(_explain_text(r, scope, root) for r in results)


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
        layered = layers.load(root, project=args.project, agent=args.agent, account=args.account, runtime=args.runtime)
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
