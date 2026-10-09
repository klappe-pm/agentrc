"""The `log` and `verify` resources of the command line.

    stratarc log show|tail|explain|export|enable|disable|prune
    stratarc verify run|last|show

`main(argv)` takes the resource as its first token. `log_main` and `verify_main` take the arguments after it. Output is human readable by default and, with `--json`, the stable envelope `{"ok", "data", "error"}` that the rest of the command line prints. Exit codes follow the design: 0 ok, 2 invalid input, 5 unavailable, 6 drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stratarc import changelog, verify
from stratarc.messages import DRIFT, INVALID_INPUT, OK, UNAVAILABLE

FILTER_FLAGS = (
    ("--status", "status"),
    ("--project", "project"),
    ("--file", "file"),
    ("--layer", "layer"),
    ("--key", "key"),
    ("--actor-kind", "actor_kind"),
    ("--actor", "actor"),
    ("--command", "command"),
    ("--cause", "cause_id"),
    ("--since", "since"),
    ("--until", "until"),
)


def _common() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", action="store_true", help="print the stable JSON envelope")
    return parent


def _filters(parser: argparse.ArgumentParser) -> None:
    for flag, _name in FILTER_FLAGS:
        parser.add_argument(flag, metavar="VALUE")


def _filter_values(args: argparse.Namespace) -> dict:
    return {name: getattr(args, name) for _flag, name in FILTER_FLAGS if getattr(args, name, None)}


def _log_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stratarc log", description="The change history.")
    sub = parser.add_subparsers(dest="verb", required=True)
    common = _common()
    show = sub.add_parser("show", parents=[common], help="list changes, newest first")
    show.add_argument("--limit", type=int, default=20)
    _filters(show)
    tail = sub.add_parser("tail", parents=[common], help="the last lines of the human readable log")
    tail.add_argument("-n", "--lines", type=int, default=20)
    explain = sub.add_parser("explain", parents=[common], help="print one change as a short story")
    explain.add_argument("id")
    export = sub.add_parser("export", parents=[common], help="print or write changes as json, jsonl or csv")
    export.add_argument("--format", choices=("json", "jsonl", "csv"), default="jsonl")
    export.add_argument("--output", metavar="PATH")
    _filters(export)
    sub.add_parser("enable", parents=[common], help="turn the database and JSONL log on")
    sub.add_parser("disable", parents=[common], help="turn the database and JSONL log off; the human log stays on")
    prune = sub.add_parser("prune", parents=[common], help="remove stored changes before a date")
    prune.add_argument("--before", required=True, metavar="YYYY-MM-DD")
    prune.add_argument("--dry-run", action="store_true")
    return parser


def _verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stratarc verify", description="The recursive write-back test.")
    sub = parser.add_subparsers(dest="verb", required=True)
    common = _common()
    run = sub.add_parser("run", parents=[common], help="re-render and compare with what is deployed")
    run.add_argument("--scope", default="all", help="all, project:<name> or change:<id>")
    run.add_argument("--root", type=Path, default=None)
    run.add_argument("--verbose", action="store_true", help="list verified files too")
    last = sub.add_parser("last", parents=[common], help="the most recent report")
    last.add_argument("--verbose", action="store_true")
    show = sub.add_parser("show", parents=[common], help="one report by id")
    show.add_argument("id", nargs="?")
    show.add_argument("--verbose", action="store_true")
    return parser


def _emit(args: argparse.Namespace, code: int, data: dict | None, text: str, error: tuple[str, str, str | None] | None = None) -> int:
    if args.json:
        body = None
        if error is not None:
            body = {"code": error[0], "message": error[1], "param": None, "hint": error[2]}
        print(json.dumps({"ok": code == 0, "data": data, "error": body}, indent=2))
    elif error is not None:
        print(f"error {error[0]}: {error[1]}", file=sys.stderr)
        if error[2]:
            print(f"  {error[2]}", file=sys.stderr)
    elif text:
        print(text)
    return code


def _fail(args: argparse.Namespace, code: int, name: str, message: str, hint: str | None = None) -> int:
    return _emit(args, code, None, "", (name, message, hint))


def _table(changes: list[dict]) -> str:
    if not changes:
        return "no changes recorded"
    lines = []
    for c in changes:
        where = ",".join(c["projects"]) or "-"
        lines.append(f"{c['id']}  {c['ts']}  {c['status']:10s} {c['actor_kind']}:{c['actor'] or '-'}  {c['layer'] or '-'}  {c['file'] or '-'}  [{where}]")
    return "\n".join(lines)


def log_main(argv: list[str] | None = None) -> int:
    args = _log_parser().parse_args(argv)
    try:
        return _log(args)
    except changelog.ChangeLogError as error:
        return _fail(args, INVALID_INPUT, "invalid-input", str(error))


def _log(args: argparse.Namespace) -> int:
    verb = args.verb
    if verb == "show":
        changes = changelog.query(_filter_values(args), limit=args.limit)
        return _emit(args, OK, {"changes": changes, "enabled": changelog.is_enabled()}, _table(changes))
    if verb == "tail":
        lines = changelog.tail(args.lines)
        return _emit(args, OK, {"lines": lines, "path": str(changelog.human_log_path())}, "\n".join(lines))
    if verb == "explain":
        found = changelog.explain_data(args.id)
        return _emit(args, OK, found, found["story"])
    if verb == "export":
        text = changelog.export_text(args.format, _filter_values(args))
        if args.output:
            target = Path(args.output).expanduser()
            target.write_text(text, encoding="utf-8")
            return _emit(args, OK, {"path": str(target), "format": args.format}, f"wrote {target}")
        if args.json:
            return _emit(args, OK, {"format": args.format, "content": text}, "")
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return OK
    if verb in ("enable", "disable"):
        on = verb == "enable"
        path = changelog.set_enabled(on)
        if on:
            changelog.ensure_logs()
        note = "" if changelog.is_enabled() == on else f" ({changelog.LOG_VARIABLE} in the environment overrides it)"
        return _emit(args, OK, {"enabled": on, "config": str(path)}, f"change log {'enabled' if on else 'disabled'} in {path}{note}")
    if verb == "prune":
        count = changelog.prune(args.before, dry_run=args.dry_run)
        word = "would remove" if args.dry_run else "removed"
        return _emit(args, OK, {"removed": count, "dry_run": args.dry_run}, f"{word} {count} change(s) dated before {args.before}")
    raise AssertionError(verb)


def verify_main(argv: list[str] | None = None) -> int:
    args = _verify_parser().parse_args(argv)
    try:
        return _verify(args)
    except verify.VerifyError as error:
        return _fail(args, INVALID_INPUT, "invalid-input", str(error))


def _verify(args: argparse.Namespace) -> int:
    if args.verb == "run":
        report = verify.run(args.scope, root=args.root)
        data = report.to_dict()
        code = report.exit_code
        text = verify.format_report(data, verbose=args.verbose)
        if code and args.json:
            return _emit(args, code, data, text, ("drift", f"{report.counts[verify.DRIFTED]} file(s) differ from the render", "Run `stratarc sync apply` or inspect with `stratarc verify show`."))
        return _emit(args, code, data, text)
    report = verify.last() if args.verb == "last" else verify.show(args.id)
    if report is None:
        what = "no verify report has been written" if args.verb == "last" or not args.id else f"no verify report {args.id}"
        return _fail(args, UNAVAILABLE, "unavailable", what, "Run `stratarc verify run` first.")
    return _emit(args, OK, report, verify.format_report(report, verbose=args.verbose))


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens or tokens[0] not in ("log", "verify"):
        print("usage: stratarc (log|verify) <verb> [options]", file=sys.stderr)
        return INVALID_INPUT
    resource, rest = tokens[0], tokens[1:]
    return log_main(rest) if resource == "log" else verify_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
