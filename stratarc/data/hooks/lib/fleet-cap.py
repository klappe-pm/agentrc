#!/usr/bin/env python3
"""Machine-wide cap on concurrently running agents of one model (WI-101b).

The operator's instruction of 2026-09-24: at most 20 concurrent agents on
Fable (claude-fable-5-1). The cap and the model it applies to are
components.json budgets.fleet {"model", "concurrent"}, read from
<source root>/components.json; a null or absent block is no cap.
scripts/budgets.py validates the block, and only the user level
(components.json) may set it.

The source root is $STRATARC_SOURCE when set, else $LLM_ROOT. With neither
there is no components.json and so no cap.

The cap is advisory (the operator's decision of 2026-09-24): every agent is
counted, a dispatch or launch at or past the cap is warned and still runs,
and nothing here ever blocks one.

What counts, from the evidence a hook can see on this machine (checked live
on 2026-09-24, Claude Code 2.1.280): every Claude Code transcript under
${FLEET_CAP_PROJECTS_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects}, a
session's own <project>/<session-id>.jsonl and each subagent's
<project>/<session-id>/subagents/**/agent-<id>.jsonl, that

  - was written within FLEET_CAP_TTL_SECS (default 900): a transcript is
    appended on every model turn and tool result, so its mtime is the time
    of the agent's last activity, and one older than the TTL is stale;
  - names the fleet model on its last assistant entry that carries a real
    model (a session can change model part way, so the last one counts, and
    the runtime's "<synthetic>" entries are skipped);
  - has not finished: its last user or assistant entry (isMeta entries,
    slash command output, skipped) is not an assistant entry that ended its
    turn (stop_reason end_turn or stop_sequence), a tool result the runtime
    marked toolEndsTurn (a workflow agent's final StructuredOutput), or a
    user interruption. A finished subagent, and a main session idle at its
    prompt, run nothing. A parent waiting on a foreground subagent is
    running and counts.

~/.claude/sessions/<pid>.json was checked and rejected: it names no model
and holds one file per top-level session, no subagents. The tool-budget
state directory was rejected too: it names no model, and a subagent's calls
land in its parent's state file with no time of their own.

Only the tail of each recent transcript is read, growing from TAIL_BYTES up
to MAX_TAIL_BYTES until an entry naming a model is found, so a long
transcript costs a seek and a read or two.

The count is not atomic with the dispatch it checks: two dispatches checked
in the same instant can both see 19.

Modes, chosen by the first argument:

    check  (default) a PreToolUse payload for a subagent dispatch on stdin.
           Exit 0 when under the cap or not on the fleet model; exit 3, with
           the warning on stdout, when the new agent would run on the fleet
           model and the live count is already at or past the cap. The
           caller warns and allows either way. The new agent's model is tool_input.model (an
           alias such as "fable" or a full id), else CLAUDE_CODE_SUBAGENT_MODEL,
           else the dispatching agent's own current model, read from its
           transcript. A model an agent definition names in its frontmatter
           is not read.
    count  print {"model", "concurrent", "live"} as JSON.

Any other exit status is an internal error, which the caller ignores. Exit 2
is kept free, as in hooks/lib/tool-budget.py.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

OVER = 3
DEFAULT_TTL_SECS = 900
TAIL_BYTES = 64 * 1024
MAX_TAIL_BYTES = 4 * 1024 * 1024
FINISHED = {"end_turn", "stop_sequence"}
SYNTHETIC = "<synthetic>"
INTERRUPTED = "[Request interrupted by user"


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def home() -> str:
    return os.environ.get("HOME") or "/tmp"


def root() -> str:
    """The source root: STRATARC_SOURCE, else LLM_ROOT, else "" (no file-location fallback)."""
    return env("STRATARC_SOURCE") or env("LLM_ROOT")


def projects_dir() -> str:
    return env("FLEET_CAP_PROJECTS_DIR") or os.path.join(env("CLAUDE_CONFIG_DIR") or os.path.join(home(), ".claude"), "projects")


def ttl_secs() -> int:
    value = env("FLEET_CAP_TTL_SECS")
    return int(value) if value.isdigit() else DEFAULT_TTL_SECS


def config(path: Optional[str] = None) -> Optional[Tuple[str, int]]:
    """(model, concurrent) from components.json budgets.fleet; None when unset or malformed."""
    if not path:
        source = root()
        if not source:
            return None
        path = os.path.join(source, "components.json")
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    budgets = document.get("budgets") if isinstance(document, dict) else None
    fleet = budgets.get("fleet") if isinstance(budgets, dict) else None
    if not isinstance(fleet, dict):
        return None
    model, cap = fleet.get("model"), fleet.get("concurrent")
    if not isinstance(model, str) or not model.strip():
        return None
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        return None
    return model.strip(), cap


def same_model(named: str, model: str) -> bool:
    """True when named (a full id, an id with a context suffix, or a family alias such as "fable") is the fleet model."""
    named = named.strip().lower()
    model = model.strip().lower()
    if "[" in named:
        named = named.split("[", 1)[0]  # a context suffix such as [1m] names the same model
    if not named or not model:
        return False
    if named == model:
        return True
    parts = model.split("-")
    family = parts[1] if len(parts) >= 3 and parts[0] == "claude" else ""
    return bool(family) and named == family


def _entries(data: bytes, cut: bool) -> List[Dict[str, Any]]:
    lines = data.split(b"\n")
    if cut:
        lines = lines[1:]  # the first line is cut part way
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _ends_turn(kind: str, entry: Dict[str, Any], message: Dict[str, Any]) -> bool:
    """True when this last entry means the agent is running nothing.

    An assistant turn that ended; a tool result the runtime marked as ending
    the turn (a workflow agent's final StructuredOutput, toolEndsTurn); or a
    user interruption, which stops the turn in flight.
    """
    if kind == "assistant":
        return message.get("stop_reason") in FINISHED
    if entry.get("toolEndsTurn") is True:
        return True
    content = message.get("content")
    texts = [content] if isinstance(content, str) else [
        block.get("text") for block in content if isinstance(block, dict) and block.get("type") == "text"
    ] if isinstance(content, list) else []
    return any(isinstance(text, str) and text.startswith(INTERRUPTED) for text in texts)


def _state_of(entries: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """(model, finished) from entries oldest first. isMeta entries (slash command output) are skipped."""
    model = ""
    finished: Optional[bool] = None
    for entry in reversed(entries):
        kind = entry.get("type")
        if kind not in ("assistant", "user") or entry.get("isMeta") is True:
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if finished is None:
            finished = _ends_turn(kind, entry, message)
        named = message.get("model") if kind == "assistant" else None
        if isinstance(named, str) and named and named != SYNTHETIC:
            model = named
            break
    return model, bool(finished)


def transcript_state(path: str) -> Tuple[str, bool]:
    """(current model, finished) of one transcript; model "" when none is named in what was read."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = TAIL_BYTES
            while True:
                start = max(0, size - window)
                handle.seek(start)
                model, finished = _state_of(_entries(handle.read(size - start), start > 0))
                if model or start == 0 or window >= MAX_TAIL_BYTES:
                    return model, bool(finished)
                window *= 4
    except OSError:
        return "", False


def recent_transcripts(directory: str, now: float, ttl: int) -> List[str]:
    patterns = (os.path.join(directory, "*", "*.jsonl"), os.path.join(directory, "*", "*", "subagents", "**", "agent-*.jsonl"))
    found = set()
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            try:
                if now - os.stat(path).st_mtime <= ttl:
                    found.add(path)
            except OSError:
                continue
    return sorted(found)


def live(model: str, now: Optional[float] = None) -> List[str]:
    """Transcripts of the agents live on the fleet model right now."""
    moment = time.time() if now is None else now
    out = []
    for path in recent_transcripts(projects_dir(), moment, ttl_secs()):
        named, finished = transcript_state(path)
        if not finished and same_model(named, model):
            out.append(path)
    return out


def caller_transcript(payload: Dict[str, Any]) -> str:
    """The transcript of the agent making the call: a subagent's own when the payload names one."""
    path = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else ""
    agent = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else ""
    if not path or not agent:
        return path
    session_dir = path[: -len(".jsonl")] if path.endswith(".jsonl") else path
    matches = glob.glob(os.path.join(glob.escape(session_dir), "subagents", "**", f"agent-{glob.escape(agent)}.jsonl"), recursive=True)
    return matches[0] if matches else path


def dispatch_model(payload: Dict[str, Any]) -> str:
    """The model a dispatched agent will run on: tool_input.model, else CLAUDE_CODE_SUBAGENT_MODEL, else the caller's model."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    for named in (tool_input.get("model"), env("CLAUDE_CODE_SUBAGENT_MODEL")):
        if isinstance(named, str) and named.strip() and named.strip().lower() != "inherit":
            return named.strip()
    path = caller_transcript(payload)
    return transcript_state(path)[0] if path else ""


def warning(count: int, model: str, cap: int, what: str) -> str:
    """The advisory message at or past the cap; the operator decided on 2026-09-24 that the cap never blocks."""
    return (
        f"fleet-cap: {count} agents are already running on {model} on this machine, at or past the fleet cap of {cap} "
        f"(components.json budgets.fleet.concurrent). The cap is advisory, so this {what} still runs; consider waiting "
        f"for one to finish or using another model. An agent counts while its transcript was written in the last "
        f"{ttl_secs()} seconds and its turn has not ended."
    )


def check() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in ("", None, "PreToolUse"):
        return 0
    configured = config()
    if configured is None:
        return 0
    model, cap = configured
    if not same_model(dispatch_model(payload), model):
        return 0
    count = len(live(model))
    if count < cap:
        return 0
    print(warning(count, model, cap, "dispatch"))
    return OVER


def count() -> int:
    configured = config()
    model, cap = configured if configured else (None, None)
    print(json.dumps({"model": model, "concurrent": cap, "live": len(live(model)) if model else 0}))
    return 0


def main(argv: List[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "check"
    try:
        return count() if mode == "count" else check()
    except Exception as error:  # every failure is an allow; the caller logs it
        print(f"fleet-cap: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
