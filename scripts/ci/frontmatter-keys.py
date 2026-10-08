#!/usr/bin/env python3
"""Fail when any Markdown file's frontmatter carries a provenance key.

A public repository never records session provenance. This check walks every
tracked and untracked, non-ignored Markdown file and fails on a `models`,
`providers` or `session-link` key in its leading frontmatter, so a stripped
key cannot return through a later pull request. Exit 0 clean, 1 on a hit.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

FORBIDDEN = re.compile(r"^(models|providers|session-link)[ \t]*:", re.MULTILINE)


def frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def markdown_files(root: pathlib.Path) -> list[pathlib.Path]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [root / name for name in listed.split("\0") if name.endswith(".md")]


def main() -> int:
    root = pathlib.Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    hits = []
    for path in markdown_files(root):
        if not path.is_file():
            continue
        found = FORBIDDEN.findall(frontmatter(path.read_text(encoding="utf-8", errors="replace")))
        for key in sorted(set(found)):
            hits.append(f"{path.relative_to(root)}: frontmatter key `{key}` is not allowed")
    for hit in hits:
        print(f"frontmatter-keys: {hit}", file=sys.stderr)
    if hits:
        return 1
    print("frontmatter-keys: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
