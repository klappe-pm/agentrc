#!/usr/bin/env bash
# Subagent-tool hook (Task|Agent) plus SubagentStart and SubagentStop: the
# per-parent live-subagent cap.
#
# WS1 P2.9. Unlike P1.6 (worktree-per-session, advisory-only: a hook cannot
# rewrite a Task call's inputs), a cap IS a real allow/deny decision at
# invocation time, so it is enforceable here. This closes the other half of
# the fan-out incident (97 subagents from 9 parent sessions): even with every
# subagent perfectly isolated in its own worktree, an unbounded fan-out from
# one parent is still a real resource and coordination hazard (host load,
# rate limits, an integration owner that cannot track that many lanes).
#
# Mechanism, since G-02 of the design record:
# one lock per subagent call in a directory keyed by the PARENT session_id,
# kept by lib/subagent-cap.py (its docstring is the full contract).
# PreToolUse counts the parent's locks and, at or past the cap, denies with a
# remediation message; below it, it reserves a pending lock under the call's
# tool_use_id that counts at once. The runtime's start signal (SubagentStart
# on Claude Code, the bridge's child session on OpenCode) confirms a pending
# lock and its finish signal (SubagentStop) releases it. A pending lock is
# also released when a guard deny record names its tool_use_id (a sibling
# hook denied the call, so it never runs), at PostToolUse or
# PostToolUseFailure for a call never seen started, and, on a runtime with a
# start signal, when still unconfirmed after SUBAGENT_CAP_CONFIRM_SECS (60).
# On a runtime without a start signal PostToolUse releases the call's lock,
# the path this guard used before G-02. SUBAGENT_CAP_TTL_SECS (3600) stays the
# last resort for any lock whose release was never seen.
#
# Tool-name assumption: registered with matcher "Task|Agent", because Claude
# Code sends tool_name "Agent" for a subagent dispatch and "Task" is the name
# this guard was registered under when it was written. The guard never reads
# tool_name, so either payload counts and denies the same way. The live check
# behind the matcher is recorded in
# the design record.
#
# Runtime: taken from where this file is installed, as tool-budget-guard.sh
# does, or from SUBAGENT_CAP_RUNTIME when a caller sets it (the tests do).
#
# Fail-open contract: any internal failure (state dir unwritable, python3
# missing, malformed payload, parent key unresolvable) allows the subagent to
# spawn. A bug in this guard must never lock out subagent spawning.

set -u
[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$HOOK_DIR/lib/log.sh"
# shellcheck source=lib/guard-log.sh
[ -f "$HOOK_DIR/lib/guard-log.sh" ] && source "$HOOK_DIR/lib/guard-log.sh"
command -v guard_log_event >/dev/null 2>&1 || guard_log_event() { :; }

# A dispatch that is allowed carries the fleet cap's advisory warning, when
# there is one, as a systemMessage (WI-101b). A deny carries only its reason.
fleet_note=""
allow() {
  if [ -n "$fleet_note" ]; then
    printf '{"systemMessage":%s}\n' "$(printf '%s' "$fleet_note" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))' 2>/dev/null || printf '""')"
  fi
  exit 0
}

trap 'log_warn "subagent-cap-guard: internal error at line ${LINENO}"; allow' ERR

ENGINE="$HOOK_DIR/lib/subagent-cap.py"
command -v python3 >/dev/null 2>&1 || { log_warn "subagent-cap-guard: python3 unavailable"; allow; }
[ -f "$ENGINE" ] || { log_warn "subagent-cap-guard: engine missing"; allow; }

case "$HOOK_DIR" in
  */.codex/*) runtime=codex ;;
  */.gemini/*) runtime=gemini ;;
  */.cursor/*) runtime=cursor ;;
  */.config/opencode/*) runtime=opencode ;;
  *) runtime=claude ;;
esac
runtime="${SUBAGENT_CAP_RUNTIME:-$runtime}"

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || allow

# WI-101b: the machine-wide fleet cap (components.json budgets.fleet), checked
# on a dispatch before the per-parent engine runs, since it reads no
# per-parent state and scans every recent transcript on the machine. The cap
# is advisory (the operator's decision of 2026-09-24): hooks/lib/fleet-cap.py
# exits 3 with a warning on stdout when the new agent would run on the fleet
# model at or past the cap, and the dispatch goes on to the per-parent check
# with that warning attached. Any other status is no warning. The start,
# finish and post events of a call are not dispatches and are not checked.
event="$(printf '%s' "$payload" | python3 -c 'import json,sys
try:
    print(json.loads(sys.stdin.read() or "{}").get("hook_event_name") or "")
except Exception:
    print("")' 2>/dev/null || true)"
FLEET_LIB="$HOOK_DIR/lib/fleet-cap.py"
case "$event" in
  PostToolUse|PostToolUseFailure|SubagentStart|SubagentStop) ;;
  *)
    if [ -f "$FLEET_LIB" ]; then
      fleet_rc=0
      fleet_warning="$(printf '%s' "$payload" | python3 "$FLEET_LIB" check 2>/dev/null)" || fleet_rc=$?
      if [ "$fleet_rc" -eq 3 ] && [ -n "$fleet_warning" ]; then
        guard_log_event subagent-cap-guard fleet-cap warn "$fleet_warning" "${payload:-}"
        fleet_note="$fleet_warning"
      fi
    fi
    ;;
esac

rc=0
out="$(printf '%s' "$payload" | SUBAGENT_CAP_RUNTIME="$runtime" SUBAGENT_CAP_PPID="$PPID" python3 "$ENGINE")" || rc=$?

case "$rc" in
  0) allow ;;
  3)
    guard_log_event subagent-cap-guard subagent-cap deny "$out" "$payload"
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
      "$(printf '%s' "$out" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"
    exit 0
    ;;
  *)
    log_warn "subagent-cap-guard: engine failed with status $rc, allowing"
    allow
    ;;
esac
