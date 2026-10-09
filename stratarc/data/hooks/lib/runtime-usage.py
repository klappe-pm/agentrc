#!/usr/bin/env python3
"""Tokens by kind for one session of a runtime other than Claude Code.

the design record (g-04, B-04)
is the design. hooks/lib/session-card.py reads Claude Code transcripts; this is
the derivation layer for the other runtimes, returning tokens in the keys of
scripts/budgets.py (token_measures). A kind a runtime does not report is
absent from the result, never zero, so a report prints it as n/a.

A reader lands only after a live file on the operator's machine confirmed
three facts for its runtime: where the session file lives, which record
carries the token counts, and that the session_id its PreToolUse payload
delivers names that file. NOT_CONFIRMED names, for every other runtime, which
of the three failed; those runtimes stay tokens n/a.

Usage:
    runtime-usage.py measure <runtime> <session-id> [--home DIR] [--json]
    runtime-usage.py capture <runtime> <session-id> <out-dir> [--home DIR]

capture writes a trimmed copy of the session's files under out-dir, keeping
their paths relative to the runtime home: only the records the reader reads,
with every free text field dropped, and every remaining string run through
the repository's token-shaped-values detector (hooks/lib/guard-utils.sh) and
replaced with [REDACTED:<kind>] on a match, per captured-content-redaction.

Env:
    CODEX_HOME   default ~/.codex (Codex's own variable); rollouts under sessions/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional

LIB = pathlib.Path(__file__).resolve().parent
DETECTOR = LIB / "guard-utils.sh"

# The kinds scripts/budgets.py measures, in session-card's usage shape.
KINDS = ("in", "out", "cache_read", "cache_write")
BUDGET_KEY = {
    "in": "tokens.input",
    "out": "tokens.output",
    "cache_read": "tokens.cache_read",
    "cache_write": "tokens.cache_write",
}

# Why a runtime has no reader: the fact of the three that a live file on the
# operator's machine did not confirm (checked 2026-09-23, WI-37).
NOT_CONFIRMED: Dict[str, str] = {
    "gemini": (
        "no reader: a current session file with a token record and a PreToolUse payload naming it "
        "were not confirmed (Gemini CLI 0.56.0 reaches no model on this machine, WI-32 note 15)"
    ),
    "opencode": (
        "no reader: no PreToolUse payload has named an OpenCode session (the deployed config is "
        "refused, WI-32 note 7)"
    ),
    "cursor": "no reader: the Cursor transcript carries no token field (WI-32 live check)",
}

# A session id is used in a glob, so it may carry no path or glob character.
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def _iso(stamp: Optional[dt.datetime]) -> Optional[str]:
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z") if stamp else None


def _loads(line: str) -> Optional[Dict[str, Any]]:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


# ---------------------------------------------------------------------------
# Codex: rollout JSONL under $CODEX_HOME/sessions/YYYY/MM/DD/.
# ---------------------------------------------------------------------------


def codex_home(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    if home is not None:
        return pathlib.Path(home).expanduser()
    override = os.environ.get("CODEX_HOME")
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".codex"


def _session_meta(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    """A rollout's session_meta record, parsed from the whole first line.

    The line is read whole and parsed, never cut to a prefix: session_meta
    carries the full base instructions, and a session_id placed after them
    would otherwise be missed (Codex review of PR 81).
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return None
    record = _loads(first)
    if record is None or record.get("type") != "session_meta":
        return None
    return record


def _first_session_id(path: pathlib.Path) -> Optional[str]:
    """The session_id on a rollout's session_meta record."""
    record = _session_meta(path)
    payload = record.get("payload") if record and isinstance(record.get("payload"), dict) else {}
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


def codex_rollout_by_cwd(
    cwd: str,
    since: dt.datetime,
    home: Optional[pathlib.Path] = None,
    exclude: Iterable[str] = (),
    until: Optional[dt.datetime] = None,
) -> Optional[str]:
    """The session id when exactly one own rollout started in cwd during the call.

    For a codex call whose output named no session id (WI-93): its rollout is
    the one whose session_meta names the same working directory and a start
    no earlier than the call's. A dispatched agent's rollout carries its
    parent's session_id, not its own id, so it is never the answer; ids in
    exclude (already claimed by earlier calls) are skipped. Date directories
    are local dates, so the day before since's and every day through until's
    are read.
    """
    target = os.path.realpath(cwd)
    sessions = codex_home(home) / "sessions"
    start = since.astimezone(dt.timezone.utc)
    end = (until or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    claimed = set(exclude)
    day = start.astimezone().date() - dt.timedelta(days=1)
    last = end.astimezone().date() + dt.timedelta(days=1)
    found: List[Any] = []
    while day <= last:
        directory = sessions / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        day += dt.timedelta(days=1)
        if not directory.is_dir():
            continue
        for path in directory.glob("rollout-*.jsonl"):
            record = _session_meta(path)
            payload = record.get("payload") if record and isinstance(record.get("payload"), dict) else {}
            own = str(payload.get("id") or "")
            parent = str(payload.get("session_id") or own)
            if not own or parent != own or own in claimed or not SAFE_ID.match(own):
                continue
            stamp = _parse_ts(payload.get("timestamp")) or _parse_ts(record.get("timestamp") if record else None)
            if stamp is None or stamp < start or stamp > end:
                continue
            where = payload.get("cwd")
            if not isinstance(where, str) or not where or os.path.realpath(where) != target:
                continue
            found.append(own)
    return found[0] if len(found) == 1 else None


def codex_rollouts(session_id: str, home: Optional[pathlib.Path] = None) -> List[pathlib.Path]:
    """The session's own rollout first, then each dispatched agent's rollout.

    The PreToolUse payload's session_id names the session's own rollout file.
    A dispatched agent (codex exec review writes one) has a rollout named by
    its own thread id whose session_meta carries the parent's session_id, so
    it is found by that field in the date directories the session spans.
    """
    if not SAFE_ID.match(session_id or ""):
        return []
    sessions = codex_home(home) / "sessions"
    own = sorted(sessions.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
    if not own:
        return []
    root = own[0]
    first_day = root.parent
    try:
        last_seen = dt.datetime.fromtimestamp(root.stat().st_mtime).date()
    except OSError:
        last_seen = None
    days = [first_day]
    try:
        start = dt.date(int(first_day.parent.parent.name), int(first_day.parent.name), int(first_day.name))
    except ValueError:
        start = None
    if start and last_seen and last_seen > start:
        span = (last_seen - start).days
        for offset in range(1, span + 2):
            day = start + dt.timedelta(days=offset)
            days.append(sessions / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}")
    elif start:
        day = start + dt.timedelta(days=1)
        days.append(sessions / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}")
    agents: List[pathlib.Path] = []
    for directory in days:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("rollout-*.jsonl")):
            if path == root:
                continue
            if _first_session_id(path) == session_id:
                agents.append(path)
    return [root] + agents


def read_codex_rollout(path: pathlib.Path) -> Dict[str, Any]:
    """Facts from one rollout: its ids, times, turns, models and last token total."""
    facts: Dict[str, Any] = {
        "path": str(path),
        "session_id": "",
        "thread_id": "",
        "parent_thread_id": "",
        "cwd": "",
        "branch": "",
        "first": None,
        "last": None,
        "turns": 0,
        "models": [],
        "usage": None,
    }
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return facts
    with handle:
        for line in handle:
            record = _loads(line)
            if record is None:
                continue
            stamp = _parse_ts(record.get("timestamp"))
            if stamp:
                facts["first"] = facts["first"] or stamp
                facts["last"] = stamp
            kind = record.get("type")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            if kind == "session_meta":
                facts["session_id"] = str(payload.get("session_id") or payload.get("id") or "")
                facts["thread_id"] = str(payload.get("id") or "")
                facts["parent_thread_id"] = str(payload.get("parent_thread_id") or "")
                facts["cwd"] = str(payload.get("cwd") or "")
                git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                facts["branch"] = str(git.get("branch") or "")
            elif kind == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model and model not in facts["models"]:
                    facts["models"].append(model)
            elif kind == "event_msg" and payload.get("type") == "task_started":
                facts["turns"] += 1
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                total = info.get("total_token_usage") if isinstance(info, dict) else None
                if isinstance(total, dict):
                    # Cumulative for the rollout: the last one is its total.
                    facts["usage"] = total
    return facts


def codex_usage(total: Dict[str, Any]) -> Dict[str, int]:
    """One rollout's total_token_usage in session-card's kinds, reported kinds only.

    input_tokens includes the cached input (total_tokens is input_tokens plus
    output_tokens on every rollout checked), and reasoning_output_tokens is
    inside output_tokens. Cache writes are taken to be inside input_tokens as
    well, so the kinds always sum to Codex's own total_tokens; every rollout on
    the operator's machine reported 0 cache writes on 2026-09-23, so that part
    is unverified.
    """
    usage: Dict[str, int] = {}
    inputs = _count(total.get("input_tokens"))
    cached = _count(total.get("cached_input_tokens"))
    written = _count(total.get("cache_write_input_tokens"))
    output = _count(total.get("output_tokens"))
    if inputs is not None:
        usage["in"] = max(inputs - (cached or 0) - (written or 0), 0)
    if output is not None:
        usage["out"] = output
    if cached is not None:
        usage["cache_read"] = cached
    if written is not None:
        usage["cache_write"] = written
    return usage


def measure_codex(session_id: str, home: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Tokens by kind, turns and wall time for one Codex session and its agents.

    Confirmed on the operator's Mac on 2026-09-23 (codex-cli 0.156.1): the
    PreToolUse payload's session_id names the tool-budget state file and the
    rollout rollout-<timestamp>-<session_id>.jsonl; token counts are the
    event_msg token_count record's info.total_token_usage, cumulative per
    rollout. A session's tokens are the sum of its own rollout's last total and
    each agent rollout's, as budget-report.py adds a Claude Code session's
    subagent transcripts.
    """
    paths = codex_rollouts(session_id, home)
    if not paths:
        return _not_measured("codex", session_id, f"no rollout for this session under {codex_home(home) / 'sessions'}")
    files = [read_codex_rollout(path) for path in paths]
    parent = files[0]["session_id"]
    if parent and parent != session_id:
        # An agent's own thread id: its tokens are counted under the session that dispatched it.
        return _not_measured("codex", session_id, f"no rollout for this session: it is an agent thread of {parent}")
    totals: Dict[str, int] = {}
    reported = 0
    for facts in files:
        if facts["usage"] is None:
            continue
        reported += 1
        for kind, value in codex_usage(facts["usage"]).items():
            totals[kind] = totals.get(kind, 0) + value
    if not reported:
        return _not_measured("codex", session_id, "its rollouts carry no token_count record")
    measures: Dict[str, Any] = {BUDGET_KEY[kind]: totals[kind] for kind in KINDS if kind in totals}
    measures["tokens.total"] = sum(totals.values())
    own = files[0]
    measures["turns"] = int(own["turns"])
    firsts = [facts["first"] for facts in files if facts["first"]]
    lasts = [facts["last"] for facts in files if facts["last"]]
    first, last = (min(firsts), max(lasts)) if firsts and lasts else (None, None)
    if first and last:
        measures["wall_minutes"] = round(max((last - first).total_seconds(), 0.0) / 60.0, 1)
    models: List[str] = []
    for facts in files:
        models.extend(model for model in facts["models"] if model not in models)
    return {
        "runtime": "codex",
        "session_id": session_id,
        "measured": True,
        "reason": "",
        "measures": measures,
        "started": _iso(first),
        "last_active": _iso(last),
        "cwd": own["cwd"],
        "branch": own["branch"],
        "models": models,
        "agents": len(files) - 1,
        "files": [facts["path"] for facts in files],
    }


# Runtimes whose reader landed, each confirmed by a live file (see the module docstring).
READERS: Dict[str, Any] = {"codex": measure_codex}


def _not_measured(runtime: str, session_id: str, reason: str) -> Dict[str, Any]:
    return {"runtime": runtime, "session_id": session_id, "measured": False, "reason": reason, "measures": {}}


def measure(runtime: str, session_id: str, home: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Tokens by kind for one session, or measured False with the reason."""
    reader = READERS.get(runtime)
    if reader is None:
        reason = NOT_CONFIRMED.get(runtime, f"no reader for runtime {runtime or 'unknown'}")
        return _not_measured(runtime, session_id, reason)
    return reader(session_id, home)


# ---------------------------------------------------------------------------
# capture: a redacted fixture from a real session file.
# ---------------------------------------------------------------------------

# Per record, the fields a Codex fixture keeps. Everything else, every prompt,
# message, instruction and tool output among it, is dropped.
CODEX_KEEP_META = (
    "session_id",
    "id",
    "parent_thread_id",
    "timestamp",
    "cwd",
    "originator",
    "cli_version",
    "source",
    "thread_source",
    "model_provider",
)


def _trim_codex(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    kept: Dict[str, Any] = {"timestamp": record.get("timestamp"), "type": kind}
    if kind == "session_meta":
        kept["payload"] = {key: payload[key] for key in CODEX_KEEP_META if key in payload}
        git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
        if git.get("branch"):
            # The branch only: spend suggests a work item from it (B-02).
            kept["payload"]["git"] = {"branch": git["branch"]}
    elif kind == "turn_context":
        kept["payload"] = {key: payload[key] for key in ("turn_id", "model") if key in payload}
    elif kind == "event_msg" and payload.get("type") in ("task_started", "task_complete"):
        kept["payload"] = {key: payload[key] for key in ("type", "turn_id") if key in payload}
    elif kind == "event_msg" and payload.get("type") == "token_count":
        # rate_limits carries the account's plan and limits, not session spend.
        kept["payload"] = {"type": "token_count", "info": payload.get("info"), "rate_limits": None}
    else:
        return None
    return kept


def detector_label(text: str) -> Optional[str]:
    """The detector's label for the first token-shaped value in text, or None.

    Exit 0 is a match and exit 1 a clean scan. Any other outcome (a missing or
    broken detector) stops the caller, since unscanned content must never be
    persisted as if it were clean (Codex review of PR 81).
    """
    if not DETECTOR.is_file():
        raise SystemExit(f"runtime-usage: the detector {DETECTOR} is missing; nothing was captured")
    try:
        result = subprocess.run(
            ["bash", str(DETECTOR), "label-stdin"],
            input=text,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"runtime-usage: the detector could not run: {error}")
    if result.returncode == 0:
        return result.stdout.strip() or "secret"
    if result.returncode == 1:
        return None
    raise SystemExit(
        f"runtime-usage: the detector exited {result.returncode}; nothing was captured"
    )


def redact(node: Any, seen: Optional[Dict[str, Optional[str]]] = None) -> Any:
    """Every string whose decoded value the detector matches, replaced with [REDACTED:<kind>].

    Each string is checked as itself, not inside serialized JSON, whose
    escaping (a tab written as \\t) can hide a value from the detector (Codex
    review of PR 81). seen caches the answer for a repeated string.
    """
    seen = {} if seen is None else seen
    if isinstance(node, dict):
        return {key: redact(value, seen) for key, value in node.items()}
    if isinstance(node, list):
        return [redact(value, seen) for value in node]
    if isinstance(node, str) and node:
        if node not in seen:
            seen[node] = detector_label(node)
        if seen[node]:
            return f"[REDACTED:{seen[node]}]"
    return node


def capture_codex(session_id: str, out_dir: pathlib.Path, home: Optional[pathlib.Path] = None) -> List[pathlib.Path]:
    sessions = codex_home(home) / "sessions"
    written: List[pathlib.Path] = []
    seen: Dict[str, Optional[str]] = {}
    for path in codex_rollouts(session_id, home):
        records: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                record = _loads(line)
                trimmed = _trim_codex(record) if record else None
                if trimmed is not None:
                    # Every string, unconditionally, before it is serialized.
                    records.append(redact(trimmed, seen))
        text = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        target = out_dir / "sessions" / path.relative_to(sessions)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


CAPTURERS = {"codex": capture_codex}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render(result: Dict[str, Any]) -> List[str]:
    head = f"{result['runtime']} {result['session_id']}"
    if not result["measured"]:
        return [f"{head}: tokens n/a ({result['reason']})"]
    measures = result["measures"]
    kinds = ", ".join(
        f"{key.split('.', 1)[1]} {measures[key]}" if key in measures else f"{key.split('.', 1)[1]} n/a"
        for key in BUDGET_KEY.values()
    )
    return [
        f"{head}: tokens {measures.get('tokens.total', 0)} ({kinds})",
        f"  turns {measures.get('turns', 'n/a')} · wall {measures.get('wall_minutes', 'n/a')}m · "
        f"agents {result.get('agents', 0)} · files {len(result.get('files', []))}",
    ]


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("measure", help="tokens by kind for one session")
    one.add_argument("runtime")
    one.add_argument("session_id")
    one.add_argument("--home", type=pathlib.Path)
    one.add_argument("--json", action="store_true")
    cap = sub.add_parser("capture", help="write a redacted fixture of one session's files")
    cap.add_argument("runtime")
    cap.add_argument("session_id")
    cap.add_argument("out_dir", type=pathlib.Path)
    cap.add_argument("--home", type=pathlib.Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "measure":
        result = measure(args.runtime, args.session_id, args.home)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print("\n".join(_render(result)))
        return 0 if result["measured"] else 1
    capturer = CAPTURERS.get(args.runtime)
    if capturer is None:
        print(f"runtime-usage: no capture for runtime {args.runtime}", file=sys.stderr)
        return 2
    written = capturer(args.session_id, args.out_dir, args.home)
    if not written:
        print(f"runtime-usage: no {args.runtime} session file for {args.session_id}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
