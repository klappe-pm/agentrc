#!/usr/bin/env python3
"""Render the status line for a running session.

Reads the status line payload on stdin and prints two lines: what this session
is (project, model, context, cost, id) and what it is for (the goal).

The payload already carries cost, context window, model, directory, and git
worktree, so nothing here parses a transcript to find them. The one field it
cannot supply is the goal, which comes from a bounded read that does not grow
with transcript size. That keeps the whole render well inside the budget for
something that runs on every frame.

Everything is optional and every failure is silent. A status line that raises
or prints a traceback is worse than one that prints a shorter line, so missing
fields degrade and nothing here can fail a render.

Payload fields consumed (confirmed against the published schema):
  session_id, session_name, transcript_path
  model.display_name
  workspace.current_dir, workspace.git_worktree
  cost.total_cost_usd
  context_window.used_percentage

This is the entry point the runtime invokes, not a helper behind a shell
wrapper. Hook entry points in this repository are shell scripts, but a shell
wrapper here could never be deployed: staging copies hooks/*.sh only for hooks
that are registered in hooks.json and opted into in the control plane, and a
status line is not an event hook so it can hold no such row. hooks/lib is
copied wholesale, so the entry point lives here and the wrapper's guards were
folded in below.

A session launched from a work item carries LLM_ROOT_BUDGET_FILE, and the
status line inherits it. When it is set, the identity line gains one segment,
"<work item> · tools <n>/<limit>", read from two small files: the launcher's
budget.json and this session's tool-budget state file. A leading "!" marks a
count at or past the warning threshold and "!!" one past the limit. The check
is repeated here in a few lines rather than imported from scripts/budgets.py,
because scripts/ is not deployed beside hooks/lib/. No token count is shown:
the payload carries no transcript total and a full transcript scan per frame
is exactly what this file avoids. Unset, the output is unchanged.

The identity line also carries the program counts (O-06 in
the design record),
"<n> running · <n> to review · <n> to decide", read from the cache
program-summary.py writes from program-status.py snapshot --json. A frame
never runs the snapshot: an old cache starts one detached refresh, one older
than an hour is not shown, and all zero counts add nothing.

The identity line also carries "agent-graph <nodes>" when the agent-graph checkout is
present, read from ${XDG_CACHE_HOME:-$HOME/.cache}/agent-graph/statusline-nodes, the
file the checkout's wiring/statusline.sh writes. A frame never runs the CLI: a missing
or old cache starts one detached wiring/statusline.sh a minute, and a file that is not
a plain integer, or a missing checkout, adds nothing.

Env:
  AGENT_GRAPH_ROOT              the agent-graph checkout; unset means no segment
  LLM_ROOT_BUDGET_FILE          the session's budget.json; enables the segment
  TOOL_BUDGET_STATE_DIR         default ~/.agent-hooks/state/tool-budget
  PROGRAM_SUMMARY_CACHE         default ~/.agent-hooks/state/program-summary.json
  SESSION_STATUSLINE_GOAL_MODE  summary (default), clean, or original
  SESSION_STATUSLINE_NO_GOAL    set to 1 to print the identity line only
  SESSION_STATUSLINE_DISABLE    set to 1 to print nothing at all
  RUNTIME_HOOKS_DISABLE         set to 1 to print nothing at all
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import sys
import time
from typing import Any

SEPARATOR = "  ·  "


def _load_module(filename: str, name: str):
    target = pathlib.Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dig(payload: Any, *path: str) -> Any:
    """Walk nested keys, returning None the moment one is missing."""
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def identity_line(payload: dict[str, Any]) -> str:
    """What this session is, from the payload alone."""
    parts: list[str] = []

    current_dir = _dig(payload, "workspace", "current_dir")
    if isinstance(current_dir, str) and current_dir:
        parts.append(pathlib.PurePath(current_dir).name or current_dir)

    worktree = _dig(payload, "workspace", "git_worktree")
    if isinstance(worktree, str) and worktree:
        parts.append(worktree)

    model = _dig(payload, "model", "display_name")
    if isinstance(model, str) and model:
        parts.append(model)

    used = _dig(payload, "context_window", "used_percentage")
    if isinstance(used, (int, float)):
        parts.append(f"{int(used)}% ctx")

    cost = _dig(payload, "cost", "total_cost_usd")
    if isinstance(cost, (int, float)) and cost > 0:
        parts.append(f"${cost:.2f}")

    name = payload.get("session_name")
    if isinstance(name, str) and name:
        parts.append(name)
    else:
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            parts.append(session_id[:8])

    return SEPARATOR.join(parts)


def goal_line(payload: dict[str, Any]) -> str:
    """What this session is for, from a bounded read of its transcript."""
    if os.environ.get("SESSION_STATUSLINE_NO_GOAL") == "1":
        return ""
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return ""
    path = pathlib.Path(transcript).expanduser()
    if not path.is_file():
        return ""
    module = _load_module("session-card.py", "_session_card")
    if module is None:
        return ""
    mode = os.environ.get("SESSION_STATUSLINE_GOAL_MODE", "summary")
    return module.read_goal_bounded(path, goal_mode=mode)


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def budget_segment(payload: dict[str, Any]) -> str:
    """The work item and its tool calls against the limit, or "" when unset."""
    budget_file = os.environ.get("LLM_ROOT_BUDGET_FILE")
    if not budget_file:
        return ""
    try:
        budget = _read_json(pathlib.Path(budget_file).expanduser())
        if not budget:
            return ""
        effective = budget.get("effective")
        effective = effective if isinstance(effective, dict) else {}
        work_item = str(budget.get("work_item") or os.environ.get("LLM_ROOT_WORK_ITEM") or "")

        state: dict[str, Any] = {}
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id and "/" not in session_id and not session_id.startswith("."):
            state_dir = os.environ.get("TOOL_BUDGET_STATE_DIR") or str(
                pathlib.Path.home() / ".agent-hooks" / "state" / "tool-budget"
            )
            state = _read_json(pathlib.Path(state_dir).expanduser() / f"{session_id}.json")
        measured = {"tool_calls.total": _count(state.get("total")) or 0}
        per_tool = state.get("per_tool")
        if isinstance(per_tool, dict):
            for name, value in per_tool.items():
                if _count(value) is not None:
                    measured[f"tool_calls.per_tool.{name}"] = _count(value)

        # The same comparison scripts/budgets.py check() makes: over past the
        # limit, warn at or past warn_at of it.
        warn_at = effective.get("warn_at")
        warn_at = warn_at if isinstance(warn_at, (int, float)) and not isinstance(warn_at, bool) else 0.8
        mark = ""
        for key, value in measured.items():
            limit = _count(effective.get(key))
            if limit is None:
                continue
            if value > limit:
                mark = "!!"
                break
            if limit and value >= warn_at * limit:
                mark = "!"

        total = measured["tool_calls.total"]
        limit = _count(effective.get("tool_calls.total"))
        tools = f"tools {total}/{limit}" if limit is not None else f"tools {total}"
        text = " · ".join(part for part in (work_item, tools) if part)
        return f"{mark} {text}" if mark else text
    except Exception:
        return ""


def program_segment() -> str:
    """The O-06 program counts from program-summary.py's cache, or ""."""
    try:
        module = _load_module("program-summary.py", "_program_summary")
        return module.cached_segment() if module is not None else ""
    except Exception:
        return ""


AGENT_GRAPH_REFRESH_AFTER = 60  # seconds before the cached node count is refreshed


def _agent_graph_root() -> pathlib.Path | None:
    configured = os.environ.get("AGENT_GRAPH_ROOT")
    return pathlib.Path(configured) if configured else None


def _agent_graph_cache() -> pathlib.Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = pathlib.Path(base) if base else pathlib.Path.home() / ".cache"
    return root / "agent-graph" / "statusline-nodes"


def _claim_agent_graph_refresh(lock: pathlib.Path) -> bool:
    """Take the once-a-minute refresh claim; False when a refresh was started within the last minute."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - lock.stat().st_mtime < AGENT_GRAPH_REFRESH_AFTER:
            return False
        aside = lock.with_name(f"{lock.name}.{os.getpid()}.{time.monotonic_ns()}")
        os.rename(lock, aside)
        aside.unlink()
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def _refresh_agent_graph(script: pathlib.Path, cache: pathlib.Path) -> None:
    """Start one detached wiring/statusline.sh, which rewrites the cache file; never waits for it."""
    import subprocess  # here, not at the top: a frame with a fresh cache never needs it

    if not _claim_agent_graph_refresh(cache.with_name(cache.name + ".refresh")):
        return
    env = dict(os.environ)
    binary = pathlib.Path.home() / ".local" / "bin" / "agent-graph"
    if not env.get("AGENT_GRAPH_BIN") and os.access(binary, os.X_OK):
        env["AGENT_GRAPH_BIN"] = str(binary)
    subprocess.Popen(
        ["/bin/bash", str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def agent_graph_segment() -> str:
    """The agent-graph node count segment from the cache wiring/statusline.sh writes, or "".

    A frame never runs the CLI: a missing or old cache starts one detached refresh a
    minute. Nothing shows when the agent-graph checkout is missing or the cache does
    not hold a plain integer.
    """
    try:
        root = _agent_graph_root()
        if root is None:
            return ""
        script = root / "wiring" / "statusline.sh"
        if not script.is_file():
            return ""
        cache = _agent_graph_cache()
        try:
            text = cache.read_text(encoding="utf-8").strip()
            age = time.time() - cache.stat().st_mtime
        except OSError:
            text, age = "", None
        if age is None or age > AGENT_GRAPH_REFRESH_AFTER:
            _refresh_agent_graph(script, cache)
        return f"agent-graph {text}" if re.fullmatch(r"[0-9]+", text) else ""
    except Exception:
        return ""


def render(payload: dict[str, Any]) -> str:
    lines = [identity_line(payload)]
    for segment in (budget_segment(payload), program_segment(), agent_graph_segment()):
        if segment:
            lines[0] = SEPARATOR.join(part for part in (lines[0], segment) if part)
    goal = goal_line(payload)
    if goal:
        lines.append(goal)
    return "\n".join(line for line in lines if line)


def main() -> int:
    if os.environ.get("RUNTIME_HOOKS_DISABLE") == "1":
        return 0
    if os.environ.get("SESSION_STATUSLINE_DISABLE") == "1":
        return 0
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        text = render(payload)
    except Exception:
        # A status line renders on every frame. Degrading to the identity line
        # is always better than printing a traceback into the interface.
        try:
            text = identity_line(payload)
        except Exception:
            return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
