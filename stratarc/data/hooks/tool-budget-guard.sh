#!/usr/bin/env bash
# PreToolUse hook (BeforeTool on Gemini CLI): count every tool call, and deny
# one that would pass the session's tool-call budget. Also registered for
# PostToolUse and PostToolUseFailure, where the engine counts a call that
# PreToolUse reserved.
#
# WI-30, the design record. No
# runtime caps tool calls itself, so the count lives here, the way
# subagent-cap-guard.sh counts live subagents. Every call counts, reads
# included (decision 4 of that record), and the count is kept whether or not
# a budget is set, because those counts are the baseline the first default
# limits are set from.
#
# The logic is in lib/tool-budget.py, one python3 process per call. It keeps
# one state file per session under ~/.agent-hooks/state/tool-budget/ (total,
# per tool, denied, the keys already warned about), guarded by flock, and on
# a work-item session's first call appends the join line to
# ~/.agent-hooks/telemetry/work-item-sessions.jsonl. That line is OpenCode's
# only join, since OpenCode delivers no SessionStart.
#
# Limits come from LLM_ROOT_BUDGET_FILE, written by scripts/session-launch.py
# ("tool_calls.total" and "tool_calls.per_tool.<tool>" under "effective").
# Past a limit the call is denied with exit 2 and the reason on stderr: the
# blocking contract Claude Code, Codex and Gemini CLI all document for a
# pre-tool hook, rather than Claude Code's JSON decision shape. The first
# call to reach warn_at of a limit is allowed with a systemMessage on stdout,
# once per limit per session.
#
# The engine answers with its own exit status: 0 allow, 3 deny, anything
# else an internal error. Exit 2 is never the engine's, so a python3 crash
# cannot be mistaken for a deny.
#
# Fail-open contract: python3 missing, a malformed payload, an unwritable
# state directory, or an unreadable budget file allows the call. A bug here
# must never block tool use.

set -u
[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$HOOK_DIR/lib/log.sh"
# shellcheck source=lib/guard-log.sh
[ -f "$HOOK_DIR/lib/guard-log.sh" ] && source "$HOOK_DIR/lib/guard-log.sh"
command -v guard_log_event >/dev/null 2>&1 || guard_log_event() { :; }

trap 'log_warn "tool-budget-guard: internal error at line ${LINENO}"; exit 0' ERR

ENGINE="$HOOK_DIR/lib/tool-budget.py"
command -v python3 >/dev/null 2>&1 || { log_warn "tool-budget-guard: python3 unavailable"; exit 0; }
[ -f "$ENGINE" ] || { log_warn "tool-budget-guard: engine missing"; exit 0; }

case "$HOOK_DIR" in
  */.codex/*) runtime=codex ;;
  */.gemini/*) runtime=gemini ;;
  */.cursor/*) runtime=cursor ;;
  */.config/opencode/*) runtime=opencode ;;
  *) runtime=claude ;;
esac
# A caller that already names the runtime (the tests do) keeps its label.
runtime="${TOOL_BUDGET_RUNTIME:-$runtime}"

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || exit 0

# "tool" lets the engine read the event: PreToolUse decides and reserves,
# PostToolUse and PostToolUseFailure confirm (G-02). A post event never
# denies, so its exit is always 0 or an internal error.
rc=0
out="$(printf '%s' "$payload" | TOOL_BUDGET_RUNTIME="$runtime" TOOL_BUDGET_PPID="$PPID" python3 "$ENGINE" tool)" || rc=$?

case "$rc" in
  0)
    [ -z "$out" ] || printf '%s\n' "$out"
    exit 0
    ;;
  3)
    guard_log_event tool-budget-guard tool-call-budget deny "$out" "$payload"
    printf '%s\n' "$out" >&2
    exit 2
    ;;
  *)
    log_warn "tool-budget-guard: engine failed with status $rc, allowing"
    exit 0
    ;;
esac
