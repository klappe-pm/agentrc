"""Command line entry point for stratarc.

Every engine command calls the `main(argv)` of the module that owns it, after the global flags have been turned into the environment variables `stratarc.paths` reads. The flags are applied for the length of one command and undone afterwards, so a call from a test or another program leaves the process as it found it.

Exit statuses: 0 ok, 1 failure, 2 invalid input, 3 denied, 4 conflict, 5 unavailable, 6 drift, 130 interrupted. The constants live in `stratarc.messages`.
"""

from __future__ import annotations

import time

_LOADED = time.perf_counter()

import argparse
import contextlib
import io
import json
import os
import sys
import traceback
from collections.abc import Callable, Iterator
from importlib.resources.abc import Traversable
from pathlib import Path

from stratarc import __version__, paths
from stratarc.messages import DRIFT, FAILURE, INTERRUPTED, CliError
from stratarc.resources import data_dir

TEMPLATE = "templates/source-root"

# Commands that forward everything after their name to the module's own parser.
PASSTHROUGH = {
    "validate": "check a source root against the schemas and the engine's rules",
    "projects": "deliver projects-root/ into the managed project checkouts",
    "gen-rules-digest": "render or check the rules digest in instruction files",
    "components": "validate components.json and check declared services",
}

EXIT_NAMES = {
    1: "failure",
    2: "invalid-input",
    3: "denied",
    4: "conflict",
    5: "unavailable",
    6: "drift",
    INTERRUPTED: "interrupted",
}


def _copy_tree(src: Traversable, dest: Path) -> int:
    """Copy a Traversable tree into ``dest``; return the number of files written."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for child in sorted(src.iterdir(), key=lambda c: c.name):
        if child.name == "__pycache__":
            continue
        target = dest / child.name
        if child.is_dir():
            count += _copy_tree(child, target)
        elif child.is_file():
            target.write_bytes(child.read_bytes())
            count += 1
    return count


# ----- the global flags -----


def _global_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    group = parser.add_argument_group("global options")
    group.add_argument("--root", metavar="PATH", help="the source root (sets STRATARC_SOURCE)")
    group.add_argument("--home", metavar="PATH", help="the home directory the runtime targets hang under (sets STRATARC_HOME)")
    group.add_argument("--projects-root", metavar="PATH", help="the directory that holds project checkouts (sets LLM_ROOT_PROJECTS_DIR)")
    group.add_argument("--owner", metavar="NAME", help="the GitHub owner whose repositories are managed (sets STRATARC_GITHUB_OWNER)")
    group.add_argument("--json", action="store_true", help="print one JSON envelope instead of text")
    group.add_argument("--debug", action="store_true", help="print a traceback when a command stops unexpectedly")
    return parser


def _flag_environment(flags: argparse.Namespace) -> dict[str, str]:
    updates: dict[str, str] = {}
    if flags.root:
        updates[paths.SOURCE_VARIABLE] = str(Path(flags.root).expanduser().resolve())
    if flags.home:
        updates[paths.HOME_VARIABLE] = str(Path(flags.home).expanduser().resolve())
    if flags.projects_root:
        updates[paths.PROJECTS_VARIABLE] = str(Path(flags.projects_root).expanduser())
    if flags.owner:
        updates[paths.OWNER_VARIABLE] = flags.owner
    return updates


@contextlib.contextmanager
def _environment(updates: dict[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ----- the checks every engine command shares -----


def _require_source_root() -> Path:
    """The resolved source root, after checking that it exists and its stratarc.toml parses."""
    from stratarc.config import ConfigError, load_config

    root = paths.source_root()
    if not root.is_dir():
        raise CliError("msg-1001", param="root", path=root)
    try:
        load_config(root)
    except ConfigError as error:
        raise CliError("msg-1002", param=paths.CONFIG_NAME, detail=error) from error
    return root


def _require_writable_home() -> None:
    home = paths.home()
    if not (home.is_dir() and os.access(home, os.W_OK | os.X_OK)):
        raise CliError("msg-1003", param="home", path=home)


def _require_known_runtime(name: str | None) -> None:
    if not name:
        return
    from stratarc.adapters._common import runtime_registry

    known = sorted(runtime_registry())
    if name not in known:
        raise CliError("msg-1004", param="only", name=name, known=", ".join(known))


# ----- commands -----


def cmd_init(args: argparse.Namespace) -> tuple[int, dict]:
    target = Path(args.target)
    if target.exists():
        if not target.is_dir():
            raise CliError("msg-1006", param="target", path=target)
        if any(target.iterdir()):
            raise CliError("msg-1005", param="target", path=target)
    template = data_dir(TEMPLATE)
    if not template.is_dir():
        raise CliError("msg-1007")
    count = _copy_tree(template, target)
    return 0, {"target": str(target), "files": count, "text": f"stratarc init: wrote {count} files to {target}"}


def cmd_doctor(args: argparse.Namespace) -> tuple[int, dict]:
    from stratarc import doctor

    started_ms = (time.perf_counter() - _LOADED) * 1000
    data, problems = doctor.collect(root_flag=bool(args.global_flags.root), started_ms=started_ms)
    data["text"] = doctor.render(data, problems)
    if problems:
        data["problems"] = [{"code": p.id, "message": p.problem, "hint": p.recovery} for p in problems]
        raise _Reported(problems[0], data)
    return 0, data


class _Reported(Exception):
    """A failure that still carries data to print: the error, then the report."""

    def __init__(self, error: CliError, data: dict) -> None:
        super().__init__(error.problem)
        self.error = error
        self.data = data


def _engine_main(command: str) -> Callable[[list[str] | None], int]:
    if command in ("sync", "check", "diff", "prune"):
        from stratarc import sync

        return sync.main
    if command == "reconcile":
        from stratarc import reconcile

        return reconcile.main
    if command == "validate":
        from stratarc import validate

        return validate.main
    if command == "projects":
        from stratarc import projects

        return projects.main
    if command == "gen-rules-digest":
        from stratarc import gen_rules_digest

        return gen_rules_digest.main
    from stratarc import components

    return components.main


def _sync_family_argv(args: argparse.Namespace) -> list[str]:
    command = args.command
    argv: list[str] = {"check": ["--check"], "diff": ["--diff"], "prune": ["--prune"]}.get(command, [])
    if getattr(args, "list", False):
        argv.append("--list")
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    if args.only:
        argv += ["--only", args.only]
    for branch in getattr(args, "allow_branch", []):
        argv += ["--allow-branch", branch]
    return argv


def _reports_drift(command: str, argv: list[str]) -> bool:
    """True when this command's exit 1 means the deployed state differs, not that it failed."""
    if command == "check":
        return True
    if command == "reconcile":
        return "--check" in argv
    if command == "projects":
        return "--check" in argv or "--verify" in argv
    if command == "gen-rules-digest":
        return "--check" in argv
    return False


def cmd_engine(args: argparse.Namespace, rest: list[str]) -> tuple[int, dict]:
    command = args.command
    if command in PASSTHROUGH:
        argv = rest
    elif command == "reconcile":
        argv = (["--check"] if args.check else []) + (["--root", str(paths.source_root())] if args.global_flags.root else [])
    else:
        argv = _sync_family_argv(args)

    if not any(flag in rest for flag in ("-h", "--help")):
        _require_source_root()
        if command in ("sync", "check", "diff", "prune"):
            _require_known_runtime(args.only)
        writes = (command == "sync" and not args.dry_run and not args.list) or (command == "prune" and not args.dry_run)
        if writes:
            _require_writable_home()

    entry = _engine_main(command)
    if args.global_flags.json:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            raw = _call(entry, argv)
        data = {"command": command, "exit": raw, "stdout": out.getvalue(), "stderr": err.getvalue()}
    else:
        raw = _call(entry, argv)
        data = {"command": command, "exit": raw}
    code = DRIFT if raw == 1 and _reports_drift(command, argv) else raw
    if code not in (0, *EXIT_NAMES):
        code = FAILURE
    return code, data


def _call(entry: Callable[[list[str] | None], int], argv: list[str]) -> int:
    """Run a module's main; an argparse exit inside it becomes its return value."""
    try:
        result = entry(argv)
    except SystemExit as stop:
        if stop.code is None:
            return 0
        return stop.code if isinstance(stop.code, int) else FAILURE
    return result or 0


# ----- the parser -----


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stratarc",
        description="One agent configuration source, rendered into every agent runtime.",
        parents=[_global_parser()],
        allow_abbrev=False,
        epilog="Global options are accepted before or after the command.",
    )
    parser.add_argument("--version", action="version", version=f"stratarc {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser("init", help="scaffold a new source root into TARGET")
    p.add_argument("target", help="directory to create; must be absent or empty")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="report the health of the install, the home and the source root")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("sync", help="render the source into every enabled runtime and managed project")
    p.add_argument("--only", metavar="RUNTIME", help="sync one runtime and skip project delivery")
    p.add_argument("--allow-branch", action="append", default=[], metavar="BRANCH", help="deploy from this branch too; repeatable")
    p.add_argument("--dry-run", action="store_true", help="print what a sync would change and write nothing")
    p.add_argument("--list", action="store_true", help="list the runtimes and whether each target exists")

    p = sub.add_parser("check", help="report what is out of date, write nothing, exit 6 on drift")
    p.add_argument("--only", metavar="RUNTIME")
    p.add_argument("--allow-branch", action="append", default=[], metavar="BRANCH")

    p = sub.add_parser("diff", help="print what a sync would change, write nothing, exit 0")
    p.add_argument("--only", metavar="RUNTIME")
    p.add_argument("--allow-branch", action="append", default=[], metavar="BRANCH")

    p = sub.add_parser("prune", help="delete files a runtime holds that the source no longer produces")
    p.add_argument("--only", metavar="RUNTIME")
    p.add_argument("--allow-branch", action="append", default=[], metavar="BRANCH")
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted")

    p = sub.add_parser("reconcile", help="refresh control-plane.md from the source tree")
    p.add_argument("--check", action="store_true", help="report what is stale, write nothing, exit 6 if any")

    for name, text in PASSTHROUGH.items():
        sub.add_parser(name, help=text, add_help=False)
    return parser


# ----- output -----


def _print_error(error: CliError) -> None:
    print(f"error {error.id}  {error.problem}", file=sys.stderr)
    print(f"  {error.recovery}", file=sys.stderr)


def _envelope(ok: bool, data: dict | None, error: dict | None) -> str:
    return json.dumps({"ok": ok, "data": data, "error": error}, indent=2)


def _error_body(error: CliError) -> dict:
    return {"code": error.id, "message": error.problem, "param": error.param, "hint": error.recovery}


def _finish(args: argparse.Namespace, flags: argparse.Namespace, code: int, data: dict, error: CliError | None = None) -> int:
    text = data.pop("text", None)
    if flags.json:
        body = None
        if error is not None:
            body = _error_body(error)
        elif code:
            stderr = str(data.get("stderr", "")).strip().splitlines()
            body = {
                "code": EXIT_NAMES.get(code, "failure"),
                "message": stderr[-1] if stderr else f"stratarc {args.command} exited {code}",
                "param": None,
                "hint": None,
            }
        print(_envelope(code == 0, data or None, body))
        return code
    if error is not None:
        if text:
            print(text)
        _print_error(error)
    elif text:
        print(text)
    return code


# ----- entry -----


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    flags, rest = _global_parser().parse_known_args(tokens)

    command = rest[0] if rest else None
    if command in PASSTHROUGH:
        args = argparse.Namespace(command=command)
        forwarded = rest[1:]
    else:
        args = build_parser().parse_args(rest)
        forwarded = []
    args.global_flags = flags

    try:
        with _environment(_flag_environment(flags)):
            if args.command in PASSTHROUGH or not hasattr(args, "func"):
                code, data = cmd_engine(args, forwarded)
            else:
                code, data = args.func(args)
        return _finish(args, flags, code, data)
    except CliError as error:
        return _finish(args, flags, error.exit, {}, error)
    except _Reported as reported:
        # The report text already lists the problem and its recovery, so text mode prints no second copy.
        return _finish(args, flags, reported.error.exit, reported.data, reported.error if flags.json else None)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return INTERRUPTED
    except Exception as exc:
        from stratarc.config import ConfigError

        if isinstance(exc, ConfigError):
            return _finish(args, flags, 2, {}, CliError("msg-1002", param=paths.CONFIG_NAME, detail=exc))
        if flags.debug:
            traceback.print_exc()
        return _finish(args, flags, FAILURE, {}, CliError("msg-1008", command=args.command, detail=exc))


if __name__ == "__main__":
    sys.exit(main())
