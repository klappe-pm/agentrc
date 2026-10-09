#!/usr/bin/env python3
"""Derive a resume card from a Claude Code session transcript.

A resume card answers one question: if I come back to this session in 24 hours,
what am I looking at? It carries the project, the branch, the goal, where the
work stopped, what it cost, and how stale it is.

Nothing here writes a state store. The transcript is already the continuously
updated record, so every field is derived on read and cannot drift from the
session it describes. These record types come from the runtime itself:

  ai-title    the runtime's own title for the session, used as the summary goal
  cost-state  totalCostUSD, duration, lines changed and the model list; its
              per-model token counts are not used for tokens, which come
              from assistant turns in every view
  last-prompt the most recent thing asked
  user        first prompt, cwd, gitBranch, timestamps
  assistant   the closing message, used as the "stopped at" line

Goal modes (rules/output-format-contract: the configured mode is the contract):

  summary   the runtime's ai-title record, falling back to clean
  clean     the first prompt, deterministically tidied
  original  the first prompt, verbatim

Every field derived from prompt or assistant text is redacted through the
shared SECRET_PATTERNS in lib/prompt-capture.py before it leaves this module,
per rules/captured-content-redaction and rules/token-shaped-values.

Usage:
    session-card.py card <transcript.jsonl> [--goal-mode MODE] [--json]
    session-card.py scan [--root DIR] [--since HOURS] [--project NAME] [--json]
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import importlib.util
import itertools
import json
import os
import pathlib
import re
import sys
from typing import Any, Iterator

GOAL_MODES = ("summary", "clean", "original")
DEFAULT_GOAL_MODE = "summary"

# Records whose last occurrence in the file is authoritative.
STATE_TYPES = ("ai-title", "cost-state", "last-prompt")

# One regex decides whether a line can contribute and what it is, without a
# json.loads on lines that cannot. Matching the raw bytes is what keeps a scan
# over large transcripts fast.
#
# The whitespace allowance is not decoration. Transcripts observed here are
# written compactly, so a plain '"type":"user"' substring happens to match, but
# any producer using a standard JSON encoder writes '"type": "user"' and every
# record would then be skipped in silence, yielding empty cards rather than an
# error. The cost of tolerating it is one regex search per line.
RECORD_TYPE_RE = re.compile(
    r'"type"\s*:\s*"(user|assistant|ai-title|cost-state|last-prompt)"'
)

_TYPE_RES: dict[str, re.Pattern[str]] = {
    name: re.compile(r'"type"\s*:\s*"' + re.escape(name) + r'"')
    for name in ("user", "assistant", *STATE_TYPES)
}


def is_type(line: str, name: str) -> bool:
    """Cheap, encoding-tolerant test for a record's type on the raw line."""
    pattern = _TYPE_RES.get(name)
    return bool(pattern and pattern.search(line))


# The four token counters a usage record carries, and the short names used for
# them throughout. This lives in one place on purpose: counting only the first
# two is the mistake that makes a dispatched agent look almost free, because
# nearly all of its input arrives as cache reads.
USAGE_KEYS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "in"),
    ("output_tokens", "out"),
    ("cache_read_input_tokens", "cache_read"),
    ("cache_creation_input_tokens", "cache_write"),
)


def empty_usage() -> dict[str, int]:
    """A zeroed usage total, with every counter present."""
    return {short: 0 for _, short in USAGE_KEYS}


def number(value: Any) -> float:
    """A recorded count or amount as a number, 0 for anything malformed.

    Counts arrive as ints in practice, but a float or a numeric string is the
    same count, and a bad value in one transcript must never take down a scan
    of every session.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def add_usage(totals: dict[str, int], usage: Any) -> dict[str, int]:
    """Add one usage record into a running total, ignoring anything malformed."""
    if isinstance(usage, dict):
        for source, short in USAGE_KEYS:
            totals[short] = totals.get(short, 0) + int(number(usage.get(source)))
    return totals


def usage_thinking(usage: Any) -> int:
    """Reasoning tokens from one usage record.

    Per-record they arrive nested under output_tokens_details, while cost-state
    reports them per model as thinkingTokens. One definition here so the card
    and the ledger cannot disagree about what effort means.
    """
    if not isinstance(usage, dict):
        return 0
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and "thinking_tokens" in details:
        return int(number(details.get("thinking_tokens")))
    return int(number(usage.get("thinking_tokens")))


def message_of(record: Any) -> dict[str, Any]:
    """A record's message as a dict, or an empty one when it is anything else."""
    message = record.get("message") if isinstance(record, dict) else None
    return message if isinstance(message, dict) else {}


_ANONYMOUS_TURNS = itertools.count()


def turn_key(record: dict[str, Any]) -> str:
    """The identity of the API turn an assistant record belongs to.

    The runtime writes one assistant record per content block of a single API
    turn (thinking, text, each tool_use), and every one of them carries the
    turn's whole usage. message.id names the turn; requestId groups the same
    records when an id is missing; uuid is the last resort, which makes a
    record with neither its own turn rather than merging it with another.
    """
    message = message_of(record)
    for value in (message.get("id"), record.get("requestId"), record.get("uuid")):
        if isinstance(value, str) and value:
            return value
    # A counter, never id(record): a freed record's address is reused, which
    # merged unrelated turns and made the count depend on memory layout.
    return f"record-{next(_ANONYMOUS_TURNS)}"


class TurnUsage:
    """Usage counted once per API turn, the last record of a turn winning.

    Summing per record inflated real totals about 2.5 times (1230 assistant
    records held 493 distinct turns in one session). The last record wins
    because a streamed turn can write a partial output count before its final
    one; the counts of one turn never need adding together.
    """

    def __init__(self) -> None:
        self._usage: dict[str, Any] = {}
        self._thinking: dict[str, int] = {}
        self._model: dict[str, str] = {}

    def add(self, record: dict[str, Any]) -> None:
        message = message_of(record)
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        key = turn_key(record)
        self._usage[key] = usage
        self._thinking[key] = usage_thinking(usage)
        model = message.get("model")
        self._model[key] = model if isinstance(model, str) and model else "unknown"

    def totals(self) -> tuple[dict[str, int], int]:
        totals = empty_usage()
        for usage in self._usage.values():
            add_usage(totals, usage)
        return totals, sum(self._thinking.values())

    def by_model(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        for key, usage in self._usage.items():
            add_usage(totals.setdefault(self._model[key], empty_usage()), usage)
        return {model: counts for model, counts in totals.items() if any(counts.values())}


def read_turns(path: pathlib.Path) -> TurnUsage:
    """Every assistant turn of one transcript that carries usage; empty when the file cannot be read."""
    turns = TurnUsage()
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return turns
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            record = loads(line)
            if record and record.get("type") == "assistant":
                turns.add(record)
    return turns


def usage_by_turn(path: pathlib.Path) -> tuple[dict[str, int], int]:
    """Usage totals and thinking tokens for one transcript, once per API turn.

    The one definition the card, the ledger and the budget reports share, so
    no two views can count the same transcript differently.
    """
    return read_turns(path).totals()


def usage_by_model(path: pathlib.Path) -> dict[str, dict[str, int]]:
    """Usage totals per model for one transcript, once per API turn.

    The same turns as usage_by_turn, split by each turn's message.model, so
    the per-model totals of a transcript sum to its usage_by_turn totals. A
    turn with no model is counted under "unknown"; a model whose turns carry
    no tokens at all (Claude Code's "<synthetic>" records) is left out.
    """
    return read_turns(path).by_model()


def subagent_transcripts(path: pathlib.Path) -> list[pathlib.Path]:
    """The dispatched agents' transcripts that belong to one session transcript."""
    folder = path.parent / path.stem / "subagents"
    if not folder.is_dir():
        return []
    return sorted(folder.glob("agent-*.jsonl"))


def compact_count(count: int) -> str:
    """Render a token count the way a person reads one."""
    count = int(count or 0)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def load_module(path: pathlib.Path, name: str):
    """Import a module from a path whose filename cannot be imported by name.

    Repository convention gives files kebab-case names, which are not valid
    identifiers, so every consumer of this module loads it this way. The
    sys.modules registration is required rather than tidy: a dataclass in the
    loaded module resolves its own module through sys.modules while it is being
    defined, and raises AttributeError on None when the entry is missing.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# How far back from the end of the file to look for the closing assistant text.
TAIL_LINES = 400

# Caps on text a card carries. A dispatched agent's first prompt is the whole
# briefing it was handed, which runs to thousands of characters, so a card that
# stored it raw would be unusable as a card and heavy to pass around.
GOAL_CAP = 160
TEXT_CAP = 600

# Freshness thresholds in hours.
LIVE_HOURS = 1
IDLE_HOURS = 24

_FILLER = re.compile(
    r"^(?:ok(?:ay)?|so|now|well|hey|please|pls|can you|could you|i need you to|"
    r"i want you to|lets|let's|go ahead and)\s+",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")


def _load_redactor():
    """Return the shared redact() from lib/prompt-capture.py.

    The filename carries a dash so it cannot be imported by name. Falling back
    to an identity function would let a token-shaped value reach a card, so a
    failure here is reported rather than silently passed through.
    """
    here = pathlib.Path(__file__).resolve().parent
    target = here / "prompt-capture.py"
    if not target.is_file():
        return lambda text: text
    try:
        return load_module(target, "_prompt_capture").redact
    except Exception:
        return lambda text: text


redact = _load_redactor()


@dataclasses.dataclass
class Card:
    """One session, reduced to what a person needs to pick it back up."""

    session_id: str
    transcript: str
    cwd: str
    project: str
    branch: str
    title: str
    goal: str
    first_prompt: str
    last_prompt: str
    stopped_at: str
    last_active: dt.datetime | None
    cost_usd: float
    cost_known: bool
    tokens: int
    # Reasoning tokens, per rules the closest thing the runtime records to
    # "effort". Counted separately and deliberately left out of tokens: it is
    # reported alongside the output count rather than in addition to it, so
    # adding it to the total would count the same work twice.
    thinking_tokens: int
    models: list[str]
    duration_ms: int
    lines_added: int
    lines_removed: int
    turns: int
    status: str
    # Provenance. A dispatched agent runs in its own transcript under the
    # parent session, and its meta file carries the tool_use id of the call
    # that spawned it. Those two fields are what make the ledger traversable.
    kind: str = "session"
    parent_session_id: str = ""
    tool_use_id: str = ""
    agent_type: str = ""
    requested_model: str = ""
    spawn_depth: int = 0
    # When the dollars were last recorded. cost-state carries no timestamp,
    # so this is the last timestamped record written before the last
    # cost-state; None when no cost-state exists. The live figure is the
    # status line's, from the runtime payload.
    cost_as_of: dt.datetime | None = None

    @property
    def short_id(self) -> str:
        return self.session_id[:8] if self.session_id else "-"

    @property
    def age(self) -> str:
        return humanize_age(self.last_active)

    @property
    def resume_command(self) -> str:
        """The exact command that reopens this session.

        Scoped to the session's own directory per rules/command-cwd-scoping:
        claude has no directory flag, so the cd is part of the command.
        """
        if not self.session_id:
            return ""
        if self.cwd:
            return f"cd {self.cwd} && claude --resume {self.session_id}"
        return f"claude --resume {self.session_id}"

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["short_id"] = self.short_id
        data["age"] = self.age
        data["resume_command"] = self.resume_command
        data["last_active"] = self.last_active.isoformat() if self.last_active else None
        data["cost_as_of"] = self.cost_as_of.isoformat() if self.cost_as_of else None
        return data


def humanize_age(when: dt.datetime | None) -> str:
    """Render a timestamp as the coarse age a person actually reads."""
    if when is None:
        return "unknown"
    delta = dt.datetime.now(dt.timezone.utc) - when
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 14:
        return f"{days}d ago"
    return f"{days // 7}w ago"


def freshness(when: dt.datetime | None) -> str:
    """Classify a session as live, idle, or stale."""
    if when is None:
        return "stale"
    hours = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600
    if hours < LIVE_HOURS:
        return "live"
    if hours < IDLE_HOURS:
        return "idle"
    return "stale"


def clean_prompt(text: str, limit: int = 0) -> str:
    """Tidy a prompt into a goal line without changing what it says.

    Deterministic and local. Collapses whitespace, drops a leading filler
    phrase, downcases text shouted in caps, and trims on a word boundary.
    No model call runs here, so this never adds latency to a status line.
    """
    text = _WHITESPACE.sub(" ", (text or "").strip())
    if not text:
        return ""
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        text = text.lower()
    text = _FILLER.sub("", text).strip()
    if text:
        text = text[0].upper() + text[1:]
    return truncate(text, limit) if limit else text


def truncate(text: str, limit: int) -> str:
    """Cut to limit characters on a word boundary, with an ellipsis."""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:") + "…"


def text_of(message: Any) -> str:
    """Flatten a message content field to its plain text."""
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def is_real_prompt(text: str) -> bool:
    """Reject synthetic user records so the goal is something a person typed."""
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        return False
    for marker in (
        "<system-reminder",
        "<command-name",
        "<local-command",
        "Caveat: The messages below",
        "[Request interrupted",
    ):
        if stripped.startswith(marker):
            return False
    return True


def _parse_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def loads(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def read_transcript(path: pathlib.Path) -> dict[str, Any]:
    """Scan one transcript and return the raw facts a card is built from.

    One pass over the file. Lines that cannot contribute are rejected by a
    substring test before any JSON parsing, and the last TAIL_LINES are kept
    so the closing assistant message can be recovered without a second read.
    """
    facts: dict[str, Any] = {
        "session_id": "",
        "cwd": "",
        "branch": "",
        "title": "",
        "first_prompt": "",
        "last_prompt": "",
        "stopped_at": "",
        "cost": None,
        "cost_as_of": None,
        "last_ts": None,
        "turns": 0,
        "usage": empty_usage(),
        "thinking": 0,
        "models": [],
    }
    tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
    turn_usage = TurnUsage()
    # The timestamp of the most recent record in file order, which dates the
    # cost-state records that carry no timestamp of their own.
    last_seen: dt.datetime | None = None
    # The newest line the type filter skips (system, progress and the like)
    # that carries a timestamp and follows last_seen. It is parsed only when a
    # cost-state arrives, so the skipped lines stay unparsed on the fast path.
    skipped_stamp_line: str | None = None

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return facts

    with handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            tail.append(line)

            if not RECORD_TYPE_RE.search(line):
                if '"timestamp"' in line:
                    skipped_stamp_line = line
                # Still worth the session id from a bridge-session stub.
                if not facts["session_id"] and '"sessionId"' in line:
                    record = loads(line)
                    if record:
                        facts["session_id"] = record.get("sessionId") or ""
                continue

            record = loads(line)
            if not record:
                continue
            kind = record.get("type")

            if not facts["session_id"]:
                facts["session_id"] = record.get("sessionId") or ""

            if kind == "ai-title":
                facts["title"] = record.get("aiTitle") or facts["title"]
                continue
            if kind == "cost-state":
                # A running total: every later record supersedes the one
                # before, so the last one is the session's cost to date.
                facts["cost"] = record
                if skipped_stamp_line is not None:
                    skipped = loads(skipped_stamp_line)
                    stamp = _parse_ts(skipped.get("timestamp")) if skipped else None
                    last_seen = stamp or last_seen
                    skipped_stamp_line = None
                facts["cost_as_of"] = last_seen
                continue
            if kind == "last-prompt":
                facts["last_prompt"] = record.get("lastPrompt") or facts["last_prompt"]
                continue

            if record.get("cwd"):
                facts["cwd"] = record["cwd"]
            if record.get("gitBranch"):
                facts["branch"] = record["gitBranch"]
            stamp = _parse_ts(record.get("timestamp"))
            if stamp:
                last_seen = stamp
                skipped_stamp_line = None
            if stamp and (facts["last_ts"] is None or stamp > facts["last_ts"]):
                facts["last_ts"] = stamp

            if kind == "user":
                # Tool results and injected skill bodies are user records too.
                # Counting them turns 6 prompts into 93 turns, and reading them
                # makes a skill's documentation look like the session's goal.
                if record.get("isMeta"):
                    continue
                text = text_of(record.get("message"))
                if not is_real_prompt(text):
                    continue
                facts["turns"] += 1
                if not facts["first_prompt"]:
                    facts["first_prompt"] = text.strip()
            elif kind == "assistant":
                model = message_of(record).get("model")
                if isinstance(model, str) and model and model not in facts["models"]:
                    facts["models"].append(model)
                turn_usage.add(record)

    facts["usage"], facts["thinking"] = turn_usage.totals()
    facts["stopped_at"] = _last_assistant_text(tail)
    if not facts["last_prompt"]:
        facts["last_prompt"] = _last_user_text(tail) or facts["first_prompt"]
    return facts


def _last_assistant_text(tail: collections.deque[str]) -> str:
    """Recover the closing assistant message from the tail of the file."""
    for line in reversed(tail):
        if not is_type(line, "assistant"):
            continue
        record = loads(line)
        if not record:
            continue
        text = text_of(record.get("message")).strip()
        if text:
            return text
    return ""


def _last_user_text(tail: collections.deque[str]) -> str:
    for line in reversed(tail):
        if not is_type(line, "user"):
            continue
        record = loads(line)
        if not record:
            continue
        text = text_of(record.get("message")).strip()
        if is_real_prompt(text):
            return text
    return ""


def read_goal_bounded(
    path: pathlib.Path,
    goal_mode: str = DEFAULT_GOAL_MODE,
    head_bytes: int = 65536,
    tail_bytes: int = 262144,
) -> str:
    """Derive only the goal line, in time that does not depend on file size.

    A status line renders on every frame, and a full scan costs 0.681s on the
    largest transcript here, so it cannot use the same path the other views do.
    This reads a bounded head and a bounded tail instead: the first prompt is
    near the start, and ai-title is rewritten throughout so the last copy is in
    the tail. Nothing else a card carries is derived, because the status line
    payload already supplies cost, context, model, and directory directly.

    Returns an empty string rather than raising, so a status line never fails.
    """
    if goal_mode not in GOAL_MODES:
        goal_mode = DEFAULT_GOAL_MODE
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(min(head_bytes, size))
            tail = b""
            if size > head_bytes:
                handle.seek(max(size - tail_bytes, head_bytes))
                tail = handle.read()
    except OSError:
        return ""

    def lines_of(chunk: bytes, drop_first: bool) -> list[str]:
        text = chunk.decode("utf-8", errors="replace")
        rows = text.split("\n")
        # A seek lands mid-line, so the first row of the tail is a fragment.
        if drop_first and rows:
            rows = rows[1:]
        return [row for row in rows if row]

    title = ""
    for line in lines_of(tail, True) + lines_of(head, False):
        if not is_type(line, "ai-title"):
            continue
        record = loads(line)
        if record and record.get("aiTitle"):
            title = str(record["aiTitle"])
    if goal_mode == "summary" and title:
        return truncate(redact(title), GOAL_CAP)

    first_prompt = ""
    for line in lines_of(head, False):
        if not is_type(line, "user"):
            continue
        record = loads(line)
        if not record or record.get("isMeta"):
            continue
        text = text_of(record.get("message"))
        if is_real_prompt(text):
            first_prompt = text.strip()
            break

    if goal_mode == "original":
        goal = first_prompt
    elif goal_mode == "clean":
        goal = clean_prompt(first_prompt)
    else:
        goal = title or clean_prompt(first_prompt)
    return truncate(redact(goal), GOAL_CAP)


def project_name(cwd: str, transcript: pathlib.Path) -> str:
    """Name the project a session belongs to.

    Prefers the working directory. Falls back to the encoded directory name
    the runtime uses under ~/.claude/projects when a transcript carries no
    cwd, which is the case for a stub that never ran a turn.
    """
    if cwd:
        name = pathlib.PurePath(cwd).name
        return name or cwd
    encoded = transcript.parent.name
    trimmed = encoded.replace("-Users-" + os.environ.get("USER", ""), "").strip("-")
    if not trimmed:
        return "home"
    return trimmed.split("-")[-1] or trimmed


def is_subagent_transcript(path: pathlib.Path) -> bool:
    """True when a transcript belongs to a dispatched agent, not a session.

    A dispatched agent writes to
    <projects>/<encoded-cwd>/<parent-session-id>/subagents/agent-<id>.jsonl
    so the shape of the path is the test.
    """
    return path.parent.name == "subagents" and path.name.startswith("agent-")


def subagent_meta(path: pathlib.Path) -> dict[str, Any]:
    """Read the dispatch metadata that sits beside a subagent transcript.

    The meta file carries toolUseId, which is the id of the Agent tool call
    in the parent transcript. That is the join that turns a pile of separate
    transcripts into one traversable tree.
    """
    meta_path = path.with_suffix(".meta.json")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def short_model(name: str) -> str:
    """Reduce a model id to the word a person recognises.

    Placeholder ids such as <synthetic> mark records the runtime generated
    rather than a model, so they return empty and callers drop them.
    """
    name = name or ""
    if name.startswith("<"):
        return ""
    for token in ("opus", "sonnet", "haiku", "fable"):
        if token in name.lower():
            return token
    return name.split("-")[0] or "?"


def build_card(
    path: pathlib.Path, goal_mode: str = DEFAULT_GOAL_MODE, limit: int = 0
) -> Card:
    """Build one card from one transcript."""
    if goal_mode not in GOAL_MODES:
        goal_mode = DEFAULT_GOAL_MODE
    facts = read_transcript(path)

    first_prompt = redact(facts["first_prompt"])
    last_prompt = redact(facts["last_prompt"])
    stopped_at = redact(facts.get("stopped_at") or "")
    title = redact(facts["title"])

    if goal_mode == "original":
        goal = first_prompt
    elif goal_mode == "clean":
        goal = clean_prompt(first_prompt)
    else:
        goal = title or clean_prompt(first_prompt)
    if limit:
        goal = truncate(goal, limit)

    meta = subagent_meta(path) if is_subagent_transcript(path) else {}
    kind = "agent" if meta or is_subagent_transcript(path) else "session"

    cost = facts["cost"] if isinstance(facts["cost"], dict) else {}
    model_usage = cost.get("modelUsage")
    model_usage = model_usage if isinstance(model_usage, dict) else {}
    # One token figure per session in every view (B-06): the transcript's own
    # turns plus each dispatched agent's transcript, the same count
    # scripts/budget-report.py (measure_session) makes. cost-state's
    # modelUsage is never the token source: it is written periodically, so it
    # lags, and it includes background calls the transcript does not record,
    # so the card and the reports disagreed. cost-state stays the source of
    # dollars, duration, lines changed and the model list.
    usage = dict(facts["usage"])
    thinking_tokens = int(facts["thinking"])
    if kind == "session":
        for agent in subagent_transcripts(path):
            agent_usage, agent_thinking = usage_by_turn(agent)
            for key, value in agent_usage.items():
                usage[key] = usage.get(key, 0) + value
            thinking_tokens += agent_thinking
    tokens = sum(usage.values())
    # Thinking tokens come from the turns too; only when no turn reports any
    # does the count fall back to cost-state's per-model thinkingTokens.
    if not thinking_tokens:
        for entry in model_usage.values():
            if isinstance(entry, dict):
                thinking_tokens += int(number(entry.get("thinkingTokens")))

    names = list(model_usage.keys()) or facts["models"]
    models: list[str] = []
    for name in names:
        short = short_model(name)
        if short and short not in models:
            models.append(short)

    last_active = facts["last_ts"]
    if last_active is None:
        try:
            last_active = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        except OSError:
            last_active = None

    if kind == "agent":
        # A dispatched agent's own id is the file stem, not the sessionId in
        # its records, which is the parent's. Reporting the parent id here is
        # what makes two different agents look like the same session.
        session_id = path.stem
        parent_session_id = path.parent.parent.name
    else:
        session_id = facts["session_id"] or path.stem
        parent_session_id = ""

    return Card(
        session_id=session_id,
        transcript=str(path),
        cwd=facts["cwd"],
        project=project_name(facts["cwd"], path),
        branch=facts["branch"],
        title=title,
        goal=truncate(goal, GOAL_CAP),
        first_prompt=truncate(first_prompt, TEXT_CAP),
        last_prompt=truncate(last_prompt, TEXT_CAP),
        stopped_at=truncate(stopped_at, TEXT_CAP),
        last_active=last_active,
        # cost-state is absent from dispatched-agent transcripts and from some
        # older sessions. Reporting 0.0 there reads as "this was free", so the
        # card carries whether the number is real and the view says "n/a".
        cost_usd=number(cost.get("totalCostUSD")),
        cost_known=bool(cost),
        cost_as_of=facts["cost_as_of"] if cost else None,
        tokens=tokens,
        thinking_tokens=thinking_tokens,
        models=models,
        duration_ms=int(number(cost.get("totalDuration"))),
        lines_added=int(number(cost.get("totalLinesAdded"))),
        lines_removed=int(number(cost.get("totalLinesRemoved"))),
        turns=facts["turns"],
        status=freshness(last_active),
        kind=kind,
        parent_session_id=parent_session_id,
        tool_use_id=str(meta.get("toolUseId") or ""),
        agent_type=str(meta.get("agentType") or ""),
        requested_model=str(meta.get("model") or ""),
        spawn_depth=int(number(meta.get("spawnDepth"))),
    )


def default_root() -> pathlib.Path:
    override = os.environ.get("SESSION_CARD_ROOT")
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".claude" / "projects"


def find_transcripts(root: pathlib.Path, since_hours: float = 0.0) -> Iterator[pathlib.Path]:
    """Yield transcripts under root, newest first, optionally time-bounded."""
    if not root.is_dir():
        return
    cutoff = 0.0
    if since_hours:
        cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - since_hours * 3600
    found: list[tuple[float, pathlib.Path]] = []
    for path in root.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if cutoff and mtime < cutoff:
            continue
        found.append((mtime, path))
    for _, path in sorted(found, key=lambda pair: pair[0], reverse=True):
        yield path


def scan(
    root: pathlib.Path | None = None,
    since_hours: float = 0.0,
    project: str = "",
    goal_mode: str = DEFAULT_GOAL_MODE,
    limit: int = 0,
    skip_empty: bool = True,
    include_agents: bool = False,
) -> list[Card]:
    """Build cards for every transcript under root, newest first.

    Dispatched agents are left out by default. They are real transcripts, but
    they are steps inside a session rather than sessions a person resumes, so
    listing them beside their own parent misrepresents what is running.
    """
    root = root or default_root()
    cards: list[Card] = []
    for path in find_transcripts(root, since_hours):
        if not include_agents and is_subagent_transcript(path):
            continue
        card = build_card(path, goal_mode=goal_mode)
        # A session opened by a slash command has no prompt the card counts,
        # but real spend; only a session with neither is empty.
        if skip_empty and not card.turns and not card.goal and not card.tokens:
            continue
        if project and project.lower() not in card.project.lower():
            continue
        cards.append(card)
        if limit and len(cards) >= limit:
            break
    return cards


def _cmd_card(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.transcript).expanduser()
    if not path.is_file():
        print(f"session-card: no transcript at {path}", file=sys.stderr)
        return 1
    card = build_card(path, goal_mode=args.goal_mode)
    if args.json:
        print(json.dumps(card.to_dict(), indent=2))
    else:
        print(f"{card.project} | {card.goal} | {card.age} | ${card.cost_usd:.2f}")
    return 0


def _cmd_goal(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.transcript).expanduser()
    if not path.is_file():
        return 0
    print(read_goal_bounded(path, goal_mode=args.goal_mode))
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).expanduser() if args.root else None
    cards = scan(
        root=root,
        since_hours=args.since,
        project=args.project,
        goal_mode=args.goal_mode,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([card.to_dict() for card in cards], indent=2))
        return 0
    for card in cards:
        print(f"{card.short_id} | {card.project} | {card.age} | {card.goal}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session-card", description="derive resume cards from transcripts"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    card = sub.add_parser("card", help="build one card from one transcript")
    card.add_argument("transcript")
    card.add_argument("--goal-mode", choices=GOAL_MODES, default=DEFAULT_GOAL_MODE)
    card.add_argument("--json", action="store_true")
    card.set_defaults(func=_cmd_card)

    goal = sub.add_parser("goal", help="the goal line only, in bounded time")
    goal.add_argument("transcript")
    goal.add_argument("--goal-mode", choices=GOAL_MODES, default=DEFAULT_GOAL_MODE)
    goal.set_defaults(func=_cmd_goal)

    scan_cmd = sub.add_parser("scan", help="build cards for every transcript")
    scan_cmd.add_argument("--root", default="")
    scan_cmd.add_argument("--since", type=float, default=0.0, help="hours")
    scan_cmd.add_argument("--project", default="")
    scan_cmd.add_argument("--goal-mode", choices=GOAL_MODES, default=DEFAULT_GOAL_MODE)
    scan_cmd.add_argument("--limit", type=int, default=0)
    scan_cmd.add_argument("--json", action="store_true")
    scan_cmd.set_defaults(func=_cmd_scan)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
