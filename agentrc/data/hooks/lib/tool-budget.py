#!/usr/bin/env python3
"""Live tool-call count and budget check, and the work-item join line.

The design is a launch-scoped tool-call budget;
hooks/tool-budget-guard.sh and hooks/session-start.sh are the only callers.
This file is deployed beside the hooks, so it reads the launcher's resolved
budget file directly rather than importing scripts/budgets.py, which is not.

Two modes, chosen by the first argument:

    pre-tool       (default) decide one tool call, and reserve or count it.
                   Exit 0 allows, with an optional {"systemMessage": ...} on
                   stdout when a count first reaches warn_at of a limit. Exit
                   3 denies, with the reason on stdout for the wrapper to log
                   and print.
    post-tool      confirm a reserved call that ran (PostToolUse or
                   PostToolUseFailure). Always exit 0, with the same optional
                   {"systemMessage": ...} on stdout when this confirmation is
                   the first to actually reach warn_at of a limit (#162): a
                   bare reservation at pre-tool time never warns on its own.
    tool           either of the two, chosen by the payload's
                   hook_event_name; what hooks/tool-budget-guard.sh runs.
    session-start  write the join line for a work-item session and print its
                   opening context: the work item, its goals and its limits.

Any other exit status is an internal error, which every caller treats as
allow. Exit 2 is kept free on purpose, so that a crash here can never be read
as the runtime's blocking status.

Every call counts, reads included (decision 4 of the record), and the count
is kept whether or not a budget is set: that count is the baseline the
default limits are later set from. A call this hook denies is not counted
toward total or per_tool, only toward denied.

Reservations (G-02 of the design record).
A sibling PreToolUse hook (prose, attribution, subagent cap) can deny a call
this hook admitted, and nothing at PreToolUse time shows that. So on a
runtime in CONFIRMING_RUNTIMES, a call whose payload carries a tool_use_id is
not counted when it is admitted: it is reserved under that id, in the
session's "reserved" map and, under a launch, in the launch's. A limit is
checked against the counted calls plus every open reservation of the budget
unit, so calls issued in parallel cannot each see the same old total and pass
a limit together. PostToolUse or PostToolUseFailure with the same id (a call
that ran and failed still ran) turns the reservation into a counted call,
once: the id is remembered in "confirmed_ids". A reservation is released
uncounted when a deny record in the guard event log names its id
(hooks/lib/guard-log.sh), applied on this hook's next run for that session. A
payload with no tool_use_id, and every runtime not in CONFIRMING_RUNTIMES,
keeps counting at PreToolUse. So total and per_tool are calls that ran where
confirmation is available, and calls this hook allowed elsewhere.

per_agent splits the counted calls by the payload's agent_id, with
"main" for a call that names no agent; limits stay session wide. started is
the time of the session's first call and is never rewritten
(the design record, B-01
and B-05).

The launch is the budget unit (the design record,
B-08). Under a launch, limits are checked against launches/<launch-id>.json,
one count shared by every session id seen under that launch (listed in its
sessions), so a resume that gets a new session id keeps counting where the
launch left off. Each session still keeps its own <session-id>.json, which
the status line and the reports read by session id, and gets a pointer
<session-id>.launch naming its launch on one line and its budget file on
the next. A session resumed with a plain runtime command carries no
LLM_ROOT_* variables; the pointer then finds its launch and budget file
(or ${LLM_ROOT_SESSIONS_DIR:-~/.agent-hooks/sessions}/<launch-id>/budget.json
for a pointer with one line). A launch-wide count that does not exist yet
starts from the per-session states of that launch, so a session launched
before the count existed keeps what it has spent.

The warning (B-09) is a systemMessage. On Claude Code that field is shown to
the operator and does not reach the model; a PreToolUse
hookSpecificOutput.additionalContext does (verified 2026-09-23, Claude Code
2.1.280, WI-32). The channel per runtime is in docs/runtimes/compatibility.md,
section live-checks-2026-09-23; moving the warning onto it is WI-38's.

wall_minutes (WI-101a) is warned the same way, once per unit at warn_at of
it, measured from the unit's started (the launch's first counted call under
a launch, else the session's). It is never denied here: an interactive
session is warned only, and a headless launch is stopped at wall_minutes by
scripts/session-launch.py (run_headless).

Environment, set by scripts/session-launch.py:
    LLM_ROOT_WORK_ITEM    work item id
    LLM_ROOT_LAUNCH_ID    id of the launch
    LLM_ROOT_SESSION_DIR  directory holding budget.json and loadout.json
    LLM_ROOT_BUDGET_FILE  the resolved budget, {"effective": {dotted: limit}}
Set by the wrapper: TOOL_BUDGET_RUNTIME (from the install path) and
TOOL_BUDGET_PPID (the session key when a payload carries no session id).
GUARD_LOG_DIR is where the deny records are read (the default is guard-log.sh's own).
Test overrides: TOOL_BUDGET_STATE_DIR, LLM_ROOT_TELEMETRY_DIR, LLM_ROOT_SESSIONS_DIR.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

DENY = 3
PRE_TOOL_EVENTS = {"", "PreToolUse", "BeforeTool", "preToolUse"}
DEFAULT_WARN_AT = 0.8
WALL_KEY = "wall_minutes"
JOIN_FILE = "work-item-sessions.jsonl"
# Launch-wide counts live one directory down, so every reader that globs
# <state dir>/*.json for per-session states (budget-report.py load_states,
# session-launch.py live_launches) never sees them and never counts a call
# twice.
LAUNCH_DIR = "launches"
# The payload field naming the dispatched agent that made a call (B-05).
# Verified 2026-09-23 on Claude Code 2.1.280 (WI-32): a subagent's PreToolUse
# payload carries agent_id (and agent_type), while the main session's call
# carries neither; docs/runtimes/evidence/claude-code-2.1.280-subagent-pretooluse.json.
AGENT_KEY = "agent_id"
MAIN_AGENT = "main"
UNATTRIBUTED = "unattributed"
# Post events that confirm a reserved call ran (G-02).
POST_TOOL_EVENTS = {"PostToolUse", "PostToolUseFailure"}
# Runtimes whose PostToolUse and PostToolUseFailure payloads carry the same
# tool_use_id as the PreToolUse payload, so a reservation can be confirmed.
# Claude Code: verified 2026-09-24 from the Claude Code 2.1.273 binary, whose
# PostToolUse and PostToolUseFailure hook inputs both set tool_use_id; the
# PreToolUse payload carries it too
# (docs/runtimes/evidence/claude-code-2.1.280-subagent-pretooluse.json). Every
# other runtime keeps counting at PreToolUse until a live check shows its post
# event fires for every tool with that id (docs/runtimes/compatibility.md).
CONFIRMING_RUNTIMES = {"claude"}
# How many confirmed ids a state remembers, so a repeated post event for one
# call is never counted twice.
CONFIRMED_MEMORY = 256


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(stamp: Any) -> Optional[datetime.datetime]:
    """An ISO time written by now() as an aware datetime; None when it cannot be read."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        moment = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def minutes_since(stamp: Any) -> Optional[float]:
    """Minutes from an ISO time written by now() to this moment; None when it cannot be read."""
    moment = parse_time(stamp)
    if moment is None:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds() / 60.0


def first_start(record: Dict[str, Any]) -> str:
    """A state's started, else its updated (a record written before started existed); "" when neither."""
    return next((value for value in (record.get("started"), record.get("updated")) if isinstance(value, str) and value), "")


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def home() -> str:
    return os.environ.get("HOME") or "/tmp"


def state_dir() -> str:
    return env("TOOL_BUDGET_STATE_DIR") or os.path.join(home(), ".agent-hooks", "state", "tool-budget")


def telemetry_dir() -> str:
    return env("LLM_ROOT_TELEMETRY_DIR") or os.path.join(home(), ".agent-hooks", "telemetry")


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def read_payload() -> Dict[str, Any]:
    data = json.loads(sys.stdin.read() or "{}")
    if not isinstance(data, dict):
        raise ValueError("payload is not a JSON object")
    return data


def text(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def string_list(value: Any) -> List[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def write_state(path: str, document: Dict[str, Any]) -> None:
    """Replace a state file atomically, so a reader never sees half of one."""
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True)
    os.replace(temporary, path)


def reservations(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """A document's open reservations, {tool_use_id: {"tool", "since", ...}}."""
    value = document.get("reserved")
    if not isinstance(value, dict):
        return {}
    return {key: entry for key, entry in value.items() if isinstance(key, str) and isinstance(entry, dict)}


def denied_ids(candidates: set) -> set:
    """The ids among candidates that a guard deny record names.

    Reads today's and yesterday's guard event log (UTC, the file names
    guard-log.sh writes), and only when there is a candidate to look for. A
    file is parsed only when one of the ids appears in its text at all, so
    the common case costs one read per file and no JSON parsing.
    """
    if not candidates:
        return set()
    directory = env("GUARD_LOG_DIR") or os.path.join(home(), ".agent-hooks", "telemetry")
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


def prune_reservations(documents: List[Dict[str, Any]]) -> None:
    """Release, uncounted, every reservation named by a deny record."""
    open_ids = {key for document in documents for key in reservations(document)}
    released = denied_ids(open_ids)
    for document in documents:
        kept = {}
        for key, entry in reservations(document).items():
            if key not in released:
                kept[key] = entry
        if kept or "reserved" in document:
            document["reserved"] = kept


def sessions_dir() -> str:
    return env("LLM_ROOT_SESSIONS_DIR") or os.path.join(home(), ".agent-hooks", "sessions")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def pointer_path(session_id: str) -> str:
    """<state dir>/<session-id>.launch: the launch a session was counted under."""
    return os.path.join(state_dir(), safe_name(session_id) + ".launch")


def read_pointer_lines(session_id: str) -> Tuple[str, str]:
    """(launch id, budget file) from a session's pointer, either "" when absent.

    Line one is the launch id; line two, when present, is the budget file the
    launch ran under, which a launch made with --session-root keeps outside
    the default sessions directory.
    """
    try:
        with open(pointer_path(session_id), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return "", ""
    launch_id = lines[0].strip() if lines else ""
    if not launch_id or safe_name(launch_id) != launch_id:
        return "", ""
    budget_file = lines[1].strip() if len(lines) > 1 else ""
    return launch_id, budget_file if os.path.isabs(budget_file) else ""


def read_pointer(session_id: str) -> str:
    return read_pointer_lines(session_id)[0]


def write_pointer(session_id: str, launch_id: str, budget_file: str) -> None:
    if read_pointer_lines(session_id) == (launch_id, budget_file if os.path.isabs(budget_file) else ""):
        return
    path = pointer_path(session_id)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(launch_id + "\n" + (budget_file + "\n" if budget_file else ""))
    os.replace(temporary, path)


def launch_for(session_id: str) -> Tuple[str, str]:
    """(launch id, budget file) that hold this session to a launch's budget.

    A launched session carries both in its environment. A session resumed
    with a plain runtime command carries neither, so the pointer written on
    an earlier counted call names its launch and the budget file it ran
    under; a pointer without that line falls back to the launch's budget.json
    in the sessions directory (B-08). Neither found is no launch: the session
    is counted on its own, with no limits.
    """
    launch_id = env("LLM_ROOT_LAUNCH_ID")
    budget_file = env("LLM_ROOT_BUDGET_FILE")
    if launch_id or budget_file:
        return launch_id, budget_file
    launch_id, budget_file = read_pointer_lines(session_id)
    if not launch_id:
        return "", ""
    return launch_id, budget_file or os.path.join(sessions_dir(), launch_id, "budget.json")


def seed_launch(directory: str, launch_id: str, session_id: str) -> Dict[str, Any]:
    """A launch-wide count built from the per-session states of that launch.

    Used only when launches/<launch-id>.json does not exist yet: a session
    launched before the launch-wide count existed has its calls in its own
    state alone, and starting the launch at zero would hand it a fresh budget
    mid-launch. Each state whose launch_id is this launch, and the calling
    session's own, is summed once. The launch starts at the earliest of those
    sessions' starts, so its wall_minutes clock is not restarted either.
    """
    total = denied = 0
    per_tool: Dict[str, int] = {}
    warned: List[str] = []
    sessions: List[str] = []
    earliest: Optional[Tuple[datetime.datetime, str]] = None
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        state = read_json(os.path.join(directory, name))
        if not isinstance(state, dict):
            continue
        sid = state.get("session_id") if isinstance(state.get("session_id"), str) else name[: -len(".json")]
        if state.get("launch_id") != launch_id and not (sid == session_id and not state.get("launch_id")):
            continue
        total += state.get("total") if is_count(state.get("total")) else 0
        denied += state.get("denied") if is_count(state.get("denied")) else 0
        tools = state.get("per_tool") if isinstance(state.get("per_tool"), dict) else {}
        for tool, count in tools.items():
            if isinstance(tool, str) and is_count(count):
                per_tool[tool] = per_tool.get(tool, 0) + count
        warned.extend(key for key in string_list(state.get("warned")) if key not in warned)
        if sid not in sessions:
            sessions.append(sid)
        start = first_start(state)
        moment = parse_time(start)
        if moment is not None and (earliest is None or moment < earliest[0]):
            earliest = (moment, start)
    seeded: Dict[str, Any] = {"total": total, "denied": denied, "per_tool": per_tool, "warned": warned, "sessions": sessions}
    if earliest is not None:
        seeded["started"] = earliest[1]
    return seeded


def limits(path: Optional[str] = None) -> Tuple[Dict[str, Any], str, str]:
    """The effective limits from a budget file (LLM_ROOT_BUDGET_FILE by default), its work item, and its started.

    started is when the launcher started the runtime (WI-101a), the clock a
    headless launch is stopped by; "" for a file written before it existed.
    An unset, unreadable or malformed file is no budget at all: the call is
    still counted and is never denied on the strength of a file that could
    not be read.
    """
    path = env("LLM_ROOT_BUDGET_FILE") if path is None else path
    if not path:
        return {}, "", ""
    document = read_json(path)
    if not isinstance(document, dict) or not isinstance(document.get("effective"), dict):
        print(f"tool-budget: budget file {path} is unreadable; counting without limits", file=sys.stderr)
        return {}, "", ""
    work_item = document.get("work_item")
    started = document.get("started")
    return (
        document["effective"],
        work_item if isinstance(work_item, str) else "",
        started if parse_time(started) is not None else "",
    )


def warn_fraction(effective: Dict[str, Any]) -> float:
    value = effective.get("warn_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1:
        return float(value)
    return DEFAULT_WARN_AT


def join_line(work_item: str, session_id: str, runtime: str, source: str, launch_id: Optional[str] = None) -> None:
    """Append one work-item join line unless one already names this session."""
    directory = telemetry_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, JOIN_FILE)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("session_id") == session_id and row.get("work_item") == work_item:
                return
        record = {
            "work_item": work_item,
            "session_id": session_id,
            "runtime": runtime,
            "launch_id": env("LLM_ROOT_LAUNCH_ID") if launch_id is None else launch_id,
            "started": now(),
            "source": source,
        }
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(record) + "\n")


def tool_event(post: Optional[bool] = None) -> int:
    """Decide and reserve a call at PreToolUse, or confirm one at a post event.

    post None reads which from the payload's hook_event_name.
    """
    payload = read_payload()
    event = text(payload, "hook_event_name")
    if post is None:
        post = event in POST_TOOL_EVENTS
    if event not in (POST_TOOL_EVENTS if post else PRE_TOOL_EVENTS):
        return 0
    tool = text(payload, "tool_name") or "unknown"
    runtime = env("TOOL_BUDGET_RUNTIME") or "claude"
    use_id = text(payload, "tool_use_id")
    reserving = runtime in CONFIRMING_RUNTIMES and bool(use_id)
    if post and not reserving:
        # Counted at PreToolUse already, or nothing names the call to confirm.
        return 0
    session_id = text(payload, "session_id") or text(payload, "conversation_id") or env("TOOL_BUDGET_PPID")
    if not session_id:
        raise ValueError("no session id and no parent pid")
    agent = text(payload, AGENT_KEY) or MAIN_AGENT
    work_item = env("LLM_ROOT_WORK_ITEM")
    launch_id, budget_file = launch_for(session_id)
    effective, budget_work_item, budget_started = limits(budget_file)
    work_item_named = work_item or budget_work_item

    directory = state_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, safe_name(session_id) + ".json")
    launch_path = ""
    if launch_id:
        os.makedirs(os.path.join(directory, LAUNCH_DIR), exist_ok=True)
        launch_path = os.path.join(directory, LAUNCH_DIR, safe_name(launch_id) + ".json")

    # Always the session lock first, then the launch lock, so two sessions of
    # one launch can never wait on each other in opposite orders.
    with open(path + ".lock", "a", encoding="utf-8") as lock, open((launch_path or path) + ".lock", "a", encoding="utf-8") as launch_lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if launch_path:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)
        state = read_json(path)
        if not isinstance(state, dict):
            state = {}
        per_tool = state.get("per_tool") if isinstance(state.get("per_tool"), dict) else {}
        total = state.get("total") if is_count(state.get("total")) else 0
        per_agent = state.get("per_agent") if isinstance(state.get("per_agent"), dict) else {}
        if "per_agent" not in state and total:
            # Counted before per_agent existed: those calls keep a bucket of
            # their own, so the breakdown still sums to the total.
            per_agent = {UNATTRIBUTED: total}
        agent_count = per_agent.get(agent) if is_count(per_agent.get(agent)) else 0
        # Written on the session's first call and never again: the baseline
        # clock starts at the oldest one (B-01). A state written before this
        # field existed takes its last update, the earliest time the hook is
        # known to have counted the session, never the time of this call.
        started = first_start(state) or now()
        denied = state.get("denied") if is_count(state.get("denied")) else 0
        warned = string_list(state.get("warned"))
        tool_count = per_tool.get(tool) if is_count(per_tool.get(tool)) else 0

        # The launch's shared count (B-08): every session of one launch, a
        # resumed one included, is held to the same limits from the same
        # count. Without a launch the session's own count is the budget unit.
        launch = read_json(launch_path) if launch_path else None
        if launch_path and not isinstance(launch, dict) and not os.path.exists(launch_path):
            launch = seed_launch(directory, launch_id, session_id)
        launch = launch if isinstance(launch, dict) else {}
        launch_per_tool = launch.get("per_tool") if isinstance(launch.get("per_tool"), dict) else {}
        launch_total = launch.get("total") if is_count(launch.get("total")) else 0
        launch_denied = launch.get("denied") if is_count(launch.get("denied")) else 0
        launch_warned = string_list(launch.get("warned"))
        launch_tool = launch_per_tool.get(tool) if is_count(launch_per_tool.get(tool)) else 0
        # The launch's clock: when the launcher started the runtime, as its
        # budget file records, so a warning and a headless stop agree. A
        # budget file written before that field existed leaves the launch's
        # own first counted call, and a launch file written before started
        # existed takes its last update, as a session state does.
        launch_started = budget_started or first_start(launch) or now()
        if launch_path:
            unit_total, unit_tool, unit_warned, unit_started = launch_total, launch_tool, launch_warned, launch_started
        else:
            unit_total, unit_tool, unit_warned, unit_started = total, tool_count, warned, started

        def mark_warned(key: str) -> None:
            """Record a warned key on the unit, and on the session too when the unit is the launch."""
            unit_warned.append(key)
            if unit_warned is not warned:
                warned.append(key)

        # Open reservations (G-02): a sibling's deny record releases one
        # first; what is left counts toward the unit's limits.
        moment = time.time()
        prune_reservations([state, launch] if launch_path else [state])
        session_reserved = reservations(state)
        unit_reserved = reservations(launch) if launch_path else session_reserved
        confirmed_ids = string_list(state.get("confirmed_ids"))

        over: Optional[Tuple[str, int, int]] = None
        message = ""
        # Set only when this call is actually confirmed to have run this
        # time: an immediate (non-reserving) pre-tool count, or a post event
        # that turns a reservation into a counted call for the first time. A
        # bare reservation is not confirmed_now, so it earns no warning (#162):
        # warning on the projected count at reservation time marks the key
        # even when a sibling guard later denies and prunes that reservation
        # uncounted, leaving the real confirmed call that later reaches the
        # same threshold silently unwarned.
        confirmed_now = False
        checks: List[Tuple[str, int, int]] = []
        if post:
            if use_id not in confirmed_ids:
                entry = session_reserved.pop(use_id, None) or {}
                counted_tool = entry.get("tool") if isinstance(entry.get("tool"), str) else tool
                counted_agent = entry.get("agent") if isinstance(entry.get("agent"), str) else agent
                total += 1
                per_tool[counted_tool] = (per_tool.get(counted_tool) if is_count(per_tool.get(counted_tool)) else 0) + 1
                per_agent[counted_agent] = (per_agent.get(counted_agent) if is_count(per_agent.get(counted_agent)) else 0) + 1
                if launch_path:
                    unit_reserved.pop(use_id, None)
                    launch_total += 1
                    launch_per_tool[counted_tool] = (
                        launch_per_tool.get(counted_tool) if is_count(launch_per_tool.get(counted_tool)) else 0
                    ) + 1
                confirmed_ids = (confirmed_ids + [use_id])[-CONFIRMED_MEMORY:]
                confirmed_now = True
                # Warn on the count this confirmation actually reached, not
                # the projected count a since-pruned reservation once held.
                # per_tool[counted_tool] (or launch_per_tool's) was just
                # incremented above, so it is already a valid int; no
                # is_count guard needed to read it back.
                unit_count_total = launch_total if launch_path else total
                unit_count_tool = (launch_per_tool if launch_path else per_tool)[counted_tool]
                for key, count in (
                    ("tool_calls.total", unit_count_total),
                    (f"tool_calls.per_tool.{counted_tool}", unit_count_tool),
                ):
                    limit = effective.get(key)
                    if is_count(limit):
                        checks.append((key, count, limit))
        else:
            # This call's own reservation, if the runtime delivered it twice,
            # is not counted against it a second time.
            others = [entry for key, entry in unit_reserved.items() if key != use_id]
            open_total = len(others)
            open_tool = sum(1 for entry in others if entry.get("tool") == tool)
            # Every limit that applies to this call, as (key, count after it, limit).
            for key, count in (
                ("tool_calls.total", unit_total + open_total + 1),
                (f"tool_calls.per_tool.{tool}", unit_tool + open_tool + 1),
            ):
                limit = effective.get(key)
                if is_count(limit):
                    checks.append((key, count, limit))
            over = next((c for c in checks if c[1] > c[2]), None)

        notes: List[str] = []
        if over:
            denied += 1
            launch_denied += 1
        else:
            fraction = warn_fraction(effective)
            if not post:
                if reserving:
                    # Counted, and only then eligible to warn, when a post
                    # event confirms it ran (G-02).
                    session_reserved[use_id] = {"tool": tool, "agent": agent, "since": moment}
                    if launch_path:
                        unit_reserved[use_id] = {"tool": tool, "session": session_id, "since": moment}
                else:
                    total += 1
                    per_tool[tool] = tool_count + 1
                    per_agent[agent] = agent_count + 1
                    launch_total += 1
                    launch_per_tool[tool] = launch_tool + 1
                    confirmed_now = True
            if confirmed_now:
                for key, count, limit in checks:
                    if key not in unit_warned and limit and count >= fraction * limit:
                        mark_warned(key)
                        notes.append(f"{key} is at {count} of {limit} ({limit - count} left)")
            # Wall time (WI-101a): warned once at warn_at, like a tool-call
            # limit, and never denied here; only a headless launch is stopped
            # at wall_minutes, by the launcher's clock. The clock starts at the
            # unit's first counted call: the launch's under a launch. Elapsed
            # time is real whether or not this call is reserved or confirmed,
            # so it is still checked on every pre-tool call.
            wall_noted = False
            if not post:
                wall_limit = effective.get(WALL_KEY)
                if is_count(wall_limit) and WALL_KEY not in unit_warned:
                    elapsed = minutes_since(unit_started)
                    if elapsed is not None and elapsed >= fraction * wall_limit:
                        mark_warned(WALL_KEY)
                        whole = int(elapsed)
                        notes.append(f"{WALL_KEY} is at {whole} of {wall_limit} minutes ({max(wall_limit - whole, 0)} left)")
                        wall_noted = True
            if notes:
                label = f" for {work_item_named}" if work_item_named else ""
                prefix = "Budget" if wall_noted else "Tool-call budget"
                message = prefix + label + ": " + "; ".join(notes) + ". Plan to finish within it."

        joined = state.get("joined") if isinstance(state.get("joined"), str) else ""
        if work_item and joined != work_item:
            join_line(work_item, session_id, runtime, "tool-budget", launch_id)
            joined = work_item

        state.update(
            {
                "session_id": session_id,
                "work_item": work_item or state.get("work_item") or budget_work_item or "",
                "launch_id": launch_id or state.get("launch_id") or "",
                "runtime": runtime,
                "total": total,
                "per_tool": per_tool,
                "per_agent": per_agent,
                "started": started,
                "denied": denied,
                "warned": sorted(set(warned)),
                "joined": joined,
                "updated": now(),
            }
        )
        if session_reserved or "reserved" in state:
            state["reserved"] = session_reserved
        if confirmed_ids:
            state["confirmed_ids"] = confirmed_ids
        write_state(path, state)
        if launch_path:
            sessions = string_list(launch.get("sessions"))
            if session_id not in sessions:
                sessions.append(session_id)
            launch.update(
                {
                    "launch_id": launch_id,
                    "work_item": work_item_named or launch.get("work_item") or "",
                    "session_id": session_id,
                    "sessions": sessions,
                    "total": launch_total,
                    "per_tool": launch_per_tool,
                    "started": launch_started,
                    "denied": launch_denied,
                    "warned": sorted(set(launch_warned)),
                    "updated": now(),
                }
            )
            if unit_reserved or "reserved" in launch:
                launch["reserved"] = unit_reserved
            write_state(launch_path, launch)
            write_pointer(session_id, launch_id, budget_file)

    if over:
        key, count, limit = over
        item = work_item_named or "this session"
        where = f"loadouts/{work_item_named}.json" if work_item_named else "the work item's loadout"
        raise_hint = (
            f", or raise it for this launch with session-launch.py raise {launch_id} {key}=<limit> --reason \"<why>\""
            if launch_id
            else ""
        )
        unit = "this launch's" if launch_path else "this session's"
        print(
            f"tool-budget-guard: denied {tool} for {item}: the {key} budget is spent "
            f"(this call would be number {count} against a limit of {limit}, counting {unit} calls). "
            f"Finish up and end the session{raise_hint}, or raise {key} in {where} within the project's budget."
        )
        return DENY
    if message:
        print(json.dumps({"systemMessage": message}))
    return 0


def session_start() -> int:
    work_item = env("LLM_ROOT_WORK_ITEM")
    if not work_item:
        return 0
    try:
        payload = read_payload()
    except ValueError:
        payload = {}
    # A resume is a start of the same launch (B-08, S-14): the goals and the
    # limits are printed again, and a resumed session that got a new id is
    # joined with source resume. join_line skips a session id already joined,
    # so a resume that keeps its id writes no second line.
    source = text(payload, "source")
    if source not in ("", "startup", "resume"):
        return 0
    session_id = text(payload, "session_id")
    runtime = env("TOOL_BUDGET_RUNTIME") or "claude"
    if session_id:
        try:
            join_line(work_item, session_id, runtime, "resume" if source == "resume" else "session-start")
        except OSError as error:
            print(f"tool-budget: join line not written: {error}", file=sys.stderr)

    session_dir = env("LLM_ROOT_SESSION_DIR")
    loadout = read_json(os.path.join(session_dir, "loadout.json")) if session_dir else None
    goals = loadout.get("goals") if isinstance(loadout, dict) else None
    goals = [g for g in goals if isinstance(g, str) and g.strip()] if isinstance(goals, list) else []
    effective, _, _ = limits()
    set_limits = [(k, v) for k, v in sorted(effective.items()) if k != "warn_at" and is_count(v)]

    lines = [f"Work item {work_item}: this session was launched for it."]
    if goals:
        lines.append("Goals:")
        lines.extend(f"- {goal.strip()}" for goal in goals)
    else:
        lines.append("Goals: none recorded in the loadout.")
    if set_limits:
        lines.append("Budget limits in force:")
        lines.extend(f"- {key}: {value}" for key, value in set_limits)
        lines.append(f"- warns at {int(round(warn_fraction(effective) * 100))}% of a limit")
    else:
        lines.append("Budget: limits are unset during the baseline period; tool calls are counted, not capped.")
    # The trailing blank line separates this block from whatever the rest of
    # session-start.sh prints after it.
    print("\n".join(lines) + "\n")
    return 0


def main(argv: List[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "pre-tool"
    try:
        if mode == "session-start":
            return session_start()
        return tool_event({"pre-tool": False, "post-tool": True}.get(mode))
    except Exception as error:  # every failure is an allow; the caller logs it
        print(f"tool-budget: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
