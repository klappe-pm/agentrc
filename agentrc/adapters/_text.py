"""Text editors shared by the adapters and the project permission renderer.

strip_jsonc_comments reads the JSONC subset OpenCode uses; set_top_level edits a top-level
TOML scalar without disturbing tables, comments or hand edits.
"""

from __future__ import annotations

import re
from typing import List


def strip_jsonc_comments(text: str) -> str:
    """Strip // line comments, /* block comments */ and trailing commas from
    JSONC text, string-aware throughout so a quoted value is never rewritten.

    This is a best-effort parser for the subset of JSONC that OpenCode uses.
    Both passes track whether the current character is inside a double-quoted
    JSON string (honoring backslash escapes) and only ever act outside one,
    so a comment marker or a trailing-comma-shaped substring that happens to
    appear inside a string's own content is left untouched (issue #315: the
    old trailing-comma regex ran over the whole text with no such awareness
    and could delete a comma out of a string like "keep,}", and block
    comments were not recognized at all).
    """

    def _scan(source: str, on_char) -> str:
        """Walk *source* one character at a time, tracking string state,
        and delegate every decision to *on_char*."""
        out: List[str] = []
        in_string = False
        escape = False
        i = 0
        n = len(source)
        while i < n:
            ch = source[i]
            if in_string:
                out.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                i += 1
                continue
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue
            i = on_char(source, i, out)
        return "".join(out)

    def _strip_comments_char(source: str, i: int, out: List[str]) -> int:
        n = len(source)
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            return i
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                if source[i] == "\n":
                    out.append("\n")  # keep line numbers stable for parse errors
                i += 1
            return min(i + 2, n)
        out.append(ch)
        return i + 1

    cleaned = _scan(text, _strip_comments_char)

    def _strip_trailing_comma_char(source: str, i: int, out: List[str]) -> int:
        n = len(source)
        if source[i] == ",":
            j = i + 1
            while j < n and source[j] in " \t\r\n":
                j += 1
            if j < n and source[j] in "}]":
                return i + 1  # drop the comma; whitespace and bracket follow normally
        out.append(source[i])
        return i + 1

    return _scan(cleaned, _strip_trailing_comma_char)


def split_top_level(toml: str) -> tuple[str, str]:
    """Split a TOML document into its top-level section and everything from the first table on."""
    first_table = re.search(r"(?m)^\[", toml)
    if not first_table:
        return toml, ""
    return toml[: first_table.start()], toml[first_table.start() :]


def value_end(text: str, pos: int) -> int:
    """Index just past the TOML value that starts at `pos`, including its newline.

    A value ends at the first newline outside every string and at bracket
    depth zero, so a multi-line array or inline table is consumed whole with
    its continuation lines and closing bracket. Brackets and `#` inside basic,
    literal, and triple-quoted strings are skipped, and a comment runs to the
    end of its line.
    """
    depth = 0
    i = pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            quote = ch * 3 if text.startswith(ch * 3, i) else ch
            i += len(quote)
            while i < n and not text.startswith(quote, i):
                if len(quote) == 1 and text[i] == "\n":
                    break
                if ch == '"' and text[i] == "\\":
                    i += 1
                i += 1
            else:
                i += len(quote)
            continue
        if ch == "#":
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "\n" and depth <= 0:
            return i + 1
        i += 1
    return n


def top_level_entries(head: str, key: str) -> list[tuple[int, int, int]]:
    """(start, value_start, end) of every `key = value` entry in `head`."""
    entries = []
    for match in re.finditer(rf"(?m)^{re.escape(key)}[ \t]*=[ \t]*", head):
        if entries and match.start() < entries[-1][2]:
            continue
        entries.append((match.start(), match.end(), value_end(head, match.end())))
    return entries


def set_top_level(toml: str, key: str, value: str | None) -> str:
    """Set or remove a top-level scalar.

    Only the section before the first table header is edited. The same key
    inside any table belongs to that table and is never rewritten or removed.
    The existing value is replaced whole, including a multi-line array as
    Codex or a hand edit writes it (one item per line, closing bracket on its
    own line); rewriting only its first line orphaned the rest and left
    config.toml unparseable.

    Codex profiles are no longer tables in this file: `codex --profile <name>`
    layers $CODEX_HOME/<name>.config.toml over it, and a top-level
    `profile = "<name>"` stops Codex with "legacy `profile = ...` config is no
    longer supported" (verified 2026-09-23, Codex 0.156.1 `codex --help` and
    `codex exec`). A leftover [profiles.<name>] table still loads. This adapter
    writes neither.
    """
    head, tail = split_top_level(toml)
    entries = top_level_entries(head, key)
    line = "" if value is None else f"{key} = {value}\n"
    if entries or value is None:
        for index, (start, _, end) in reversed(list(enumerate(entries))):
            head = head[:start] + (line if index == 0 else "") + head[end:]
        return head + tail
    if head and not head.endswith("\n"):
        head += "\n"
    return head + line + tail
