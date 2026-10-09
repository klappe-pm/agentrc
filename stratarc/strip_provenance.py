"""Remove session provenance keys from Markdown frontmatter.

Frontmatter is the block that opens with a ``---`` line on the first line of the
file and closes at the next ``---`` line. Inside it, the keys ``models``,
``providers`` and ``session-link`` are removed together with any continuation
lines that belong to them (indented lines and ``- item`` list lines). Every
other byte of the file is preserved.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROVENANCE_KEYS = ("models", "providers", "session-link")

_KEY_RE = re.compile(r"^(?:" + "|".join(re.escape(k) for k in PROVENANCE_KEYS) + r")[ \t]*:")


def _is_fence(line: str) -> bool:
    return line.rstrip("\r\n") == "---"


def _is_continuation(line: str) -> bool:
    body = line.rstrip("\r\n")
    if not body.strip():
        return False
    if body[0] in " \t":
        return True
    return body == "-" or body.startswith("- ")


def strip_provenance(text: str) -> str:
    """Return ``text`` with the provenance keys removed from its frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or not _is_fence(lines[0]):
        return text
    close = next((i for i in range(1, len(lines)) if _is_fence(lines[i])), None)
    if close is None:
        return text

    kept: list[str] = [lines[0]]
    i = 1
    while i < close:
        line = lines[i]
        if _KEY_RE.match(line):
            i += 1
            while i < close and _is_continuation(lines[i]):
                i += 1
            continue
        kept.append(line)
        i += 1
    kept.extend(lines[close:])
    return "".join(kept)


def _process(path: Path, check: bool) -> bool:
    original = path.read_bytes().decode("utf-8")
    stripped = strip_provenance(original)
    if stripped == original:
        return False
    if not check:
        path.write_bytes(stripped.encode("utf-8"))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m stratarc.strip_provenance",
        description="Remove models, providers and session-link from Markdown frontmatter.",
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    parser.add_argument("--check", action="store_true", help="report files that would change and exit 1; write nothing")
    args = parser.parse_args(argv)

    changed = [p for p in args.paths if _process(p, args.check)]
    verb = "would strip" if args.check else "stripped"
    for p in changed:
        print(f"{verb}: {p}")
    if args.check and changed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
