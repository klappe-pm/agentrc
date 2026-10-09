"""Command line entry point for stratarc."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from importlib.resources.abc import Traversable

from stratarc import __version__
from stratarc.resources import data_dir

TEMPLATE = "templates/source-root"
STUBS = ("sync", "check", "diff", "prune", "reconcile")


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


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if target.exists():
        if not target.is_dir():
            print(f"stratarc init: {target} exists and is not a directory", file=sys.stderr)
            return 1
        if any(target.iterdir()):
            print(f"stratarc init: {target} is not empty; refusing to scaffold into it", file=sys.stderr)
            return 1
    template = data_dir(TEMPLATE)
    if not template.is_dir():
        print("stratarc init: packaged template is missing", file=sys.stderr)
        return 1
    count = _copy_tree(template, target)
    print(f"stratarc init: wrote {count} files to {target}")
    return 0


def _stub(name: str):
    def run(_args: argparse.Namespace) -> int:
        print(f"stratarc {name}: not yet extracted", file=sys.stderr)
        return 2

    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stratarc", description="One agent configuration source, rendered into every agent runtime.")
    parser.add_argument("--version", action="version", version=f"stratarc {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p_init = sub.add_parser("init", help="scaffold a new source root into TARGET")
    p_init.add_argument("target", help="directory to create; must be absent or empty")
    p_init.set_defaults(func=cmd_init)

    for name in STUBS:
        p = sub.add_parser(name, help=f"{name} (not yet extracted)")
        p.set_defaults(func=_stub(name))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
