#!/usr/bin/env python3
"""Shared detection and stripping for agent authorship attribution.

Backs the no-agent-attribution rule. Runtime system prompts routinely append
authorship credit to commit messages, pull request bodies, and file footers:
a co-author trailer naming a model, a "generated with" line, a session
permalink, a robot emoji byline. The user's standing rule is that authored
output carries no agent attribution, so this module is the single detection
implementation behind every enforcement point:

  * the PreToolUse guard hooks/no-attribution-guard.sh, which denies a write,
    an authoring shell command, or a GitHub MCP call whose text carries
    attribution before it lands (an Edit is judged in the file it lands in,
    a shell command with every body file it names), and the carried copy of
    that guard the project sync deploys into each managed project;
  * the git commit-msg backstop scripts/git-hooks/strip-commit-attribution.sh,
    which silently strips the offending lines out of a commit message written
    directly with plain git (bypassing the runtime) at commit time;
  * the server check scripts/ci/attribution-check.py, the handoff check in
    hooks/require-pr-on-stop.sh, and the delivery script.

Keeping one detector means those paths can never drift.

Public functions:
  detect(text, exempt_provenance=False) -> str
                                 label of the first attribution form found,
                                 or "" when the text is clean
  strip(text) -> str             the text with every attribution line removed
                                 and the trailing separator it left behind
                                 cleaned up
  detect_in_payload(payload) -> str
                                 the hook path: reads a PreToolUse payload and
                                 scans only the fields that carry authored
                                 text for the tool in question

CLI: attribution-detect.py detect reads text on stdin and prints the label
(one line) when attribution is found, prints nothing when clean, always exits
0. attribution-detect.py detect-payload reads the PreToolUse JSON payload on
stdin instead of bare text. attribution-detect.py strip reads text on stdin
and writes the cleaned text on stdout. Input is read as bytes, so text in any
encoding is judged, and strip writes untouched bytes back unchanged. A
nonzero exit signals a usage error or a crash, never a detection result, so
callers distinguish "clean" from "broke" by exit status and treat "broke" as
a failed gate.

Determinism: pure Python stdlib, no network, no model calls.

Self-avoidance: every pattern below is written so that this source file does
not itself match it (a regex metacharacter always sits where the literal text
would have to continue). That keeps the guard from denying edits to its own
detector.
"""

import json
import os
import re
import shlex
import stat
import sys

# Names that identify a coding agent or its vendor in an attribution line.
# Word boundaries on both sides are load bearing: without them "example.com"
# contains an agent name and every human co-author trailer is a false deny.
_AGENT = (
    r"\b(?:Claude|Anthropic|Codex|OpenAI|ChatGPT|GPT-[0-9]|Copilot|Cursor"
    r"|Devin|Gemini|Aider|Windsurf|AI assistant|AI agent|LLM)\b"
)

# A trailer normally opens a line, but inside a shell command it opens a
# quoted argument instead (git commit -m "Co-Authored-By: ..."), so a quote
# counts as a line start for detection. Stripping stays line oriented.
_LINE_START = r"(?:^|[\"'`])[ \t>*_-]*"

# One "label|pattern" pair per attribution form. Add a new form as its own
# entry; do not fold forms into a shared monolith regex. All matching is
# case-insensitive and line-oriented: a match marks the whole physical line
# for removal, which is the shape attribution always takes.
# A repository content path (an issue, a pull request, a release, a
# documentation page, and the other paths a citation legitimately links to)
# is not a product landing page, even under a vendor's own org on GitHub.
# The unnarrowed agent-product-link pattern denied a descriptive citation of
# a GitHub issue in the vendor's own repository, because its
# URL check only looked for the vendor's name anywhere in the link, not at
# whether the link actually pointed at repository content.
_REPO_CONTENT_PATH = (
    r"(?:issues|pull|pulls|releases|blob|tree|wiki|discussions|commit|compare|docs)/"
)

# A product link points at the vendor, so the link's host decides, not a
# vendor word anywhere in the URL. An earlier form
# denied `## sources` citations of third-party pages (a nasdaq.com press
# release, a constellationr.com article) whose paths carried the product's
# name. A link denies only when its host is a vendor domain or subdomain
# (the vendor word as a whole domain label, then the top-level domain and the
# end of the host), or when it is on github.com and names the vendor outside
# a repository content path. Userinfo before the host is skipped, so it does
# not hide a vendor host. Every repeat is bounded or stops at a delimiter, so
# the pattern stays linear on untrusted text.
_LINK_USERINFO = r"(?:[^/@?#)\s\]\[]{0,64}@)?"
_VENDOR_HOST = (
    r"(?:[a-z0-9-]{1,63}\.){0,8}(?:anthropic|claude|openai)\.[a-z]{2,24}" r"(?=[/:?#)])"
)
_GITHUB_VENDOR_URL = (
    r"(?:www\.)?github\.com(?=[/)])(?!/[^/)\s]+/[^/)\s]+/" + _REPO_CONTENT_PATH + r")"
    r"[^][)\s]*(?:anthropic|claude|openai|/features/copilot)"
)

# A footer or byline opens its line, follows the end of a sentence or clause
# (". ", ", ", "; "), or opens a bracket, quote or separator, with at most a
# few marks (an emoji, markdown emphasis) in front. A technical mention
# inside a sentence ("parse config files created by <tool>", "an adapter
# built with <SDK>") is none of these and is not attribution (both were once denied).
_FOOTER_OPENING = r"(?:^|(?<=[.!?,;])[ \t]+|[(\[\"'`|])[^A-Za-z0-9\n]{0,8}?"

# In a trailer the agent is named in the name part: an address inside <...>
# is not a name, and neither is a word inside an address, so a person's
# address at a vendor's domain is not a trailer naming the vendor (PR 294
# review, P2). A vendor noreply address is still denied by its own form.
_TRAILER_NAME = re.compile(r"(?<![@.])" + _AGENT, re.IGNORECASE)


class _Found:
    """The span of one attribution match, shaped like a re.Match."""

    __slots__ = ("_start", "_end", "_text")

    def __init__(self, start, end, text):
        self._start, self._end, self._text = start, end, text

    def start(self):
        return self._start

    def end(self):
        return self._end

    def group(self):
        return self._text[self._start : self._end]


class _TrailerPattern:
    """A trailer keyword, then an agent name later on its line.

    Matched in two steps rather than one regex, because a keyword followed
    by an unbounded run and a name rescans the rest of the line from every
    repeated keyword, which is quadratic on a long line and fails open past
    the hook's timeout. The name is searched once per stretch of line (up to
    a `<` or the line's end), so the work stays linear. finditer and search
    take the same arguments as a compiled pattern's.
    """

    def __init__(self, keyword):
        self._keyword = re.compile(_LINE_START + keyword, re.IGNORECASE | re.MULTILINE)

    def finditer(self, text, pos=0, endpos=None):
        endpos = len(text) if endpos is None else min(endpos, len(text))
        covered = -1
        for opened in self._keyword.finditer(text, pos, endpos):
            stop = text.find("\n", opened.end(), endpos)
            stop = endpos if stop < 0 else stop
            bracket = text.find("<", opened.end(), stop)
            stop = stop if bracket < 0 else bracket
            if stop == covered:
                continue
            covered = stop
            last = None
            for last in _TRAILER_NAME.finditer(text, opened.end(), stop):
                pass
            if last is not None:
                yield _Found(opened.start(), last.end(), text)

    def search(self, text, pos=0, endpos=None):
        return next(self.finditer(text, pos, endpos), None)


# Every pattern below runs on untrusted text in a hook with a 3 second
# timeout, and a timeout fails open, so each one is linear: no unbounded
# repeat is followed by a pattern that can backtrack into it from many
# starting points (the link text and address stop at the next bracket).
_PATTERNS = (
    ("co-author-trailer", _TrailerPattern(r"Co[- ]?Authored[- ]?By:")),
    ("assisted-by-trailer", _TrailerPattern(r"(?:Assisted|Generated)[- ]?By:")),
    (
        "generated-with",
        _FOOTER_OPENING
        + r"(?:Generated|Created|Authored|Written|Built)\s+(?:with|by)\b.{0,40}?"
        + _AGENT,
    ),
    ("agent-vendor-email", r"noreply@(?:anthropic|openai)\.com"),
    ("session-permalink", r"claude\.(?:ai|com)/(?:code|share|chat)/[A-Za-z0-9_-]+"),
    ("session-trailer", _LINE_START + r"(?:Claude|Codex|Agent)[- ]Session:"),
    (
        "agent-product-link",
        r"\[[^][\n]*(?:Claude Code|Claude|Codex|Copilot)[^][\n]*\]"
        r"\(https?://"
        + _LINK_USERINFO
        + r"(?:"
        + _VENDOR_HOST
        + r"|"
        + _GITHUB_VENDOR_URL
        + r")"
        r"[^][)\s]*\)",
    ),
    ("robot-byline", "\U0001f916"),
)

_COMPILED = tuple(
    (
        label,
        re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        if isinstance(pattern, str)
        else pattern,
    )
    for label, pattern in _PATTERNS
)

# The one place a session link is metadata rather than credit: the
# `session-link:` frontmatter key that the docs-provenance-frontmatter rule
# requires on agent-written docs files (the provenance-exception section of
# the no-agent-attribution rule). The exception covers the frontmatter of a
# Markdown file under a docs/ directory (or a .docs/ directory, where a
# project keeps internal work product, or a developer-docs/ directory, where
# an extracted public repository keeps its own) and nothing else, so it is decided on
# the payload path, where the target file is known: detect_in_payload exempts
# the session-permalink form on such a line of a Write or Edit to such a file.
# Bare text (a commit message, a pull request body, a handoff comment) is
# never docs frontmatter, so detect() exempts nothing unless a caller that
# knows better asks it to. strip() never honours it: it runs on commit
# messages, which never legitimately carry the key.
#
# 2026-09-23, review cluster F2: detect() used to exempt every such line by
# default, so the commit-msg backstop (which gates strip on detect) let a
# `session-link:` permalink through a plain git commit, and the guard let one
# through a gh pr body, an MCP message, and a handoff comment.
_PROVENANCE_LINE = re.compile(r"^[ \t]*session-link:", re.IGNORECASE)
_PROVENANCE_EXEMPT = frozenset({"session-permalink"})
_DOCS_SEGMENTS = frozenset({"docs", ".docs", "developer-docs"})
_SESSION_PERMALINK = next(
    pattern for label, pattern in _COMPILED if label == "session-permalink"
)


def _docs_markdown_path(path):
    """True for a Markdown file under a docs/, .docs/ or developer-docs/
    directory, empty segments aside. .docs/ holds internal work product
    (ADR-0003) and carries the same provenance frontmatter as docs/.

    Split rather than matched: the regex this replaces nested a repeat and
    went quadratic on a path of many slashes. The segment is matched whole:
    mydocs/, .docsx/ and developer-docs-old/ are not docs directories.
    """
    parts = path.lower().split("/")
    if len(parts[-1]) <= len(".md") or not parts[-1].endswith(".md"):
        return False
    last_empty = max(
        (index for index, part in enumerate(parts) if not part), default=-1
    )
    return any(part in _DOCS_SEGMENTS for part in parts[last_empty + 1 : -1])


def _without_provenance_lines(text):
    return "\n".join(
        line for line in text.split("\n") if not _PROVENANCE_LINE.match(line)
    )


def _frontmatter_end(lines):
    """Index of the closing --- of a leading frontmatter block, else -1."""
    if not lines or lines[0].strip() != "---":
        return -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index
    return -1


def _without_frontmatter_provenance(document):
    """A whole file's text with permitted session permalinks removed.

    A line is exempt only when it opens with the key and lies strictly
    between the file's leading --- fences; other text is kept. An Edit is
    judged in its landed file by _detect_authored_document instead.
    """
    lines = document.split("\n")
    end = _frontmatter_end(lines)
    return "\n".join(
        _SESSION_PERMALINK.sub("", line)
        if 0 < index < end and _PROVENANCE_LINE.match(line)
        else line
        for index, line in enumerate(lines)
    )


def _docs_markdown_target(tool_input):
    path = tool_input.get("file_path")
    return isinstance(path, str) and _docs_markdown_path(path)


# The exemption is off in a public repository (the public-repository section
# of the no-agent-attribution rule): a session link in a published tree is
# a link to the agent transcript whoever reads it as metadata. A checkout is
# public when the environment says so (STRATARC_PUBLIC=1) or when it is listed
# in <source root>/projects-root/public-targets.json by its directory name,
# its main worktree's directory name or its origin's owner/repo slug. That
# file and that identity are what hooks/lib/public-targets.py defines; the
# logic is reimplemented here rather than imported because a carried copy
# of this guard ships this file alone, and the public-targets tests hold the
# two to the same answers. The source root is STRATARC_SOURCE when set, else
# LLM_ROOT, else none, as hooks/lib/prompt-capture.py resolves it. A missing
# list names nobody, so the exemption then holds as before. A list that exists and
# cannot be read fails closed: the exemption is off everywhere until the
# file is fixed, the same refusal the reconciler and the project sync make.
_PUBLIC_VARIABLE = "STRATARC_PUBLIC"
_SOURCE_VARIABLES = ("STRATARC_SOURCE", "LLM_ROOT")
_GITHUB_ORIGINS = (
    re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?"),
    re.compile(r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?"),
    re.compile(r"ssh://git@github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?"),
)


def _source_root():
    """STRATARC_SOURCE, else LLM_ROOT, else "" (no file-location fallback)."""
    for name in _SOURCE_VARIABLES:
        override = os.environ.get(name, "")
        if override:
            return os.path.expanduser(override)
    return ""


def _public_targets():
    """The entries of the public-target list: a frozenset, or None when the
    file exists and cannot be read or has another shape (a missing file is
    the empty frozenset). Mirrors load_public_targets in public-targets.py."""
    root = _source_root()
    if not root:
        return frozenset()
    path = os.path.join(root, "projects-root", "public-targets.json")
    if not os.path.isfile(path):
        return frozenset()
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    entries = document.get("targets") if isinstance(document, dict) else document
    if not isinstance(entries, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in entries
    ):
        return None
    return frozenset(entry.strip() for entry in entries)


def _git_toplevel(path, cwd):
    """The nearest ancestor of path holding a .git entry (a directory, or
    the file a linked worktree carries), else None. Walked rather than asked
    of git: the file may not exist yet, the hook runs under a timeout, and
    a path of many segments is still linear."""
    if "\0" in path:
        return None
    current = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.exists(os.path.join(parent, ".git")):
            return parent
        current = parent


def _git_common_dir(toplevel):
    """The shared .git directory of the checkout at toplevel, else None.
    Mirrors git_common_dir in public-targets.py: a .git directory is its own
    common directory; a linked worktree's .git file names its gitdir, whose
    commondir file names the shared one."""
    entry = os.path.join(toplevel, ".git")
    if os.path.isdir(entry):
        gitdir = entry
    elif os.path.isfile(entry):
        try:
            with open(entry, encoding="utf-8") as handle:
                first = handle.read().splitlines()[0]
        except (OSError, ValueError, IndexError):
            return None
        if not first.startswith("gitdir:"):
            return None
        gitdir = os.path.normpath(
            os.path.join(toplevel, first[len("gitdir:") :].strip())
        )
    else:
        return None
    common = os.path.join(gitdir, "commondir")
    if os.path.isfile(common):
        try:
            with open(common, encoding="utf-8") as handle:
                relative = handle.read().strip()
        except (OSError, ValueError):
            return None
        if relative:
            gitdir = os.path.normpath(os.path.join(gitdir, relative))
    return gitdir


def _origin_slug(common_dir):
    """owner/repo from the origin url in <common dir>/config when the origin
    is on GitHub, else None. Mirrors origin_url and origin_slug in
    public-targets.py."""
    try:
        with open(os.path.join(common_dir, "config"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, ValueError):
        return None
    in_origin = False
    url = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = (
                re.fullmatch(r'\[\s*remote\s+"origin"\s*\]', stripped) is not None
            )
            continue
        if in_origin and "=" in stripped:
            key, value = (part.strip() for part in stripped.split("=", 1))
            if key == "url":
                url = value
                break
    if url is None:
        return None
    for pattern in _GITHUB_ORIGINS:
        match = pattern.fullmatch(url)
        if match:
            return match.group(1)
    return None


def _checkout_is_public(toplevel, targets):
    """True when the checkout at toplevel is listed in targets by its own
    directory name, its main worktree's directory name or its origin slug.
    Mirrors is_public_checkout in public-targets.py."""
    if not targets:
        return False
    names = {os.path.basename(toplevel)}
    common = _git_common_dir(toplevel)
    slug = None
    if common is not None:
        if os.path.basename(common) == ".git":
            names.add(os.path.basename(os.path.dirname(common)))
        slug = _origin_slug(common)
    if names & targets:
        return True
    if slug is None:
        return False
    return slug.casefold() in {entry.casefold() for entry in targets if "/" in entry}


def _public_checkout(tool_input, cwd):
    """True when the target file sits in a public repository, or when the
    public-target list cannot be read (then no checkout is trusted as
    private)."""
    if os.environ.get(_PUBLIC_VARIABLE) == "1":
        return True
    targets = _public_targets()
    if targets is None:
        return True
    path = tool_input.get("file_path")
    if not isinstance(path, str):
        return False
    toplevel = _git_toplevel(path, cwd)
    return toplevel is not None and _checkout_is_public(toplevel, targets)


def _read_edit_file(file_path):
    if not isinstance(file_path, str):
        return None
    try:
        with open(file_path, "rb") as handle:
            existing = handle.read(_BODY_FILE_LIMIT + 1)
    except (OSError, ValueError):
        return None
    if len(existing) > _BODY_FILE_LIMIT:
        return None
    return existing.decode("utf-8", errors="replace")


def _edit_context(tool_input, existing):
    """Offsets for every replacement location in existing, else None."""
    old_string = tool_input.get("old_string")
    if not isinstance(old_string, str) or not old_string or existing is None:
        return None
    offsets = []
    start = 0
    while (index := existing.find(old_string, start)) >= 0:
        offsets.append(index)
        if not tool_input.get("replace_all"):
            break
        start = index + len(old_string)
    return offsets or None


# Flags _apply_edits keeps per character of the landed document.
# _TOUCHED: an edit wrote the character, or a deletion changed the one
# character beside it, which an anchor (^, \b) reads. _JOINED: a deletion
# joined this character to the next one.
_TOUCHED = 1
_JOINED = 2
# The one form whose \s+ can cross a line break.
_CROSS_LINE = frozenset({"generated-with"})
_ANY_FLAG = re.compile(rb"[^\x00]")
_TOUCHED_FLAG = re.compile(
    b"[" + re.escape(bytes([_TOUCHED, _TOUCHED | _JOINED])) + b"]"
)
_JOINED_FLAG = re.compile(b"[" + re.escape(bytes([_JOINED, _TOUCHED | _JOINED])) + b"]")


def _detect_authored_document(document, authored, exempt_provenance):
    """Detect attribution in the final document that the edits made.

    authored holds _apply_edits' flags. A match counts when it touches a
    character an edit wrote, even when the edit alone matches nothing (a
    name swapped into an existing trailer), or when it spans a deletion's
    join or touches a character whose context the deletion changed (a word
    removed from inside a name). Attribution the edits left untouched does
    not count. exempt_provenance applies the docs frontmatter exception.
    """
    exempt = bytearray(len(document))
    if exempt_provenance:
        lines = document.split("\n")
        end = _frontmatter_end(lines)
        offset = 0
        for index, line in enumerate(lines):
            if 0 < index < end and _PROVENANCE_LINE.match(line):
                for match in _SESSION_PERMALINK.finditer(line):
                    exempt[offset + match.start() : offset + match.end()] = b"\1" * len(
                        match.group()
                    )
            offset += len(line) + 1
    # Scan only the lines an edit touched. Every form but one is judged
    # within a line, and scanning the whole file let one pattern go
    # quadratic on a large file, past the hook's 3 second timeout, which
    # fails open. generated-with may break its line between the verb and
    # the preposition so it alone is also given the
    # nearest non-blank line on each side.
    for span in _authored_line_spans(document, authored):
        for label, pattern in _COMPILED:
            first, last = _widened(document, *span) if label in _CROSS_LINE else span
            for match in pattern.finditer(document, first, last):
                start, stop = match.start(), match.end()
                if not (
                    _TOUCHED_FLAG.search(authored, start, stop)
                    or _JOINED_FLAG.search(authored, start, stop - 1)
                ):
                    continue
                if label == "session-permalink" and all(exempt[start:stop]):
                    continue
                return label
    return ""


def _widened(document, start, stop):
    """start and stop moved out to the nearest non-blank line on each side."""
    while start > 0 and document[start - 1].isspace():
        start -= 1
    start = document.rfind("\n", 0, start) + 1
    while stop < len(document) and document[stop].isspace():
        stop += 1
    stop = document.find("\n", stop)
    return start, len(document) if stop < 0 else stop


def _authored_line_spans(document, authored):
    """(start, stop) of each run of whole lines holding flagged characters."""
    spans = []
    flagged = _ANY_FLAG.search(authored)
    while flagged:
        index = run_end = flagged.start()
        while run_end < len(authored) and authored[run_end]:
            run_end += 1
        start = document.rfind("\n", 0, index) + 1
        stop = document.find("\n", run_end)
        stop = len(document) if stop < 0 else stop
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(stop, spans[-1][1]))
        else:
            spans.append((start, stop))
        flagged = _ANY_FLAG.search(authored, max(run_end, stop))
    return spans


def _apply_edits(existing, edits):
    """(document, flags) once edits land in existing, else None.

    Each character the edits wrote is flagged _TOUCHED, and so is each
    character just outside a replacement whose one character of context the
    replacement changed, so an anchor (^, \\b) completed from outside the
    match counts. A deletion writes no character, so the character before
    its join is also flagged _JOINED, and a match spanning the join counts.
    Editing beside attribution that was already there, without changing the
    character next to it, flags none of it.

    None when the file could not be read or an edit cannot be placed, so the
    caller falls back to judging the replacement text alone.
    """
    if existing is None:
        return None
    authored = bytearray(len(existing))
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("new_string"), str):
            return None
        offsets = _edit_context(edit, existing)
        if offsets is None:
            return None
        old_string = edit["old_string"]
        replacement = edit["new_string"]
        parts = []
        flags = bytearray()
        placed = []
        start = 0
        for index in offsets:
            parts.extend((existing[start:index], replacement))
            flags.extend(authored[start:index])
            placed.append(len(flags))
            flags.extend(bytes([_TOUCHED]) * len(replacement))
            start = index + len(old_string)
        parts.append(existing[start:])
        flags.extend(authored[start:])
        existing = "".join(parts)
        authored = flags
        # The start and end of the file read as a newline would, so removing
        # the first or last line changes nothing a pattern reads there.
        for first in placed:
            stop = first + len(replacement)
            if first > 0:
                if not replacement:
                    authored[first - 1] |= _JOINED
                after = existing[first] if first < len(existing) else "\n"
                if after != old_string[0]:
                    authored[first - 1] |= _TOUCHED
            if stop < len(existing):
                before = existing[stop - 1] if stop > 0 else "\n"
                if before != old_string[-1]:
                    authored[stop] |= _TOUCHED
    return existing, authored


def _detect_write(tool_name, tool_input, cwd):
    """detect() for a file write, judged in the file it lands in.

    The replacement text alone is judged first. An Edit or MultiEdit is also
    judged in its target file once it lands, which catches attribution the
    edit completes around existing text; the docs frontmatter exception is
    applied only there, where the target file is known, and never in a
    public checkout. cwd resolves a relative file_path.
    """
    text = "\n".join(_strings(tool_input, _WRITE_FIELDS))
    label = detect(text)
    docs = _docs_markdown_target(tool_input) and not _public_checkout(tool_input, cwd)
    provenance_only = label in _PROVENANCE_EXEMPT and docs
    if tool_name == "Write":
        if provenance_only and isinstance(tool_input.get("content"), str):
            return detect(_without_frontmatter_provenance(tool_input["content"]))
        return label
    if tool_name not in ("Edit", "MultiEdit"):
        return label
    if label and not provenance_only:
        return label
    edits = [tool_input] if tool_name == "Edit" else tool_input.get("edits")
    if not isinstance(edits, list):
        return label
    landed = _apply_edits(_read_edit_file(tool_input.get("file_path")), edits)
    if landed is None:
        return label
    return _detect_authored_document(*landed, exempt_provenance=docs)


# Fields that carry newly authored text, by tool family.
_WRITE_FIELDS = ("content", "new_string", "new_source")
_MESSAGE_FIELDS = (
    "body",
    "commit_message",
    "message",
    "title",
    "description",
    "text",
    "content",
)

# Tools whose input is local bookkeeping that is never published, so their
# message-shaped fields (a todo item's "content", for instance) are not judged.
# The guard runs on every tool, which is what makes this list needed.
_LOCAL_TOOLS = frozenset({"TodoWrite"})

# A body file larger than this is not read: the hook runs under a 3 second
# timeout, and a message body is never this large. A larger one is denied as
# unverifiable, never scanned as empty.
_BODY_FILE_LIMIT = 1024 * 1024

# ---------------------------------------------------------------------------
# Shell commands.
#
# A Bash command is judged only on what it publishes: the arguments of each
# git, gh or glab invocation that writes (git commit, tag, notes, merge and
# friends; a gh pr, issue, release or gist verb that writes; a gh api call
# that is not a GET) and every body those arguments name (a --body-file, an
# -F file, a gh api --input file, a field's @file, stdin). An unrelated
# command in the same line (a grep for an attribution string) is not judged.
#
# The command is read the way the shell reads it: quotes, escapes, $'...',
# substitutions, arithmetic, heredocs and comments are decoded before any
# word is judged, so a quoted or escaped program name, a name the shell
# rebuilds from pieces, and a line hidden behind a misread heredoc are all
# seen. When a publishing invocation's full message cannot be known (a body
# built at run time, read from a pipe, or from a file that cannot be read,
# an alias, xargs), the command is denied as unverifiable rather than passed
# unchecked. An adversarial review found 31 shapes the regex
# prepass this replaces let attribution publish.
# ---------------------------------------------------------------------------

_UNVERIFIABLE = "unverifiable-body"

# gh verbs that only read. Any other verb on pr, issue, release, or gist
# (create, edit, comment, review, merge, close, and the rest) publishes.
# An earlier form treated every gh pr, gh issue, and gh api call was
# treated as publishing, so searching issues for an attribution string was
# denied although nothing was published.
_GH_READ_VERBS = frozenset(
    {"list", "ls", "view", "status", "diff", "checks", "checkout", "download"}
)
_GH_API_BODY_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
_GH_TEXT_SUBCOMMANDS = frozenset({"pr", "issue", "release", "gist"})
# gh's own commands that publish no message this check judges. A command
# outside this list and _GH_TEXT_SUBCOMMANDS is an alias or an extension,
# whose expansion the check cannot see.
_GH_OTHER_COMMANDS = frozenset(
    {
        "accessibility",
        "agent-task",
        "alias",
        "attestation",
        "auth",
        "browse",
        "cache",
        "co",
        "codespace",
        "completion",
        "config",
        "copilot",
        "cs",
        "ext",
        "extension",
        "extensions",
        "gpg-key",
        "help",
        "label",
        "licenses",
        "org",
        "preview",
        "project",
        "repo",
        "ruleset",
        "run",
        "search",
        "secret",
        "ssh-key",
        "status",
        "variable",
        "version",
        "workflow",
    }
)
# Per gh command family: flags whose value is published text, flags whose
# value names a body file, and other flags that take a value (so the value
# is not read as a positional argument).
_GH_FLAGS = {
    "pr": (
        {"--body", "--title", "--comment", "--subject", "-b", "-t", "-c"},
        {"--body-file", "-F"},
        {"-a", "-B", "-H", "-l", "-m", "-p", "-r", "-R", "-T", "-A", "-S", "-s", "-L"},
    ),
    "release": (
        {"--notes", "--title", "-n", "-t"},
        {"--notes-file", "-F"},
        {"-R", "--target", "--notes-start-tag", "--discussion-category"},
    ),
    "gist": ({"--desc", "-d"}, {"--add", "-a"}, {"-f", "--filename"}),
}
_GH_FLAGS["issue"] = _GH_FLAGS["pr"]
_GH_API_VALUE_FLAGS = frozenset(
    {"-X", "--method", "-H", "--header", "-q", "--jq", "-t", "--template"}
    | {"--cache", "-p", "--preview", "--hostname"}
    | _GH_API_BODY_FLAGS
)
_GLAB_FAMILIES = frozenset({"mr", "issue", "release"})
_GLAB_MESSAGE_FLAGS = frozenset(
    {"--description", "--title", "--message", "--notes", "-d", "-t", "-m", "-N"}
)
_GLAB_FILE_FLAGS = frozenset({"--notes-file", "-F"})

# git subcommands that write a message, with per subcommand: short options
# that take a value, which of them carry message text, which name a message
# file, and which run a command.
_GIT_AUTHORING = {
    "commit": ("mFCct", "m", "Ft", ""),
    "tag": ("mFu", "m", "F", ""),
    "notes": ("mFCc", "m", "F", ""),
    "merge": ("mFsX", "m", "F", ""),
    "commit-tree": ("mFp", "m", "F", ""),
    "revert": ("mXs", "", "", ""),
    "cherry-pick": ("mXs", "", "", ""),
    "rebase": ("xsXo", "", "", "x"),
}
_GIT_LONG_MESSAGE = frozenset({"--message", "--trailer"})
_GIT_LONG_FILE = frozenset({"--file", "--template"})
_GIT_LONG_EXEC = frozenset({"--exec"})
_GIT_LONG_VALUE = frozenset(
    {"--author", "--date", "--cleanup", "--reuse-message", "--reedit-message"}
    | {"--fixup", "--squash", "--local-user", "--strategy", "--strategy-option"}
    | {"--into-name", "--ref", "--onto", "--pathspec-from-file", "--mainline"}
)
_GIT_LONG_OPTIONS = (
    _GIT_LONG_MESSAGE | _GIT_LONG_FILE | _GIT_LONG_EXEC | _GIT_LONG_VALUE
)
_GIT_GLOBAL_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    | {"--super-prefix"}
)
# git's own commands. An unknown subcommand given a message flag may be an
# alias from the user's git config, whose expansion the check cannot see.
_GIT_KNOWN = frozenset(
    "add am annotate apply archive bisect blame branch bundle cat-file check-attr "
    "check-ignore check-mailmap check-ref-format checkout checkout-index cherry "
    "clean clone column commit-graph config count-objects credential describe "
    "diff diff-files diff-index diff-tree difftool fetch fetch-pack filter-branch "
    "fmt-merge-msg for-each-ref format-patch fsck gc grep hash-object help init "
    "instaweb interpret-trailers lfs log ls-files ls-remote ls-tree mailinfo "
    "mailsplit maintenance merge-base merge-file merge-tree mergetool mktag mktree "
    "mv name-rev pack-objects prune pull push range-diff read-tree reflog remote "
    "repack replace request-pull rerere reset restore rev-list rev-parse rm "
    "send-email shortlog show show-branch show-ref sparse-checkout stash status "
    "submodule switch symbolic-ref unpack-objects update-index update-ref var "
    "verify-commit verify-pack verify-tag version whatchanged worktree "
    "write-tree".split()
)
# Flags that carry a message on some publishing program, for judging a
# command whose program or subcommand the check cannot resolve.
_MESSAGE_FLAG_NAMES = frozenset(
    {"-m", "-F", "-b", "-t", "--message", "--file", "--body", "--body-file"}
    | {"--title", "--notes", "--notes-file", "--input", "-f", "--field"}
    | {"--raw-field", "--description"}
)
# Words that, as a subcommand, make an unresolved program a likely publisher.
_PUBLISHING_FIRST_WORDS = (
    _GH_TEXT_SUBCOMMANDS | {"api", "mr"} | frozenset(_GIT_AUTHORING)
)
# Programs whose further arguments come from somewhere the check cannot see.
_ARGUMENT_FEEDERS = frozenset(
    {"xargs", "parallel", "-exec", "-execdir", "-ok", "-okdir"}
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "yash", "busybox"})
# Words that open a command without being its program.
_PREFIX_WORDS = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "while", "until", "do", "time"}
    | {"builtin", "exec", "nohup", "noglob", "nocorrect", "command"}
)
# Programs that only read the files they name, so naming a body file does not
# change it before it is published.
_READ_ONLY_PROGRAMS = frozenset(
    {"cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "ls", "test"}
    | {"[", "[[", "stat", "file", "less", "more", "diff", "cmp", "shasum"}
    | {"sha256sum", "md5", "md5sum", "echo", "printf", "git", "gh", "glab"}
    | {"cd", "pushd", "popd", "true", "false", "jq", "wait"}
)
# Variables that set the editor git writes a message with.
_EDITOR_VARIABLES = frozenset({"GIT_EDITOR", "EDITOR", "VISUAL", "GIT_SEQUENCE_EDITOR"})
# A variable whose environment value is not what the command would expand.
_UNSAFE_VARIABLES = frozenset({"_", "PWD", "OLDPWD", "RANDOM", "LINENO", "SECONDS"})
# A publishing program named anywhere in some text, quotes and escapes aside.
_LOOSE_PUBLISHER = re.compile(r"(?<![\w.-])(?:gh|git|glab)(?![\w.-])")

# Quoting and substitution may nest this deep before the command is judged
# unreadable; a real command nests a few levels.
_MAX_NESTING = 40
# Command text inside a word (bash -c, eval) is followed at most this deep.
_NESTED_COMMAND_DEPTH = 3
_BLANKS = " \t\r"
_METACHARACTERS = frozenset(" \t\r\n;&|()<>")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NAME_AT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_PLAIN_RUN = re.compile(r"[^ \t\r\n;&|()<>\\'\"$`*?\[~]+")
_QUOTED_RUN = re.compile(r"[^\"\\$`]+")
_ANSI_RUN = re.compile(r"[^'\\]+")
_HEX = {"x": re.compile(r"[0-9A-Fa-f]{1,2}"), "u": re.compile(r"[0-9A-Fa-f]{1,4}")}
_HEX["U"] = re.compile(r"[0-9A-Fa-f]{1,8}")
_OCTAL = re.compile(r"[0-7]{1,2}")
_ANSI_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
_OPERATORS = (";;&", ";;", ";&", "&&", "||", "|&", "|", "&", ";")
_REDIRECTS = ("<<<", "<<-", "<<", "<>", "<&", "<", ">>", ">&", ">|", ">", "&>>", "&>")
_WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})
_APPEND_REDIRECTS = frozenset({">>", "&>>", "<>"})
_PIPES = frozenset({"|", "|&"})
_BOOLEAN = object()


class _ParseError(ValueError):
    """The command cannot be read the way the shell reads it."""


class _Word:
    """One shell word: its parts in order, and the commands it runs.

    Each part is (kind, value, raw): "lit" text the shell passes through
    unchanged once quotes and escapes are removed, "var" a variable named by
    value, "sub" a command substitution whose tokens are value, "tilde" a
    leading ~, and "expr" anything the check does not evaluate (arithmetic,
    a parameter expansion with operators, a process substitution).
    """

    __slots__ = ("parts", "subs", "quoted", "glob")

    def __init__(self):
        self.parts = []
        self.subs = []
        self.quoted = False
        self.glob = False

    @staticmethod
    def of(text):
        word = _Word()
        word.parts.append(("lit", text, text))
        return word

    def add(self, kind, value, raw):
        if kind == "lit" and self.parts and self.parts[-1][0] == "lit":
            self.parts[-1] = ("lit", self.parts[-1][1] + value, "")
        else:
            self.parts.append((kind, value, raw))

    def text(self):
        """The word as scanned: literal parts decoded, the rest as written."""
        return "".join(
            value if kind == "lit" else raw for kind, value, raw in self.parts
        )

    def literal(self):
        """The word's value when it holds no expansion, else None."""
        if all(kind == "lit" for kind, _, _ in self.parts):
            return "".join(value for _, value, _ in self.parts)
        return None

    def prefix(self):
        """The literal text before the word's first expansion."""
        head = []
        for kind, value, _ in self.parts:
            if kind != "lit":
                break
            head.append(value)
        return "".join(head)

    def tail(self, count):
        """The word with its first count literal characters removed."""
        rest = _Word()
        rest.subs, rest.quoted, rest.glob = self.subs, self.quoted, self.glob
        for kind, value, raw in self.parts:
            if count and kind == "lit":
                if count >= len(value):
                    count -= len(value)
                    continue
                value, count = value[count:], 0
            rest.parts.append((kind, value, raw))
        return rest


class _Lexer:
    """Tokens of a shell command: ("word", _Word), ("op", text), and
    ("redir", op, fd, target), where a heredoc's target is a dict holding
    its delimiter and, once read, its body as a _Word."""

    def __init__(self, text, depth, flags, index=0):
        if depth > _MAX_NESTING:
            raise _ParseError("quoting nests too deeply")
        self.text, self.index, self.depth = text, index, depth
        self.nest = 0
        self.pending = []
        self.flags = flags

    def _enter(self):
        self.nest += 1
        if self.depth + self.nest > _MAX_NESTING:
            raise _ParseError("quoting nests too deeply")

    def _leave(self):
        self.nest -= 1

    def _child(self, text=None):
        if text is None:
            return _Lexer(self.text, self.depth + self.nest + 1, self.flags, self.index)
        return _Lexer(text, self.depth + self.nest + 1, self.flags)

    def tokens(self, closing=False):
        """Every token up to the end, or up to an unmatched ) when closing.

        Inside case ... esac a ) ends a pattern, not a substitution, so it
        is kept as a command separator (763ae4b correctness pass: the ) of
        a case pattern closed $( early and hid the command after it).
        """
        text, tokens, parens, cases = self.text, [], 0, 0
        while self.index < len(text):
            char = text[self.index]
            if char in _BLANKS:
                self.index += 1
            elif text.startswith("\\\n", self.index):
                self.index += 2
            elif char == "#":
                end = text.find("\n", self.index)
                self.index = len(text) if end < 0 else end
            elif char == "\n":
                self.index += 1
                tokens.append(("op", "\n"))
                self._heredoc_bodies()
            elif char == ")" and parens == 0 and cases:
                tokens.append(("op", ";"))
                self.index += 1
            elif char == ")" and closing and parens == 0:
                self.index += 1
                self._heredoc_bodies()
                return tokens
            elif text.startswith("((", self.index):
                # An arithmetic command: its << is a shift, never a heredoc.
                self.index += 2
                word = _Word()
                raw = self._arithmetic(word)
                word.add("expr", None, "((" + raw + "))")
                tokens.append(("word", word))
            elif char in "()":
                parens += 1 if char == "(" else -1
                tokens.append(("op", char))
                self.index += 1
            elif self._redirect_ahead():
                tokens.append(self._redirect(None))
            elif char in ";&|":
                operator = next(
                    op for op in _OPERATORS if text.startswith(op, self.index)
                )
                tokens.append(("op", operator))
                self.index += len(operator)
            else:
                start = self.index
                word = self._word()
                digits = text[start : self.index]
                if digits.isdigit() and self._redirect_ahead():
                    tokens.append(self._redirect(digits))
                    continue
                starts = (
                    not tokens
                    or tokens[-1][0] == "op"
                    or (
                        tokens[-1][0] == "word"
                        and tokens[-1][1].literal() in _PREFIX_WORDS
                    )
                )
                if starts and word.literal() == "case":
                    cases += 1
                elif starts and word.literal() == "esac" and cases:
                    cases -= 1
                tokens.append(("word", word))
        if closing:
            raise _ParseError("a substitution is not closed")
        self._heredoc_bodies()
        return tokens

    def _redirect_ahead(self):
        text, index = self.text, self.index
        if index >= len(text):
            return False
        if text[index] == "&":
            return text.startswith("&>", index)
        return text[index] in "<>" and not text.startswith("(", index + 1)

    def _redirect(self, fd):
        text = self.text
        operator = next(op for op in _REDIRECTS if text.startswith(op, self.index))
        self.index += len(operator)
        while self.index < len(text) and (
            text[self.index] in _BLANKS or text.startswith("\\\n", self.index)
        ):
            self.index += 2 if text[self.index] == "\\" else 1
        if self.index >= len(text) or (
            text[self.index] in _METACHARACTERS and not self._substitution_ahead()
        ):
            raise _ParseError("a redirect has no target")
        target = self._word()
        if operator in ("<<", "<<-"):
            heredoc = {
                "delimiter": target.text(),
                "strip": operator == "<<-",
                "quoted": target.quoted,
                "body": None,
            }
            self.pending.append(heredoc)
            return ("redir", operator, fd, heredoc)
        return ("redir", operator, fd, target)

    def _substitution_ahead(self):
        return self.text[self.index] in "<>" and self.text.startswith(
            "(", self.index + 1
        )

    def _heredoc_bodies(self):
        text = self.text
        for heredoc in self.pending:
            lines, terminated = [], False
            while self.index < len(text):
                end = text.find("\n", self.index)
                end = len(text) if end < 0 else end
                line = text[self.index : end]
                self.index = min(end + 1, len(text))
                if heredoc["strip"]:
                    line = line.lstrip("\t")
                if line == heredoc["delimiter"]:
                    terminated = True
                    break
                lines.append(line)
            body = "".join(line + "\n" for line in lines)
            if not terminated:
                self.flags["unterminated"] += body
            if heredoc["quoted"]:
                heredoc["body"] = _Word.of(body)
            else:
                word = _Word()
                self._child(body)._double_quoted(word, None)
                heredoc["body"] = word
        self.pending = []

    def _word(self):
        text, word, start = self.text, _Word(), self.index
        while self.index < len(text):
            char = text[self.index]
            if self._substitution_ahead():
                self._process_substitution(word)
            elif char in _METACHARACTERS:
                break
            elif char == "\\":
                following = text[self.index + 1 : self.index + 2]
                if following == "\n":
                    self.index += 2
                    continue
                word.quoted = True
                word.add("lit", following or "\\", "")
                self.index += 2 if following else 1
            elif char == "'":
                end = text.find("'", self.index + 1)
                if end < 0:
                    raise _ParseError("a single quote is not closed")
                word.quoted = True
                word.add("lit", text[self.index + 1 : end], "")
                self.index = end + 1
            elif char == '"':
                self.index += 1
                word.quoted = True
                self._double_quoted(word, '"')
            elif char == "$":
                self._dollar(word, False)
            elif char == "`":
                self._backticks(word)
            elif char == "~" and self.index == start:
                end = self.index + 1
                while end < len(text) and not (
                    text[end] in _METACHARACTERS or text[end] in "/'\"\\$`"
                ):
                    end += 1
                word.add("tilde", text[self.index : end], text[self.index : end])
                self.index = end
            else:
                run = _PLAIN_RUN.match(text, self.index)
                if run:
                    # An unquoted { may brace-expand, and a word opening
                    # with = is a path lookup in zsh: either can expand to
                    # a program name the literal text does not show.
                    if "{" in run.group() or (self.index == start and char == "="):
                        word.glob = True
                    word.add("lit", run.group(), "")
                    self.index = run.end()
                else:
                    word.glob = word.glob or char in "*?["
                    word.add("lit", char, "")
                    self.index += 1
        return word

    def _double_quoted(self, word, terminator):
        """Read to terminator (None: the end, as in a heredoc body)."""
        text = self.text
        self._enter()
        while True:
            if self.index >= len(text):
                if terminator is None:
                    break
                raise _ParseError("a double quote is not closed")
            char = text[self.index]
            if char == terminator:
                self.index += 1
                break
            run = _QUOTED_RUN.match(text, self.index)
            if run:
                word.add("lit", run.group(), "")
                self.index = run.end()
            elif char == "\\":
                following = text[self.index + 1 : self.index + 2]
                if following == "\n":
                    self.index += 2
                elif following in ("$", "`", "\\") or (following == '"' and terminator):
                    word.add("lit", following, "")
                    self.index += 2
                else:
                    word.add("lit", "\\", "")
                    self.index += 1
            elif char == "$":
                self._dollar(word, True)
            elif char == "`":
                self._backticks(word)
            else:
                word.add("lit", char, "")
                self.index += 1
        self._leave()

    def _dollar(self, word, quoted):
        text, start = self.text, self.index
        following = text[start + 1 : start + 2]
        if text.startswith("$((", start):
            self.index = start + 3
            raw = self._arithmetic(word)
            word.add("expr", None, "$((" + raw + "))")
        elif following == "(":
            self.index = start + 2
            inner = self._child()
            tokens = inner.tokens(closing=True)
            self.index = inner.index
            word.subs.append(tokens)
            word.add("sub", tokens, text[start : self.index])
        elif following == "{":
            self.index = start + 2
            self._braced(word)
            raw = text[start : self.index]
            if _NAME.match(raw[2:-1]):
                word.add("var", raw[2:-1], raw)
            else:
                word.add("expr", None, raw)
        elif following == "'" and not quoted:
            self.index = start + 2
            word.quoted = True
            word.add("lit", self._ansi_c(), "")
        elif following == '"' and not quoted:
            self.index = start + 2
            word.quoted = True
            self._double_quoted(word, '"')
        else:
            name = _NAME_AT.match(text, start + 1)
            if name:
                word.add("var", name.group(), text[start : name.end()])
                self.index = name.end()
            elif following and following in "0123456789@*#?$!-":
                word.add("expr", None, text[start : start + 2])
                self.index = start + 2
            else:
                word.add("lit", "$", "")
                self.index = start + 1

    def _braced(self, word):
        """Skip a ${...} expansion, keeping the commands it runs."""
        text, depth = self.text, 1
        scratch = _Word()
        self._enter()
        while self.index < len(text):
            char = text[self.index]
            if char == "}":
                depth -= 1
                self.index += 1
                if not depth:
                    word.subs.extend(scratch.subs)
                    self._leave()
                    return
            elif char == "{":
                depth += 1
                self.index += 1
            elif char == "\\":
                self.index += 2
            elif char == "'":
                end = text.find("'", self.index + 1)
                if end < 0:
                    raise _ParseError("a single quote is not closed")
                self.index = end + 1
            elif char == '"':
                self.index += 1
                self._double_quoted(scratch, '"')
            elif char == "$":
                self._dollar(scratch, True)
            elif char == "`":
                self._backticks(scratch)
            else:
                self.index += 1
        raise _ParseError("a ${ expansion is not closed")

    def _arithmetic(self, word):
        """Read an arithmetic body up to its )), keeping the commands it runs."""
        text, start, depth = self.text, self.index, 0
        scratch = _Word()
        self._enter()
        while self.index < len(text):
            char = text[self.index]
            if char == "(":
                depth += 1
                self.index += 1
            elif char == ")":
                if depth:
                    depth -= 1
                    self.index += 1
                    continue
                if not text.startswith("))", self.index):
                    raise _ParseError("an arithmetic expansion is not closed")
                raw = text[start : self.index]
                self.index += 2
                word.subs.extend(scratch.subs)
                self._leave()
                return raw
            elif char == "$":
                self._dollar(scratch, True)
            elif char == "`":
                self._backticks(scratch)
            elif char == '"':
                self.index += 1
                self._double_quoted(scratch, '"')
            elif char == "'":
                end = text.find("'", self.index + 1)
                if end < 0:
                    raise _ParseError("a single quote is not closed")
                self.index = end + 1
            else:
                self.index += 1
        raise _ParseError("an arithmetic expansion is not closed")

    def _backticks(self, word):
        text, index, inner = self.text, self.index + 1, []
        while index < len(text):
            char = text[index]
            if char == "\\" and text[index + 1 : index + 2] in ("`", "\\", "$"):
                inner.append(text[index + 1])
                index += 2
            elif char == "`":
                break
            else:
                inner.append(char)
                index += 1
        else:
            raise _ParseError("a backquote is not closed")
        tokens = self._child("".join(inner)).tokens()
        raw = text[self.index : index + 1]
        self.index = index + 1
        word.subs.append(tokens)
        word.add("sub", tokens, raw)

    def _process_substitution(self, word):
        start = self.index
        self.index += 2
        inner = self._child()
        tokens = inner.tokens(closing=True)
        self.index = inner.index
        word.subs.append(tokens)
        word.add("expr", None, self.text[start : self.index])

    def _ansi_c(self):
        """Decode a $'...' string; the index starts after the opening quote."""
        text, out = self.text, []
        while self.index < len(text):
            char = text[self.index]
            if char == "'":
                self.index += 1
                return "".join(out)
            run = _ANSI_RUN.match(text, self.index)
            if run:
                out.append(run.group())
                self.index = run.end()
                continue
            following = text[self.index + 1 : self.index + 2]
            self.index += 2
            if following in _ANSI_ESCAPES:
                out.append(_ANSI_ESCAPES[following])
            elif following in _HEX:
                digits = _HEX[following].match(text, self.index)
                if digits:
                    self.index = digits.end()
                    code = int(digits.group(), 16)
                    out.append(chr(code) if code <= 0x10FFFF else "�")
                else:
                    out.append("\\" + following)
            elif following and following in "01234567":
                digits = _OCTAL.match(text, self.index)
                value = following + (digits.group() if digits else "")
                self.index += len(value) - 1
                out.append(chr(int(value, 8) & 0xFF))
            elif following == "c" and self.index < len(text):
                out.append(chr(ord(text[self.index]) & 31))
                self.index += 1
            else:
                out.append("\\" + following)
        raise _ParseError("a $' string is not closed")


def _lex(text, flags):
    return _Lexer(text, 0, flags).tokens()


class _Command:
    """One simple command: its words and its redirects."""

    __slots__ = ("words", "redirects")

    def __init__(self, words, redirects):
        self.words, self.redirects = words, redirects


def _commands(tokens):
    """Simple commands in order, each operator between them as a string."""
    items, words, redirects = [], [], []
    for token in tokens:
        if token[0] == "op":
            if words or redirects:
                items.append(_Command(words, redirects))
                words, redirects = [], []
            items.append(token[1])
        elif token[0] == "word":
            words.append(token[1])
        else:
            redirects.append(token[1:])
    if words or redirects:
        items.append(_Command(words, redirects))
    return items


class _State:
    """Where the shell stands: its directory (None when unknown), the
    variables the command set, the pushd stack, and whether the directory
    was changed by a command that may not have run."""

    __slots__ = ("cwd", "variables", "pushed", "outer", "conditional", "tainted")

    def __init__(self, cwd, variables=None, pushed=None, tainted=False):
        self.cwd = cwd
        self.variables = variables if variables is not None else {}
        self.pushed = pushed if pushed is not None else []
        self.outer = []
        self.conditional = False
        self.tainted = tainted

    def copy(self):
        return _State(self.cwd, dict(self.variables), list(self.pushed), self.tainted)

    def taint(self):
        """A command the check does not follow (eval, source, printf -v, a
        function call) may have set any variable: forget them all, and stop
        trusting the environment until a later assignment."""
        self.variables.clear()
        self.tainted = True


class _Invocation:
    """What one publishing invocation publishes: texts to scan, body files
    to read, and the reasons its message cannot be known in full."""

    __slots__ = ("strict", "texts", "files", "problems")

    def __init__(self, strict):
        self.strict, self.texts, self.files, self.problems = strict, [], [], []


class _Context:
    """Everything one Bash payload publishes, gathered before judging."""

    def __init__(self, command):
        self.command = command
        self.invocations = []
        self.writes = []
        self.mentions = []
        self.unknown_writes = False
        self.flags = {"unterminated": ""}
        # Functions the command defines: calling one may set any variable.
        self.functions = set()
        # Whether the simple command being judged sets an editor for git.
        self.editor_override = False

    def invocation(self, strict):
        found = _Invocation(strict)
        self.invocations.append(found)
        return found

    def fail(self, strict, problem):
        if strict:
            self.invocation(True).problems.append(problem)


def _loose(text):
    """True when text names a publishing program, quotes and escapes aside."""
    return bool(_LOOSE_PUBLISHER.search(re.sub(r"[\\'\"]", "", text)))


def _short(text, limit=60):
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _variable(name, state):
    if name in state.variables:
        return state.variables[name]
    if name in _UNSAFE_VARIABLES or state.tainted:
        return None
    return os.environ.get(name)


def _resolve(word, state):
    """The word's value as the shell would expand it, or None when unknown."""
    out = []
    for kind, value, _ in word.parts:
        if kind == "lit":
            out.append(value)
        elif kind == "tilde":
            out.append(os.path.expanduser(value))
        elif kind == "var":
            found = _variable(value, state)
            if found is None:
                return None
            out.append(found)
        elif kind == "sub":
            found = _substitution_output(value, state)
            if found is None:
                return None
            out.append(found)
        else:
            return None
    return "".join(out)


def _absolute(path, cwd):
    # A path holding NUL names no file, and the os calls on it raise.
    if "\0" in path:
        return None
    if os.path.isabs(path):
        return os.path.normpath(path)
    return None if cwd is None else os.path.normpath(os.path.join(cwd, path))


# Per judged command: each file is read once, and the text gathered from all
# reads is capped, so repeating a large substitution cannot push the judge
# past the hook's timeout (763ae4b correctness pass: 69 copies of a 1 MB
# $(cat f) took 9.7 s). Reset by _judge_command.
_READS = {}
_READ_BUDGET = [0]
_READ_TOTAL_LIMIT = 4 * _BODY_FILE_LIMIT


def _read_limited(path):
    """(text, problem) for a regular file read whole, within the size limits.

    Only a regular file is read: /dev/stdin is the detector's own (already
    drained) stdin, and a FIFO would block past the timeout.
    """
    if path in _READS:
        return _READS[path]
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            found = None, f"{path} is not a regular file"
        elif info.st_size > _BODY_FILE_LIMIT:
            found = None, f"{path} is larger than 1 MB"
        elif _READ_BUDGET[0] + info.st_size > _READ_TOTAL_LIMIT:
            found = None, "the command reads more body text than the check can judge"
        else:
            with open(path, "rb") as handle:
                data = handle.read(_BODY_FILE_LIMIT + 1)
            _READ_BUDGET[0] += len(data)
            if len(data) > _BODY_FILE_LIMIT:
                found = None, f"{path} is larger than 1 MB"
            else:
                found = data.decode("utf-8", errors="replace"), None
    except (OSError, ValueError):
        found = None, f"cannot read {path}"
    _READS[path] = found
    return found


def _substitution_output(tokens, state):
    """What $(...) prints, for the shapes that only print text: cat of
    literal files, of a heredoc or of a redirected file, $(< file), and
    echo; None for anything else."""
    items = [item for item in _commands(tokens) if item != "\n"]
    if len(items) != 1 or not isinstance(items[0], _Command):
        return None
    command = items[0]
    values = [_resolve(word, state) for word in command.words]
    if None in values or (values and values[0] not in ("cat", "echo")):
        return None
    if values and values[0] == "echo":
        return None if command.redirects else _printed("echo", values[1:])
    if any(value.startswith("-") for value in values[1:]):
        return None
    paths = [_absolute(value, state.cwd) for value in values[1:]]
    if not paths:
        # cat with no file, or a bare redirect: $(< file), $(cat <<'EOF' ...).
        source, found = _stdin(command, state, False)
        if source == "text":
            return None if found is None else found.rstrip("\n")
        if source != "file":
            return None
        paths = [found]
    elif command.redirects:
        return None
    out = []
    for path in paths:
        text = None if path is None else _read_limited(path)[0]
        if text is None:
            return None
        out.append(text)
    return "".join(out).rstrip("\n")


def _word_texts(words, state):
    """Each word as scanned: as written, its value after a =, and its
    expansion when the check can compute one."""
    out = []
    for word in words:
        text = word.text()
        out.append(text)
        if "=" in text:
            out.append(text.split("=", 1)[1])
        if word.literal() is None:
            value = _resolve(word, state)
            if value is not None and value != text:
                out.append(value)
    return out


def _all_strings(value):
    """Every string in a decoded JSON document, keys aside."""
    found, stack = [], [value]
    while stack and len(found) < 100000:
        item = stack.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _json_text(text):
    """A JSON request body, raw and with every string decoded: an escaped
    newline hides a line-anchored trailer from the raw text."""
    try:
        decoded = json.loads(text)
    except ValueError:
        return text
    return "\n".join([text] + _all_strings(decoded))


def _program_index(words):
    index = 0
    while index < len(words):
        value = words[index].literal()
        if value not in _PREFIX_WORDS:
            break
        if value == "command":
            following = words[index + 1].literal() if index + 1 < len(words) else None
            if following in ("-v", "-V"):
                return len(words)
        index += 1
    return index


def _options(args, value_flags, dash_stops, long_name=None):
    """(flag, value) for each option in args, (None, word) for a positional.

    A flag's value is a _Word, None when the value is missing, or _BOOLEAN
    for a flag that takes none. A short option cluster reads each letter
    until one takes a value, which is the rest of the word or the next word.
    With dash_stops, a next word that looks like a flag is not taken as a
    value (gh's flags that take a value differ per verb).
    """
    found, position = [], 0

    def value_at(position):
        if position >= len(args):
            return None, position
        head = args[position].prefix()
        if dash_stops and re.match(r"--?[A-Za-z]", head) and not re.search(r"\s", head):
            return None, position
        return args[position], position + 1

    while position < len(args):
        word = args[position]
        head = word.prefix()
        position += 1
        if head == "--" and word.literal() == "--":
            found.extend((None, rest) for rest in args[position:])
            break
        if head.startswith("--") and len(head) > 2:
            name, equals, _ = head.partition("=")
            name = long_name(name) if long_name else name
            if equals:
                found.append((name, word.tail(len(head.partition("=")[0]) + 1)))
            elif name in value_flags:
                value, position = value_at(position)
                found.append((name, value))
            else:
                found.append((name, _BOOLEAN))
        elif head.startswith("-") and len(head) > 1 and head[1] != "-":
            for offset in range(1, len(head)):
                flag = "-" + head[offset]
                if flag in value_flags:
                    rest = word.tail(offset + 1)
                    if rest.parts and (rest.parts[0][0] != "lit" or rest.parts[0][1]):
                        found.append((flag, rest))
                    else:
                        value, position = value_at(position)
                        found.append((flag, value))
                    break
                found.append((flag, _BOOLEAN))
        else:
            found.append((None, word))
    return found


def _message(invocation, flag, word, state):
    """Record a flag whose value is published text."""
    if word is _BOOLEAN:
        return
    if word is None:
        invocation.problems.append(f"{flag} has no value")
        return
    value = _resolve(word, state)
    if value is None:
        invocation.problems.append(
            f"the {flag} value is built at run time ({_short(word.text())})"
        )
        return
    invocation.texts.append(value)
    if "=" in value:
        invocation.texts.append(value.split("=", 1)[1])
        if flag == "--trailer":
            # git accepts key=value for a trailer and writes it key: value.
            invocation.texts.append(value.replace("=", ": ", 1))


def _expanded_arguments(invocation, options, state):
    """A positional argument built at run time can expand to flags ("$@",
    ${a[@]}, an unquoted $x holding -m...), so its message is unknown. One
    with a literal prefix (repos/o/r/$(...)) stays a positional."""
    for flag, word in options:
        if flag is not None or word.literal() is not None or word.prefix():
            continue
        value = _resolve(word, state)
        if value is None or value.startswith("-"):
            invocation.problems.append(
                f"an argument is built at run time ({_short(word.text())})"
            )


def _stdin(command, state, piped):
    """Where a simple command's stdin comes from: ("file", path), ("text",
    text), ("pipe", None), or ("none", None). A None path or text means the
    source is built at run time."""
    source = ("pipe", None) if piped else ("none", None)
    for operator, fd, target in command.redirects:
        if fd not in (None, "0"):
            continue
        if operator in ("<", "<>"):
            value = _resolve(target, state)
            source = ("file", None if value is None else _absolute(value, state.cwd))
        elif operator in ("<<", "<<-"):
            source = ("text", _resolve(target["body"], state))
        elif operator == "<<<":
            value = _resolve(target, state)
            source = ("text", None if value is None else value + "\n")
    return source


def _stdin_body(invocation, kind, stdin):
    source, value = stdin
    if source == "file":
        if value is None:
            invocation.problems.append("stdin is a file named at run time")
        else:
            invocation.files.append((value, kind))
    elif source == "text":
        if value is None:
            invocation.problems.append("stdin is a heredoc built at run time")
        else:
            invocation.texts.append(_json_text(value) if kind == "json" else value)
    elif source == "pipe":
        invocation.problems.append("the body is read from a pipe")
    else:
        invocation.problems.append("the body is read from stdin, which is not given")


def _body_file(invocation, flag, word, kind, cwd, state, stdin):
    """Record a flag whose value names a body file (- for stdin)."""
    if word is None or word is _BOOLEAN:
        invocation.problems.append(f"{flag} has no value")
        return
    value = _resolve(word, state)
    if value is None:
        invocation.problems.append(
            f"the {flag} path is built at run time ({_short(word.text())})"
        )
        return
    if value == "-":
        _stdin_body(invocation, kind, stdin)
        return
    if word.glob:
        invocation.problems.append(f"the {flag} path {value} is a glob")
        return
    path = _absolute(value, cwd)
    if path is None:
        invocation.problems.append(
            f"{value} is relative to a directory the command changes at run time"
        )
        return
    invocation.files.append((path, kind))


def _has_message_flag(words):
    for word in words:
        name = word.prefix().partition("=")[0]
        if name in _MESSAGE_FLAG_NAMES or name[:2] in ("-m", "-F", "-b"):
            return True
    return False


def _gh(args, cwd, state, stdin, extended, strict, context, seen):
    position = 0
    while position < len(args):
        head = args[position].literal()
        if head in ("-R", "--repo"):
            position += 2
        elif head is not None and head.startswith("--repo="):
            position += 1
        else:
            break
    rest = args[position:]
    if not rest:
        return
    family = _resolve(rest[0], state)
    key = ("gh", cwd, family, extended)
    if key in seen:
        return
    seen.add(key)
    if family is None:
        if _has_message_flag(rest):
            context.fail(strict, "the gh command is built at run time")
        return
    if family == "api":
        _gh_api(rest, cwd, state, stdin, extended, strict, context)
        return
    if family == "alias":
        if len(rest) > 1 and rest[1].literal() == "set":
            context.invocation(strict).texts.extend(_word_texts(rest[2:], state))
        return
    if family not in _GH_FLAGS:
        if family not in _GH_OTHER_COMMANDS and _has_message_flag(rest):
            context.fail(
                strict,
                f"gh {family} is an alias or extension whose expansion the check "
                "cannot see",
            )
        return
    message_flags, file_flags, value_flags = _GH_FLAGS[family]
    options = _options(
        rest[1:], message_flags | file_flags | value_flags | {"--repo"}, True
    )
    positionals = [word for flag, word in options if flag is None]
    verb = _resolve(positionals[0], state) if positionals else None
    if verb in _GH_READ_VERBS:
        return
    invocation = context.invocation(strict)
    if extended:
        invocation.problems.append(
            "xargs or find -exec adds arguments the check cannot see"
        )
    invocation.texts.extend(_word_texts(rest[1:], state))
    _expanded_arguments(invocation, options, state)
    for flag, word in options:
        if flag in message_flags:
            _message(invocation, flag, word, state)
        elif flag in file_flags:
            _body_file(invocation, flag, word, "text", cwd, state, stdin)
    if family == "gist" and verb in ("create", "new"):
        files = positionals[1:]
        for word in files:
            _body_file(invocation, "gist file", word, "text", cwd, state, stdin)
        if not files:
            _stdin_body(invocation, "text", stdin)


def _gh_api(rest, cwd, state, stdin, extended, strict, context):
    values = [_resolve(word, state) for word in rest]
    tokens = [
        word.text() if value is None else value for word, value in zip(rest, values)
    ]
    if not _gh_publishes(tokens):
        return
    invocation = context.invocation(strict)
    if extended:
        invocation.problems.append(
            "xargs or find -exec adds arguments the check cannot see"
        )
    invocation.texts.extend(_word_texts(rest[1:], state))
    options = _options(rest[1:], _GH_API_VALUE_FLAGS, True)
    _expanded_arguments(invocation, options, state)
    for flag, word in options:
        if flag in ("-f", "--raw-field"):
            _message(invocation, flag, word, state)
        elif flag in ("-F", "--field"):
            if word is None or word is _BOOLEAN:
                invocation.problems.append(f"{flag} has no value")
                continue
            value = _resolve(word, state)
            if value is None:
                invocation.problems.append(
                    f"the {flag} value is built at run time ({_short(word.text())})"
                )
                continue
            _, equals, field = value.partition("=")
            if equals and field.startswith("@"):
                _body_file(
                    invocation, flag, _Word.of(field[1:]), "text", cwd, state, stdin
                )
            else:
                invocation.texts.extend((value, field))
        elif flag == "--input":
            _body_file(invocation, flag, word, "json", cwd, state, stdin)


def _gh_publishes(tokens):
    """True when gh api (tokens from `api` on) can write."""
    args = tokens
    if "graphql" in args[1:]:
        # A GraphQL mutation writes whatever the HTTP method.
        return True
    # args runs to the end of the segment, so it may hold a later gh's flags
    # too; any method other than GET therefore counts, and a later GET never
    # makes an earlier POST read-only.
    methods, has_body = [], False
    for position, token in enumerate(args[1:], start=1):
        flag, _, attached = token.partition("=")
        if token in ("-X", "--method"):
            methods.append(args[position + 1] if position + 1 < len(args) else "")
        elif flag == "--method":
            methods.append(attached)
        elif token in _GH_API_BODY_FLAGS or flag in _GH_API_BODY_FLAGS:
            has_body = True
        elif token.startswith("-") and not token.startswith("--") and len(token) > 2:
            # A cluster of short flags, where a value may follow attached:
            # -XPOST, -fbody=x, -iXPOST.
            for offset, char in enumerate(token[1:], start=2):
                if char == "X":
                    methods.append(token[offset:])
                    break
                if char in "fF":
                    has_body = True
                    break
    if methods:
        # With an explicit GET, gh sends fields as query parameters.
        return any(method.upper() != "GET" for method in methods)
    return has_body


def _git_long_name(name):
    """A git long option, abbreviations expanded as git's parser allows."""
    if name in _GIT_LONG_OPTIONS or len(name) < 4:
        return name
    matches = [option for option in _GIT_LONG_OPTIONS if option.startswith(name)]
    return matches[0] if len(matches) == 1 else name


def _git(args, cwd, state, stdin, extended, strict, context, seen, nested):
    position, base, aliases, unknown_config = 0, cwd, {}, False
    editor = context.editor_override
    while position < len(args):
        head = args[position].literal()
        if head is None or not head.startswith("-"):
            break
        value = args[position + 1] if position + 1 < len(args) else None
        if head in _GIT_GLOBAL_VALUE:
            resolved = None if value is None else _resolve(value, state)
            if head == "-C":
                base = None if resolved is None else _absolute(resolved, base)
            elif head == "-c":
                if resolved is None:
                    unknown_config = editor = True
                elif resolved.lower().startswith("alias."):
                    name, _, expansion = resolved[len("alias.") :].partition("=")
                    aliases[name.lower()] = expansion
                elif resolved.lower().startswith(("core.editor", "sequence.editor")):
                    editor = True
            elif head == "--config-env":
                unknown_config = editor = True
            position += 2
        else:
            position += 1
    if position >= len(args):
        return
    subcommand = _resolve(args[position], state)
    rest = args[position + 1 :]
    # Only what changes how the invocation reads its message is in the key:
    # an unrelated -c value per invocation made a long command quadratic.
    key = (
        "git",
        base,
        subcommand,
        extended,
        tuple(sorted(aliases.items())),
        editor,
        unknown_config,
    )
    if key in seen:
        return
    seen.add(key)
    for _ in range(5):
        if subcommand is None or subcommand.lower() not in aliases:
            break
        expansion = aliases[subcommand.lower()]
        if expansion.startswith("!"):
            context.fail(strict, f"git alias {subcommand} runs a shell command")
            return
        try:
            words = shlex.split(expansion)
        except ValueError:
            words = []
        if not words:
            context.fail(strict, f"git alias {subcommand} cannot be read")
            return
        subcommand, rest = words[0], [_Word.of(word) for word in words[1:]] + rest
    if subcommand == "am":
        # git am commits each patch with the message the patch carries.
        invocation = context.invocation(strict)
        options = _options(rest, {"-S", "--gpg-sign", "-p", "-C"}, False)
        _expanded_arguments(invocation, options, state)
        control = {"--continue", "--skip", "--abort", "--quit", "--resolved"}
        control |= {"-r", "--show-current-patch", "--retry", "--allow-empty"}
        patches = [word for flag, word in options if flag is None]
        for word in patches:
            _body_file(invocation, "git am", word, "text", base, state, stdin)
        if not patches and not any(flag in control for flag, _ in options):
            _stdin_body(invocation, "text", stdin)
        return
    if subcommand is None or subcommand not in _GIT_AUTHORING:
        if _has_message_flag(rest) and (
            subcommand is None or subcommand not in _GIT_KNOWN or unknown_config
        ):
            context.fail(
                strict,
                f"git {subcommand or 'subcommand built at run time'} may be an alias "
                "whose expansion the check cannot see",
            )
        return
    values, messages, files, commands = _GIT_AUTHORING[subcommand]
    invocation = context.invocation(strict)
    if extended:
        invocation.problems.append(
            "xargs or find -exec adds arguments the check cannot see"
        )
    invocation.texts.extend(_word_texts(rest, state))
    value_flags = {"-" + char for char in values} | _GIT_LONG_OPTIONS
    options = _options(rest, value_flags, False, _git_long_name)
    _expanded_arguments(invocation, options, state)
    given, edits = False, False
    for flag, word in options:
        if flag is None:
            continue
        edits = edits or flag in ("-e", "--edit")
        if flag in _GIT_LONG_MESSAGE or flag[1:] in tuple(messages):
            given = given or flag != "--trailer"
            _message(invocation, flag, word, state)
        elif flag in ("--file", "--template") or flag[1:] in tuple(files):
            given = given or flag not in ("--template", "-t")
            _body_file(invocation, flag, word, "text", base, state, stdin)
        elif flag in _GIT_LONG_EXEC or flag[1:] in tuple(commands):
            script = None if word in (None, _BOOLEAN) else _resolve(word, state)
            if script is None:
                invocation.problems.append(f"the {flag} command is built at run time")
            else:
                _nested(script, state, context, nested, strict)
    if editor and (edits or not given):
        # An editor the command sets writes the message itself.
        invocation.problems.append("an editor the command sets writes the message")


def _glab(args, cwd, state, stdin, extended, strict, context, seen):
    if not args:
        return
    family = _resolve(args[0], state)
    key = ("glab", cwd, family, extended)
    if key in seen:
        return
    seen.add(key)
    if family == "api":
        # glab api takes gh api's flags for a request body.
        _gh_api(args, cwd, state, stdin, extended, strict, context)
        return
    if family not in _GLAB_FAMILIES:
        return
    options = _options(args[1:], _GLAB_MESSAGE_FLAGS | _GLAB_FILE_FLAGS, True)
    positionals = [word for flag, word in options if flag is None]
    if positionals and _resolve(positionals[0], state) in _GH_READ_VERBS:
        return
    invocation = context.invocation(strict)
    if extended:
        invocation.problems.append(
            "xargs or find -exec adds arguments the check cannot see"
        )
    invocation.texts.extend(_word_texts(args[1:], state))
    _expanded_arguments(invocation, options, state)
    for flag, word in options:
        if flag in _GLAB_MESSAGE_FLAGS:
            _message(invocation, flag, word, state)
        elif flag in _GLAB_FILE_FLAGS:
            _body_file(invocation, flag, word, "text", cwd, state, stdin)


def _nested(text, state, context, nested, strict):
    """Judge command text a word carries (bash -c, eval, an alias, a script
    on stdin) as the commands it runs."""
    if nested >= _NESTED_COMMAND_DEPTH:
        if _loose(text):
            context.fail(strict, "publishing commands nest too deeply to follow")
        return
    try:
        tokens = _lex(text, context.flags)
    except (_ParseError, RecursionError) as error:
        if _loose(text):
            context.fail(strict, f"a nested command cannot be read ({error})")
        return
    _analyze(tokens, state.copy(), context, nested + 1, strict)


def _change_directory(name, args, state, uncertain, conditional):
    """Move state the way cd, pushd or popd moves the shell. A move the check
    cannot follow leaves the directory unknown, so relative body paths after
    it are unverifiable rather than read from the wrong place."""
    if uncertain:
        state.cwd = None
        return
    values = [_resolve(word, state) for word in args]
    if None in values:
        state.cwd = None
        return
    if name == "popd":
        state.cwd = state.pushed.pop() if state.pushed and not values else None
        return
    operands, options_done = [], False
    for value in values:
        if not options_done and value == "--":
            options_done = True
        elif not options_done and len(value) > 1 and value.startswith("-"):
            if name != "cd" or not set(value[1:]) <= set("LPe@"):
                state.cwd = None
                return
        else:
            operands.append(value)
    if len(operands) > 1 or operands == ["-"] or (name == "pushd" and not operands):
        state.cwd = None
        return
    target = operands[0] if operands else os.environ.get("HOME")
    target = None if target is None else _absolute(target, state.cwd)
    if target is None or not os.path.isdir(target):
        state.cwd = None
        return
    if name == "pushd":
        state.pushed.append(state.cwd)
    state.cwd = target
    state.conditional = state.conditional or conditional


def _assign(state, word, unknown):
    matched = _ASSIGNMENT.match(word.prefix()).group()
    name = matched.rstrip("+=")
    value = None if unknown else _resolve(word.tail(len(matched)), state)
    if value is not None and matched.endswith("+="):
        before = _variable(name, state)
        value = None if before is None else before + value
    state.variables[name] = value


def _record_writes(command, state, context, piped):
    """Remember each file the command writes, so a body file it produces is
    judged by what is written into it, not by what is on disk now."""
    for operator, _, target in command.redirects:
        if operator not in _WRITE_REDIRECTS or isinstance(target, dict):
            continue
        value = _resolve(target, state)
        if operator == ">&" and value is not None and (value.isdigit() or value == "-"):
            continue
        path = None if value is None else _absolute(value, state.cwd)
        if path is None:
            context.unknown_writes = True
            continue
        context.writes.append(
            (
                os.path.realpath(path),
                operator in _APPEND_REDIRECTS,
                command,
                state.copy(),
                piped,
            )
        )


def _printed(program, args):
    """What echo or printf prints, when it is only its arguments; None when
    a format, an option or an escape could build other text (763ae4b
    correctness pass: printf and echo -e assembled a trailer from pieces).
    zsh's echo expands backslash escapes by default, so any backslash is
    unknown."""
    if program == "printf":
        if len(args) != 1 or "%" in args[0] or "\\" in args[0]:
            return None
        return args[0]
    if (args and args[0].startswith("-")) or any("\\" in arg for arg in args):
        return None
    return " ".join(args)


def _producer_output(command, state, piped):
    """What a command writes to its redirect target, for the programs that
    only print text (cat of a heredoc or literal files, echo, printf); None
    when the check cannot know."""
    words = command.words[_program_index(command.words) :]
    while words and _ASSIGNMENT.match(words[0].prefix()):
        words = words[1:]
    if not words:
        return ""
    values = [_resolve(word, state) for word in words]
    if None in values:
        return None
    program, args = os.path.basename(values[0]), values[1:]
    if program in ("echo", "printf"):
        return _printed(program, args)
    if program != "cat":
        return None
    files = [arg for arg in args if not arg.startswith("-")]
    out = []
    for arg in files:
        path = _absolute(arg, state.cwd)
        text = None if path is None else _read_limited(path)[0]
        if text is None:
            return None
        out.append(text)
    if not files or "-" in args:
        source, text = _stdin(command, state, piped)
        if source != "text" or text is None:
            return None
        out.append(text)
    return "\n".join(out)


def _shell(args, command, state, context, nested, strict, piped):
    """A shell program: judge the script it runs (-c text or stdin)."""
    skip = False
    for position, word in enumerate(args):
        if skip:
            skip = False
            continue
        value = word.literal()
        if value is None or value == "--" or not value[:1] in ("-", "+"):
            return
        if value.startswith("--"):
            continue
        if "c" in value[1:]:
            script = args[position + 1] if position + 1 < len(args) else None
            text = None if script is None else _resolve(script, state)
            if text is None:
                if script is not None and _loose(script.text()):
                    context.fail(strict, "a shell runs command text built at run time")
                return
            _nested(text, state, context, nested, strict)
            return
        skip = value in ("-o", "+o", "-O", "+O")
    source, text = _stdin(command, state, piped)
    if source == "text":
        if text is None:
            context.fail(strict, "a shell runs a heredoc built at run time")
        else:
            _nested(text, state, context, nested, strict)
    elif source == "pipe" and _loose(context.command):
        context.fail(strict, "a shell runs a script piped into it")


def _analyze(tokens, state, context, nested, strict):
    items = _commands(tokens)
    previous = None
    for position, item in enumerate(items):
        if (
            isinstance(item, _Command)
            and items[position + 1 : position + 3] == ["(", ")"]
            and len(item.words) in (1, 2)
            and item.words[-1].literal()
        ):
            # name() { ...; }: the body is judged where it is written, as if
            # it ran there, and each later call forgets every variable.
            context.functions.add(item.words[-1].literal())
            continue
        if isinstance(item, str):
            if item != "&&" and state.conditional:
                state.cwd, state.conditional = None, False
            if item == "(":
                state.outer.append(
                    (state.cwd, dict(state.variables), list(state.pushed))
                )
            elif item == ")":
                if state.outer:
                    state.cwd, state.variables, state.pushed = state.outer.pop()
                else:
                    state.cwd = None
            previous = item
            continue
        following = items[position + 1] if position + 1 < len(items) else None
        _analyze_command(item, state, context, nested, strict, previous, following)


def _analyze_command(command, state, context, nested, strict, previous, following):
    # Every substitution runs, whatever the command around it does.
    for word in command.words:
        for sub in word.subs:
            _analyze(sub, state.copy(), context, nested, strict)
    for _, _, target in command.redirects:
        word = target["body"] if isinstance(target, dict) else target
        for sub in word.subs if word is not None else ():
            _analyze(sub, state.copy(), context, nested, strict)
    piped_in = previous in _PIPES
    uncertain = piped_in or following in _PIPES or following == "&"
    conditional = previous in ("&&", "||")
    words = command.words
    index = _program_index(words)
    start = index
    while index < len(words) and _ASSIGNMENT.match(words[index].prefix()):
        index += 1
    _record_writes(command, state, context, piped_in)
    if index >= len(words):
        for word in words[start:index]:
            _assign(state, word, uncertain or conditional)
        return
    program = _resolve(words[index], state)
    name = None if program is None else os.path.basename(program)
    args = words[index + 1 :]
    context.editor_override = any(
        _ASSIGNMENT.match(word.prefix())
        and _ASSIGNMENT.match(word.prefix()).group().rstrip("+=") in _EDITOR_VARIABLES
        for word in words
    ) or any(
        state.variables.get(variable) is not None for variable in _EDITOR_VARIABLES
    )
    if name in context.functions or name in ("eval", "source", "."):
        # Run after its text is judged below; any variable may change.
        taint_after = True
    else:
        taint_after = False
    if name == "printf" and any(word.literal() == "-v" for word in args):
        state.taint()
    if name in ("cd", "pushd", "popd"):
        _change_directory(name, args, state, uncertain, conditional)
    elif name in ("export", "declare", "typeset", "local", "readonly"):
        for word in args:
            if _ASSIGNMENT.match(word.prefix()):
                _assign(state, word, uncertain or conditional)
    elif name in ("unset", "read", "mapfile", "readarray", "getopts", "for", "select"):
        for word in args:
            value = word.literal()
            if value and _NAME.match(value):
                state.variables[value] = None
    # Every git, gh and glab word is an invocation over the words after it,
    # so a wrapper in front (env, op run --, sudo, the credential launcher)
    # hides nothing. An invocation with the same key as an earlier one in
    # this command reads a subset of what the earlier one read, and is
    # skipped, which keeps a command of many invocations linear. The
    # wrapper's directory (--cwd, env -C) and any argument feeder (xargs,
    # find -exec) in front are tracked as the words go by.
    stdin, seen, first_invocation = None, set(), len(words)
    cwd, env, extended = state.cwd, False, False
    for position in range(index, len(words)):
        word = words[position]
        value = word.literal()
        base_name = _publisher(word, state)
        if base_name == "?":
            following_word = words[position + 1] if position + 1 < len(words) else None
            first = None if following_word is None else _resolve(following_word, state)
            if first in _PUBLISHING_FIRST_WORDS:
                context.fail(strict, "the program name is built at run time")
        elif base_name in ("git", "gh", "glab"):
            first_invocation = min(first_invocation, position)
            if stdin is None:
                stdin = _stdin(command, state, piped_in)
            rest = words[position + 1 :]
            if base_name == "gh":
                _gh(rest, cwd, state, stdin, extended, strict, context, seen)
            elif base_name == "git":
                _git(rest, cwd, state, stdin, extended, strict, context, seen, nested)
            else:
                _glab(rest, cwd, state, stdin, extended, strict, context, seen)
        target = None
        if value == "env":
            env = True
        elif value in _ARGUMENT_FEEDERS:
            extended = True
        elif value == "--cwd" or (env and value in ("-C", "--chdir")):
            target = words[position + 1] if position + 1 < len(words) else None
        elif value is not None and value.startswith(("--cwd=", "--chdir=")):
            target = word.tail(value.index("=") + 1)
        if target is not None:
            resolved = _resolve(target, state)
            cwd = None if resolved is None else _absolute(resolved, cwd)
    # A program other than a reader or the publisher itself, in front of the
    # first invocation, may change a file it names before the body is read.
    # The publisher's own arguments are not such a mention, so a wrapper
    # (op run, timeout, the credential launcher) does not taint its body.
    if name not in _READ_ONLY_PROGRAMS:
        for word in words[index + 1 : first_invocation]:
            value = _resolve(word, state)
            if value:
                context.mentions.append((state.cwd, value))
    # Shells and eval before the first publishing invocation run text as
    # commands; any other word that reads like a publishing command (python
    # -c, an alias body) is followed too, but only to find attribution.
    for position in range(index, first_invocation):
        word = words[position]
        value = word.literal()
        base_name = None if value is None else os.path.basename(value)
        if base_name in _SHELLS:
            _shell(
                words[position + 1 :], command, state, context, nested, strict, piped_in
            )
        elif value == "eval":
            values = [_resolve(arg, state) for arg in words[position + 1 :]]
            if None in values:
                if _loose(" ".join(arg.text() for arg in words[position + 1 :])):
                    context.fail(strict, "eval runs command text built at run time")
            else:
                _nested(" ".join(values), state, context, nested, strict)
        elif value in ("-S", "--split-string") and position + 1 < len(words):
            text = _resolve(words[position + 1], state)
            if text is not None:
                _nested(text, state, context, nested, strict)
        elif position > index:
            text = word.text()
            if (" " in text or "\n" in text) and _loose(text):
                _nested(text, state, context, nested, False)
    if taint_after:
        state.taint()


def _publisher(word, state):
    """The program a word names, "?" when it may expand to a name the check
    cannot see (a glob, a brace, zsh =name, a value built at run time), or
    None for an empty word. Resolved words count: a variable, a substitution
    or a ~ path naming git runs git (763ae4b correctness pass)."""
    value = _resolve(word, state)
    if value is None or word.glob:
        return "?"
    return os.path.basename(value) if value else None


def _body_text(path, kind, context):
    """(text, problem) for a body file: what the command writes into it when
    it produces the file itself, else what is on disk."""
    real = os.path.realpath(path)
    texts = []
    produced = [write for write in context.writes if write[0] == real]
    for _, append, command, state, piped in produced:
        output = _producer_output(command, state, piped)
        if output is None:
            return (
                "",
                f"the command writes {path} with a program the check cannot follow",
            )
        texts.append(output)
    if produced and not any(write[1] for write in produced):
        return "\n".join(texts), None
    if context.unknown_writes:
        return (
            "",
            f"the command writes to a path built at run time, which may be {path}",
        )
    name = os.path.basename(real)
    for cwd, value in context.mentions:
        if value.endswith(name):
            candidate = _absolute(value, cwd)
            if candidate is not None and os.path.realpath(candidate) == real:
                return "", f"another program in the command changes {path}"
    text, problem = _read_limited(path)
    if text is None:
        return "", problem
    texts.append(_json_text(text) if kind == "json" else text)
    return "\n".join(texts), None


def _judge_command(command, cwd):
    """The attribution label, an unverifiable-body reason, or "" for a Bash
    command, judged on what it publishes."""
    context = _Context(command)
    _READS.clear()
    _READ_BUDGET[0] = 0
    try:
        tokens = _lex(command, context.flags)
        _analyze(tokens, _State(cwd), context, 0, True)
    except (_ParseError, RecursionError) as error:
        if _loose(command):
            return f"{_UNVERIFIABLE}: the command cannot be read ({error})"
        return ""
    texts, problems, bodies = [], [], {}
    for invocation in context.invocations:
        texts.extend(invocation.texts)
        for path, kind in invocation.files:
            if (path, kind) not in bodies:
                bodies[(path, kind)] = _body_text(path, kind, context)
            text, problem = bodies[(path, kind)]
            texts.append(text)
            if problem and invocation.strict:
                problems.append(problem)
        if invocation.strict:
            problems.extend(invocation.problems)
    # The same text reached by several words is judged once, and a command
    # that gathers more than the check can scan inside its timeout is
    # unverifiable rather than scanned slowly.
    texts = list(dict.fromkeys(texts))
    if sum(len(text) for text in texts) > 2 * _READ_TOTAL_LIMIT:
        return (
            f"{_UNVERIFIABLE}: the command publishes more text than the check can judge"
        )
    label = detect("\n".join(texts))
    if label:
        return label
    if problems:
        return f"{_UNVERIFIABLE}: {problems[0]}"
    if _loose(context.flags["unterminated"]):
        return f"{_UNVERIFIABLE}: a heredoc that is never closed holds a publishing command"
    return ""


def detect(text, exempt_provenance=False):
    """Return the label of the first attribution form in text, else "".

    By default every line is judged, which is right for any text published as
    a message (a commit, a pull request, a comment) since it can never be docs
    frontmatter. exempt_provenance=True ignores the session-permalink form on
    every `session-link:` line, for a caller that already knows the text is
    the frontmatter of a Markdown file under docs/; detect_in_payload decides
    that itself and exempts only the lines that qualify.
    """
    if not text:
        return ""
    exempt_subject = None
    for label, pattern in _COMPILED:
        subject = text
        if exempt_provenance and label in _PROVENANCE_EXEMPT:
            if exempt_subject is None:
                exempt_subject = _without_provenance_lines(text)
            subject = exempt_subject
        if pattern.search(subject):
            return label
    return ""


def _line_is_attribution(line):
    for _, pattern in _COMPILED:
        if pattern.search(line):
            return True
    return False


def strip(text):
    """Return text with every attribution line removed.

    Attribution always occupies whole lines (a trailer, a footer, a byline),
    so removal is line-oriented: a line that matches any form is dropped
    whole. Removing a footer usually leaves an orphaned horizontal rule and a
    run of blank lines at the end, so those are cleaned up too. A trailing
    newline is preserved when the input had one, which is what a commit
    message file expects.
    """
    if not text:
        return text
    had_trailing_newline = text.endswith("\n")
    kept = [line for line in text.split("\n") if not _line_is_attribution(line)]
    # Drop blank lines and an orphaned rule left at the end by the removal.
    while kept and (
        kept[-1].strip() == ""
        or re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", kept[-1])
    ):
        kept.pop()
    result = "\n".join(kept)
    if had_trailing_newline and result:
        result += "\n"
    return result


def detect_in_payload(payload):
    """Return the attribution label for a PreToolUse payload, else "".

    Write, Edit, and NotebookEdit are scanned on their authored-text fields;
    a `session-link:` line is exempt from the session-permalink form only in
    a Write or Edit to a Markdown file under docs/, only when the line sits
    inside that file's leading frontmatter once the write lands, and never
    when the file's checkout is public (_public_checkout).
    Bash is judged on what the command publishes: the arguments of each
    git, gh or glab invocation that writes, and every body they name (gh
    --body-file, -F body=@<path>, --field body=@<path>, gh api --input, git
    commit -F or --file, stdin), relative to the payload's cwd. A publishing
    command whose full message cannot be known (a file that cannot be read,
    a body built at run time or piped in, an alias) returns an
    "unverifiable-body: <reason>" label, so it is denied rather than passed
    unchecked. Any other tool (a GitHub
    writer from any MCP server, for instance) is scanned on the message-shaped
    fields of its input, one level deep plus nested objects, so a pull request
    body or an issue comment is covered; local bookkeeping tools are skipped.
    """
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    tool_name = payload.get("tool_name") or ""

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return _detect_write(tool_name, tool_input, cwd)
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return ""
        return _judge_command(command, cwd)
    if tool_name in _LOCAL_TOOLS:
        return ""
    if not tool_name:
        # An unlabeled payload (a bare tool_input, as the tests and the
        # OpenCode bridge send) is judged on every text-bearing field.
        return detect("\n".join(_strings(tool_input, _WRITE_FIELDS + _MESSAGE_FIELDS)))
    return detect("\n".join(_strings(tool_input, _MESSAGE_FIELDS)))


def _strings(value, fields, depth=0):
    """Collect string values stored under fields, recursing into containers."""
    found = []
    if depth > 4:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                if key in fields:
                    found.append(item)
            else:
                found.extend(_strings(item, fields, depth + 1))
    elif isinstance(value, list):
        for item in value:
            found.extend(_strings(item, fields, depth + 1))
    return found


def _main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in ("detect", "detect-payload", "strip"):
        sys.stderr.write("usage: attribution-detect.py detect|detect-payload|strip\n")
        return 64
    # Bytes in, bytes out. A commit message in a legacy encoding is not UTF-8,
    # and a text-mode stdin raised UnicodeDecodeError on it, which the
    # commit-msg backstop read as clean (review cluster F2). surrogateescape
    # decodes every byte, so the ASCII attribution forms still match, and
    # strip writes each untouched byte back exactly as it came in.
    text = sys.stdin.buffer.read().decode("utf-8", errors="surrogateescape")
    if mode == "strip":
        sys.stdout.buffer.write(strip(text).encode("utf-8", errors="surrogateescape"))
        return 0
    if mode == "detect":
        label = detect(text)
    else:
        try:
            payload = json.loads(text or "{}")
        except ValueError:
            payload = {}
        label = detect_in_payload(payload)
    if label:
        print(label)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
