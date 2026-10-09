#!/usr/bin/env python3
"""Per-parent live-subagent cap: the reservation state behind subagent-cap-guard.sh.

hooks/subagent-cap-guard.sh is the only caller. It passes the hook payload on
stdin and answers the runtime; this file keeps the locks. Exit 0 allows, exit
3 denies with the reason on stdout, and any other status is an internal
error, which the caller treats as allow.

G-02 of the design record is the
design. One lock file per subagent call, in a directory keyed by the parent
session id:

    pending-<key>.lock  created at PreToolUse when the call is admitted; it
                        counts toward the cap at once, so calls issued
                        together cannot pass the cap before any of them
                        starts. <key> is the payload's tool_use_id, or a
                        random name when the payload carries none.
    child-<key>.lock    a pending lock the runtime's start signal confirmed;
                        <key> is the started subagent's agent_id.

Each lock holds key=value lines: since (epoch seconds, reset when
confirmed), tool_use_id, subagent_type and, once confirmed, agent_id.

A pending lock is released:
    - when a guard deny record (hooks/lib/guard-log.sh) names its
      tool_use_id, since a call another hook denied never runs;
    - on a runtime with a start signal (START_SIGNAL_RUNTIMES), when it is
      older than SUBAGENT_CAP_CONFIRM_SECS (60) and still unconfirmed;
    - at PostToolUse or PostToolUseFailure for its tool_use_id, since the
      call returned. A failed call already confirmed as a child leaves other
      pending locks held. On a runtime with a start signal a confirmed lock is not released
      there: a background subagent returns its tool call at once and keeps
      running, so only the finish signal ends it. On a
      runtime without one, PostToolUse releases the call's lock, confirmed
      or not, and failing a named lock the oldest one, which is the release
      path this guard had before G-02.
A confirmed lock is released at the finish signal (SubagentStop) for its
agent_id. SUBAGENT_CAP_TTL_SECS (3600) stays the last resort for any lock
whose release was never seen.

The start and finish signals per runtime (docs/runtimes/compatibility.md):
    claude    SubagentStart and SubagentStop hooks. Verified 2026-09-24 from
              the Claude Code 2.1.273 binary: SubagentStart carries the
              session's own fields plus agent_id and agent_type, and no
              tool_use_id of the Agent call; SubagentStop carries agent_id,
              agent_type and agent_transcript_path. Because SubagentStart
              names no tool_use_id, it confirms the oldest pending lock of
              that parent, one whose subagent_type matches agent_type first.
              Pending locks are interchangeable for counting.
    opencode  the bridge hooks/opencode-runtime-hooks.ts sends SubagentStart
              on session.created for a child session (one with a parentID)
              and SubagentStop when that child goes idle or is deleted.
    others    no start signal found; see compatibility.md.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

DENY = 3
PRE_EVENTS = {"PreToolUse", ""}
POST_EVENTS = {"PostToolUse", "PostToolUseFailure"}
START_EVENT = "SubagentStart"
STOP_EVENT = "SubagentStop"
START_SIGNAL_RUNTIMES = {"claude", "opencode"}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def number(name: str, default: int) -> int:
    value = env(name)
    if not value:
        return default
    if not value.isdigit():
        raise ValueError(f"{name} is not a whole number")
    return int(value)


def safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def read_lock(path: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.rstrip("\n").partition("=")
                if sep:
                    fields[key] = value
    except OSError:
        pass
    return fields


def write_lock(path: str, fields: Dict[str, str]) -> None:
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{key}={value}\n" for key, value in fields.items() if value))
    os.replace(temporary, path)


def locks(directory: str) -> List[Tuple[str, str, Dict[str, str]]]:
    """(kind, path, fields) for every lock, oldest first, kind pending or child."""
    found = []
    for name in os.listdir(directory):
        if not name.endswith(".lock"):
            continue
        kind = "pending" if name.startswith("pending-") else "child" if name.startswith("child-") else ""
        if kind:
            path = os.path.join(directory, name)
            found.append((kind, path, read_lock(path)))
    return sorted(found, key=lambda item: (since(item[2]), item[1]))


def since(fields: Dict[str, str]) -> int:
    value = fields.get("since", "")
    return int(value) if value.isdigit() else -1


def denied_ids(candidates: set) -> set:
    """The ids among candidates that a guard deny record names (today and yesterday, UTC)."""
    if not candidates:
        return set()
    directory = env("GUARD_LOG_DIR") or os.path.join(os.environ.get("HOME") or "/tmp", ".agent-hooks", "telemetry")
    today = datetime.datetime.now(datetime.timezone.utc).date()
    found: set = set()
    for day in (today, today - datetime.timedelta(days=1)):
        try:
            with open(os.path.join(directory, f"guard-events-{day.isoformat()}.jsonl"), encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            continue
        if not any(candidate in content for candidate in candidates):
            continue
        for line in content.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("decision") == "deny" and record.get("tool_use_id") in candidates:
                found.add(record["tool_use_id"])
    return found


def remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def prune(directory: str, moment: int, start_signal: bool) -> None:
    ttl = number("SUBAGENT_CAP_TTL_SECS", 3600)
    window = number("SUBAGENT_CAP_CONFIRM_SECS", 60)
    current = locks(directory)
    denied = denied_ids({f.get("tool_use_id", "") for kind, _, f in current if kind == "pending"} - {""})
    for kind, path, fields in current:
        age = moment - since(fields)
        if since(fields) < 0 or age > ttl:
            remove(path)
        elif kind == "pending" and fields.get("tool_use_id") in denied:
            remove(path)
        elif kind == "pending" and start_signal and age > window:
            remove(path)


def first(items: List[Tuple[str, str, Dict[str, str]]], kind: str, **match: str) -> Optional[Tuple[str, str, Dict[str, str]]]:
    return next(
        (item for item in items if item[0] == kind and all(item[2].get(k) == v for k, v in match.items())),
        None,
    )


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    event = payload.get("hook_event_name") or ""
    parent = payload.get("session_id") or env("SUBAGENT_CAP_PPID")
    if not isinstance(event, str) or not isinstance(parent, str) or not parent:
        raise ValueError("no event or parent key")
    use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    agent_id = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else ""
    cap = number("SUBAGENT_CAP_PER_PARENT", 16)
    if cap == 0:
        raise ValueError("invalid cap")
    runtime = env("SUBAGENT_CAP_RUNTIME") or "claude"
    start_signal = runtime in START_SIGNAL_RUNTIMES

    state_dir = env("RUNTIME_HOOK_STATE_DIR") or os.path.join(os.environ.get("HOME") or "/tmp", ".agent-hooks", "state", "subagent-caps")
    directory = os.path.join(state_dir, safe(parent))
    os.makedirs(directory, exist_ok=True)
    moment = int(time.time())

    with open(os.path.join(directory, ".mutex.lock"), "a", encoding="utf-8") as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        prune(directory, moment, start_signal)
        current = locks(directory)

        if event in POST_EVENTS:
            named = first(current, "pending", tool_use_id=use_id) if use_id else None
            if not start_signal:
                named = named or (first(current, "child", tool_use_id=use_id) if use_id else None)
                named = named or (current[0] if current else None)
            elif not named and not use_id:
                named = first(current, "pending")
            if named:
                remove(named[1])
            return 0

        if event == START_EVENT:
            agent_type = payload.get("agent_type") if isinstance(payload.get("agent_type"), str) else ""
            pending = first(current, "pending", subagent_type=agent_type) or first(current, "pending")
            if pending and agent_id:
                fields = dict(pending[2], since=str(moment), agent_id=agent_id)
                write_lock(os.path.join(directory, f"child-{safe(agent_id)}.lock"), fields)
                remove(pending[1])
            return 0

        if event == STOP_EVENT:
            confirmed = first(current, "child", agent_id=agent_id) if agent_id else None
            if confirmed:
                remove(confirmed[1])
            return 0

        if event not in PRE_EVENTS:
            return 0
        if len(current) >= cap:
            print(
                f"This parent session already has {len(current)} live subagent(s) in flight, at the cap of {cap} "
                "(SUBAGENT_CAP_PER_PARENT). Wait for an in-flight subagent to complete before spawning another, "
                "or raise the cap explicitly for this run (SUBAGENT_CAP_PER_PARENT=<n>) if the fan-out is "
                "intentional and each subagent is isolated in its own worktree."
            )
            return DENY
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        subagent_type = tool_input.get("subagent_type") if isinstance(tool_input.get("subagent_type"), str) else ""
        key = safe(use_id) if use_id else uuid.uuid4().hex
        write_lock(
            os.path.join(directory, f"pending-{key}.lock"),
            {"since": str(moment), "tool_use_id": use_id, "subagent_type": subagent_type or "general-purpose"},
        )
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # every failure is an allow; the caller logs it
        print(f"subagent-cap: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
