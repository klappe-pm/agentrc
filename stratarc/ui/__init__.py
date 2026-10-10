"""`stratarc ui`: the full-screen terminal interface.

The interface needs Textual, which is an optional extra (`pip install "stratarc[ui]"`). This module imports nothing from it, so a base install can import `stratarc.ui` and ask for the interface; without Textual `main` prints a message that names the extra and returns 5 (unavailable).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stratarc.messages import UNAVAILABLE

MISSING = "error  The terminal interface needs Textual, which is not installed.\n  Install the extra with: pip install 'stratarc[ui]'"


def textual_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("textual") is not None


def interactive() -> bool:
    """Whether both standard input and output are a terminal, which the full-screen interface needs."""
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stratarc ui", description="Open the terminal interface.")
    parser.add_argument("--root", metavar="PATH", help="the source root")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not textual_available():
        print(MISSING, file=sys.stderr)
        return UNAVAILABLE
    from stratarc.ui.app import run

    from stratarc.paths import source_root

    return run(source_root(Path(args.root) if args.root else None))
