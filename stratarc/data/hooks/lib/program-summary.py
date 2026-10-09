#!/usr/bin/env python3
"""Tell the operator what the program is doing, without interrupting them (O-06).

the design record, O-06:
an interactive session is told, in one line, how many runs are in flight, how
many pull requests wait for the operator's review and how many items wait on
a decision, and the status line carries the same counts.

The counts come from ``scripts/program-status.py snapshot --json`` and nothing
else: this file never reads the event log or PLAN.md itself. It classifies the
snapshot's ``events.items`` by each work item's latest state event:

  in flight          dispatched, item_started, test_failing, gates_passed,
                     pr_opened, codex_review_posted, fix_pushed, resumed;
                     only when that event is younger than IN_FLIGHT_HOURS,
                     so a run that died without a last word stops counting
  awaiting review    handoff_posted: the run stopped for the operator
  waiting on you     needs_decision
  not counted        merged, item_done, run_failed, blocked, escalated,
                     paused, or anything else

``finding``, ``review_completed`` and ``deployed`` are reports about an item,
not steps of its run, so they never replace its state: a background code
review completing says nothing about whether the
item's own run is still in flight, awaiting the operator, or done. item
``dispatcher`` (the dispatcher's own pause and resume) is not a work item.
The event names are compared as plain strings, so ``dispatched`` counts from
the moment the event schema admits it.

Commands:
  line     run the snapshot within PROGRAM_SUMMARY_TIMEOUT seconds (default
           3), write the counts to the cache, print one sentence, or nothing
           when every count is zero. hooks/session-start.sh runs this.
  segment  print the status line segment from the cache alone, never running
           the snapshot on a frame; a missing cache, or one older than
           REFRESH_AFTER, starts one detached ``refresh`` under a lock, and
           one older than SHOW_FOR prints nothing. A test and debugging entry
           point: hooks/lib/session-statusline.py calls cached_segment().
  refresh  what line does, printing nothing. Internal: ``--lock-token``
           names the lock the detached refresh releases on success; a failed
           refresh keeps it for LOCK_HOLD, so a broken snapshot is retried
           once per LOCK_HOLD rather than on every frame.

Limits of reading the event stream alone: an event names its item but not
its project, so two projects that both number an item WI-n share one state
here; and a decision or a review never ages out, so it counts until a later
event replaces it or it falls out of the last EVENTS_TAIL lines, and a pull
request merged without a ``merged`` event keeps counting until then.

The snapshot runs with --no-remote: the counts come from the event stream,
which covers every project's runs, and a gh call would cost a second of every
session start. Every failure (no checkout, a timeout, a non-zero exit, output
that is not a snapshot) prints nothing and exits 0, since a session start or a
status line frame must never break on this.

scripts/ is not deployed beside hooks/lib, so the snapshot is run from the
source checkout, the pattern hooks/session-start.sh uses for
session-resume.py: $STRATARC_SOURCE when set, else $LLM_ROOT. With neither
there is no snapshot script and nothing is printed.

Env:
  STRATARC_SOURCE, LLM_ROOT  the source checkout holding scripts/program-status.py (first set wins)
  PROGRAM_SUMMARY_CACHE     default ~/.agent-hooks/state/program-summary.json
  PROGRAM_SUMMARY_TIMEOUT   seconds the snapshot may take (default 3)
  RUNTIME_HOOKS_DISABLE     set to 1 to print nothing and write nothing
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

IN_FLIGHT = frozenset({"dispatched", "item_started", "test_failing", "gates_passed", "pr_opened", "codex_review_posted", "fix_pushed", "resumed"})
AWAITING_REVIEW = frozenset({"handoff_posted"})
NEEDS_DECISION = frozenset({"needs_decision"})
NOT_A_STEP = frozenset({"finding", "deployed", "review_completed"})
NOT_AN_ITEM = frozenset({"dispatcher"})

IN_FLIGHT_HOURS = 24  # the longest a run is expected to go without an event (WI-60's --max-run-hours default)
EVENTS_TAIL = 2000  # event log lines the snapshot reads; a busy day writes several hundred
DEFAULT_TIMEOUT = 3.0
REFRESH_AFTER = 300  # seconds before the status line asks for a fresh count
SHOW_FOR = 3600  # seconds a cached count is still worth showing
LOCK_HOLD = REFRESH_AFTER  # seconds a refresh lock is held; a failed refresh keeps it that long

ZERO = {"runs": 0, "reviews": 0, "decisions": 0}


def _parse_ts(value: Any) -> dt.datetime | None:
    """A contract timestamp (UTC, Z suffix, optional fraction) as an aware datetime.

    strptime on the whole seconds rather than fromisoformat, which on Python
    3.9 (/usr/bin/python3) refuses a fraction of other than 3 or 6 digits.
    """
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return dt.datetime.strptime(value[:-1].split(".", 1)[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def counts(snapshot: Any) -> dict[str, int]:
    """The three O-06 counts from a program-status snapshot document."""
    result = dict(ZERO)
    events = snapshot.get("events") if isinstance(snapshot, dict) else None
    items = events.get("items") if isinstance(events, dict) else None
    if not isinstance(items, list):
        return result
    now = _parse_ts(snapshot.get("generated_at")) or dt.datetime.now(dt.timezone.utc)
    latest: dict[str, dict] = {}
    for record in items:
        if not isinstance(record, dict):
            continue
        item, name = record.get("item"), record.get("event")
        if not isinstance(item, str) or not isinstance(name, str) or item in NOT_AN_ITEM or name in NOT_A_STEP:
            continue
        latest[item] = record
    horizon = now - dt.timedelta(hours=IN_FLIGHT_HOURS)
    for record in latest.values():
        name = record["event"]
        if name in NEEDS_DECISION:
            result["decisions"] += 1
        elif name in AWAITING_REVIEW:
            result["reviews"] += 1
        elif name in IN_FLIGHT:
            when = _parse_ts(record.get("ts"))
            if when is not None and when >= horizon:
                result["runs"] += 1
    return result


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def sentence(values: dict[str, int]) -> str:
    """The SessionStart line, or "" when there is nothing to tell."""
    if not any(values.values()):
        return ""
    return (
        f"Program: {_plural(values['runs'], 'run', 'runs')} in flight, "
        f"{_plural(values['reviews'], 'pull request', 'pull requests')} awaiting your review, "
        f"{_plural(values['decisions'], 'item', 'items')} waiting on your decision."
    )


def segment(values: dict[str, int]) -> str:
    """The status line segment, or "" when there is nothing to tell."""
    if not any(values.values()):
        return ""
    return f"{values['runs']} running · {values['reviews']} to review · {values['decisions']} to decide"


def cache_path() -> Path:
    override = os.environ.get("PROGRAM_SUMMARY_CACHE")
    return Path(override).expanduser() if override else Path.home() / ".agent-hooks" / "state" / "program-summary.json"


def llm_root() -> Path | None:
    """The source root: STRATARC_SOURCE, else LLM_ROOT, else None (no file-location fallback)."""
    value = os.environ.get("STRATARC_SOURCE") or os.environ.get("LLM_ROOT")
    return Path(value).expanduser() if value else None


def _timeout() -> float:
    try:
        value = float(os.environ.get("PROGRAM_SUMMARY_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def snapshot() -> dict | None:
    """program-status.py snapshot --json, or None on any failure or past the timeout."""
    root = llm_root()
    if root is None:
        return None
    script = root / "scripts" / "program-status.py"
    if not script.is_file():
        return None
    import subprocess  # here, not at the top: a status line frame with a fresh cache never needs it

    command = [sys.executable, str(script), "snapshot", "--json", "--no-remote", "--events", str(EVENTS_TAIL)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_timeout(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        document = json.loads(result.stdout)
    except ValueError:
        return None
    return document if isinstance(document, dict) and isinstance(document.get("events"), dict) else None


def write_cache(values: dict[str, int]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scratch = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        scratch.write_text(json.dumps({"generated_at": stamp, "counts": values}) + "\n", encoding="utf-8")
        os.replace(scratch, path)
    finally:
        try:
            scratch.unlink()
        except OSError:
            pass


def refresh() -> dict[str, int] | None:
    """Run the snapshot and cache its counts; None when the snapshot failed."""
    document = snapshot()
    if document is None:
        return None
    values = counts(document)
    try:
        write_cache(values)
    except OSError:
        pass
    return values


def read_cache() -> tuple[dict[str, int], float] | None:
    """The cached counts and their age in seconds, or None."""
    try:
        document = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    when = _parse_ts(document.get("generated_at"))
    values = document.get("counts")
    if when is None or not isinstance(values, dict):
        return None
    clean = {key: values.get(key) for key in ZERO}
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in clean.values()):
        return None
    return clean, (dt.datetime.now(dt.timezone.utc) - when).total_seconds()


def _lock_path() -> Path:
    path = cache_path()
    return path.with_name(path.name + ".lock")


def _take_lock() -> str | None:
    """Create the refresh lock and return its token, or None when another holds it.

    A lock younger than LOCK_HOLD is held. An older one is renamed aside
    before it is removed, so of two frames that both find it old only one
    removes it, and O_EXCL lets only one create the next.
    """
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - lock.stat().st_mtime <= LOCK_HOLD:
            return None
        aside = lock.with_name(f"{lock.name}.{os.getpid()}.{time.monotonic_ns()}")
        os.rename(lock, aside)
        aside.unlink()
    except FileNotFoundError:
        pass
    token = f"{os.getpid()}.{time.time_ns()}"
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(handle, "w") as stream:
        stream.write(token + "\n")
    return token


def _release_lock(token: str) -> None:
    """Remove the lock only when it is still the one this token took."""
    lock = _lock_path()
    try:
        if lock.read_text().strip() == token:
            lock.unlink()
    except OSError:
        pass


def refresh_in_background() -> None:
    """Start one detached refresh unless one ran or failed within LOCK_HOLD."""
    import subprocess  # here, not at the top: see snapshot()

    try:
        token = _take_lock()
    except OSError:
        return
    if token is None:
        return
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "refresh", "--lock-token", token],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        _release_lock(token)


def cached_segment() -> str:
    """The status line segment from the cache, asking for a refresh when it is old; never raises."""
    try:
        cached = read_cache()
        if cached is None or cached[1] > REFRESH_AFTER:
            refresh_in_background()
        if cached is None or cached[1] > SHOW_FOR:
            return ""
        return segment(cached[0])
    except Exception:
        return ""


def main(argv: list[str]) -> int:
    if os.environ.get("RUNTIME_HOOKS_DISABLE") == "1" or not argv:
        return 0
    command = argv[0]
    try:
        if command == "line":
            values = refresh()
            text = sentence(values) if values else ""
            if text:
                print(text)
        elif command == "segment":
            text = cached_segment()
            if text:
                print(text)
        elif command == "refresh":
            # A refresh that fails keeps its lock, so the status line retries
            # once per LOCK_HOLD rather than on every frame; one that succeeds
            # releases it.
            token = argv[2] if len(argv) > 2 and argv[1] == "--lock-token" else None
            if refresh() is not None and token:
                _release_lock(token)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
