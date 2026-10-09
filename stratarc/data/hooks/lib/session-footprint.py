#!/usr/bin/env python3
"""What one Claude Code session did to git working trees, read from its transcript.

Backs hooks/require-pr-on-stop.sh. Several sessions share one canonical checkout,
and git itself cannot tell their work apart: remote tracking reflogs live in the
common git directory of every worktree, a fetch by any session moves them, and
`git status` lists every session's uncommitted files and the user's hand edits
alike. The Stop payload's transcript_path is the only signal that belongs to one
session, so this module reads it, plus the transcripts of subagents the session
dispatched, and reports:

  repos <transcript> <cwd>
      one directory per line: the cwd, the parent of every file an editor tool
      wrote, and every directory a shell command entered with `cd <dir>` or
      addressed with `git -C <dir>`. The caller resolves each to its worktree
      top level and judges only those.
  merges <transcript>
      one pull request merged with `gh pr merge` per line, as
      "<selector>␟<repository or empty>␟<directory or empty>", joined by the ASCII
      unit separator (0x1f). The selector
      is the number, URL or branch the command named; a merge that named none
      is reported with the directory a preceding `cd` entered, or skipped when
      there was none, since its pull request cannot be identified.
  owned <transcript> <repo> <session start epoch or empty>
      reads `git status --porcelain=v1 -z` output on stdin and prints, one per
      line, the changed paths that are this session's: written by an editor
      tool, or named in a shell command by absolute path or by repository
      relative path alongside the repository root. A path whose file was last
      modified before the session start is never this session's.

A transcript that is absent or unreadable is reported on stdout as the single
line "unavailable", so the caller keeps its transcript-free behavior.
"""

import glob
import json
import os
import re
import shlex
import sys
from typing import Iterator, List, Optional, Set, Tuple

# Merge fields are joined with the ASCII unit separator, not a tab: a shell `read` collapses
# adjacent whitespace delimiters, which would shift an empty field into the next one.
UNIT = "\x1f"
EDITOR_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "path")
SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
MERGE_VALUE_FLAGS = {
    "-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject",
    "-A", "--author-email", "--match-head-commit",
}


def transcripts(path: str) -> Optional[List[str]]:
    """The session transcript and its subagent transcripts, or None when the session's is unreadable."""
    if not path or not os.path.isfile(path):
        return None
    base = path[: -len(".jsonl")] if path.endswith(".jsonl") else path
    found = [path]
    found.extend(sorted(glob.glob(os.path.join(glob.escape(base), "subagents", "**", "*.jsonl"), recursive=True)))
    return found


def tool_uses(paths: List[str]) -> Iterator[Tuple[str, dict]]:
    """Every (tool name, input) the transcripts record; unparseable lines are skipped."""
    for path in paths:
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"tool_use"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message") if isinstance(record, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use" and isinstance(item.get("input"), dict):
                        yield str(item.get("name") or ""), item["input"]


def is_editor(name: str) -> bool:
    return name in EDITOR_TOOLS or name.endswith("__write_file") or name.endswith("__edit_block")


def written_paths(uses: List[Tuple[str, dict]]) -> Set[str]:
    out = set()
    for name, given in uses:
        if not is_editor(name):
            continue
        for key in PATH_KEYS:
            value = given.get(key)
            if isinstance(value, str) and value:
                out.add(os.path.realpath(os.path.expanduser(value)))
    return out


def shell_commands(uses: List[Tuple[str, dict]]) -> List[str]:
    return [given["command"] for name, given in uses if name == "Bash" and isinstance(given.get("command"), str)]


def words(segment: str) -> List[str]:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def entered_dirs(command: str) -> List[str]:
    """Directories a command enters with cd or addresses with git -C, in order."""
    out = []
    for segment in SEPARATORS.split(command):
        tokens = words(segment)
        if len(tokens) >= 2 and tokens[0] == "cd":
            out.append(tokens[1])
        for index, token in enumerate(tokens[:-1]):
            if token == "-C" and index > 0 and os.path.basename(tokens[index - 1]) == "git":
                out.append(tokens[index + 1])
    return [os.path.expanduser(value) for value in out]


def merges_in(command: str) -> Iterator[Tuple[str, str, str]]:
    current_dir = ""
    for segment in SEPARATORS.split(command):
        tokens = words(segment)
        if len(tokens) >= 2 and tokens[0] == "cd":
            current_dir = os.path.expanduser(tokens[1])
            continue
        if tokens[:3] != ["gh", "pr", "merge"]:
            continue
        selector, repo, rest = "", "", tokens[3:]
        index = 0
        while index < len(rest):
            token = rest[index]
            if token in MERGE_VALUE_FLAGS:
                if token in ("-R", "--repo") and index + 1 < len(rest):
                    repo = rest[index + 1]
                index += 2
                continue
            if token.startswith("--repo="):
                repo = token.split("=", 1)[1]
            elif not token.startswith("-") and not selector:
                selector = token
            index += 1
        if selector or current_dir:
            yield selector, repo, current_dir


def load(path: str) -> Optional[List[Tuple[str, dict]]]:
    found = transcripts(path)
    return None if found is None else list(tool_uses(found))


def porcelain_paths(raw: str) -> List[str]:
    fields = raw.split("\0")
    out, index = [], 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        out.append(entry[3:])
        if entry[0] in "RC":
            index += 1
    return out


def owned(uses: List[Tuple[str, dict]], repo: str, start: str, raw: str) -> List[str]:
    root = os.path.realpath(repo)
    written = written_paths(uses)
    commands = shell_commands(uses)
    rooted = [command for command in commands if root in command or repo in command]
    out = []
    for relative in porcelain_paths(raw):
        absolute = os.path.join(root, relative.rstrip("/"))
        if start:
            try:
                if os.path.getmtime(absolute) < int(start):
                    continue
            except OSError:
                pass
        named = (
            os.path.realpath(absolute) in written
            or any(absolute in command or os.path.join(repo, relative) in command for command in commands)
            or any(re.search(r"(^|[\s'\"=/])" + re.escape(relative) + r"($|[\s'\";|&)])", command) for command in rooted)
        )
        if named:
            out.append(relative)
    return out


def main(argv: List[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("usage: session-footprint.py repos|merges|owned <transcript> ...\n")
        return 2
    action, path = argv[1], argv[2]
    uses = load(path)
    if uses is None:
        print("unavailable")
        return 0
    if action == "repos":
        seen: List[str] = []
        candidates = [argv[3] if len(argv) > 3 else ""]
        candidates += [os.path.dirname(value) for value in sorted(written_paths(uses))]
        for command in shell_commands(uses):
            candidates += entered_dirs(command)
        for value in candidates:
            if value and os.path.isdir(value) and value not in seen:
                seen.append(value)
        print("\n".join(seen))
    elif action == "merges":
        seen_merges: List[Tuple[str, str, str]] = []
        for command in shell_commands(uses):
            for merge in merges_in(command):
                if merge not in seen_merges:
                    seen_merges.append(merge)
        print("\n".join(UNIT.join(merge) for merge in seen_merges))
    elif action == "owned":
        repo = argv[3] if len(argv) > 3 else ""
        start = argv[4] if len(argv) > 4 else ""
        print("\n".join(owned(uses, repo, start, sys.stdin.read())))
    else:
        sys.stderr.write("session-footprint.py: unknown action: %s\n" % action)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
