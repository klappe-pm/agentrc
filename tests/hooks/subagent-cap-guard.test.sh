#!/usr/bin/env bash
# Functional test for subagent-cap-guard.sh: per-parent live-subagent cap.
# Plants N live child locks and confirms the (N+1)th PreToolUse (Task or
# Agent) is denied, that a PostToolUse releases one lock and the cap re-opens,
# that TTL-stale locks are pruned even without a matching Post, and the
# fail-open contract on malformed input.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hook-test.sh"

HOOK="$DIR/subagent-cap-guard.sh"
PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1" >&2; [ -n "${2:-}" ] && printf '       %s\n' "$2" >&2; return 0; }

# WI-16/F-21: a real HOME override already keeps guard-log's default
# directory off the live telemetry tree, but every test that can fire a guard
# sets GUARD_LOG_DIR explicitly too, so isolation does not depend on HOME
# staying overridden. REAL_HOME and the live-file line count are captured
# before that override, so the before/after proof below reads the operator's
# actual telemetry, not the scratch one.
REAL_HOME="${HOME:-}"
LIVE_FILE="$REAL_HOME/.agent-hooks/telemetry/guard-events-$(date -u '+%Y-%m-%d').jsonl"
live_lines() {
  [ -n "$REAL_HOME" ] && [ -f "$LIVE_FILE" ] && grep -c '' "$LIVE_FILE" 2>/dev/null || printf '0'
}
LIVE_BEFORE="$(live_lines)"

scratch="$(mktemp -d "${TMPDIR:-/tmp}/scg-XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
export HOME="$scratch/home"; mkdir -p "$HOME"
export RUNTIME_HOOK_STATE_DIR="$scratch/state"
export GUARD_LOG_DIR="$scratch/telemetry"

PARENT="parent-sess-1"
parent_dir="$RUNTIME_HOOK_STATE_DIR/$PARENT"

mk_payload() {  # event session_id; TOOL_NAME overrides the tool name (default Task)
  python3 -c '
import json,sys
print(json.dumps({"hook_event_name":sys.argv[1],"session_id":sys.argv[2],
                  "tool_name":sys.argv[3],"tool_input":{"subagent_type":"reviewer"}}))
' "$1" "$2" "${TOOL_NAME:-Task}"
}
run_pre()  { printf '%s' "$(mk_payload PreToolUse "$PARENT")"  | SUBAGENT_CAP_PER_PARENT="${1:-16}" bash "$HOOK"; }
run_post() { printf '%s' "$(mk_payload PostToolUse "$PARENT")" | SUBAGENT_CAP_PER_PARENT="${1:-16}" bash "$HOOK"; }
is_deny() { printf '%s' "$1" | grep -q '"deny"'; }
# Every lock counts toward the cap: pending (admitted, not yet seen started)
# and child (confirmed started), G-02.
live_count() { local n=0 f; for f in "$parent_dir"/pending-*.lock "$parent_dir"/child-*.lock; do [ -e "$f" ] && n=$((n+1)); done; printf '%s' "$n"; }
clear_locks() { rm -f "$parent_dir"/pending-*.lock "$parent_dir"/child-*.lock; }

# Case 1: cap of 2. First two admits succeed and each plants one lock.
out="$(run_pre 2)"; is_deny "$out" && bad "admit 1 should not deny" || ok
out="$(run_pre 2)"; is_deny "$out" && bad "admit 2 should not deny" || ok
[ "$(live_count)" = "2" ] && ok || bad "two live locks planted"

# Case 2: the 3rd Pre call past the cap of 2 is denied.
out="$(run_pre 2)"
is_deny "$out" && ok || bad "3rd Pre call past cap of 2 must deny"
printf '%s' "$out" | grep -q "SUBAGENT_CAP_PER_PARENT" && ok || bad "cap-block message has remediation"
[ "$(live_count)" = "2" ] && ok || bad "denied Pre call must not plant a new lock"

# Case 3: a PostToolUse call releases one lock, dropping back below the cap.
run_post 2 >/dev/null
[ "$(live_count)" = "1" ] && ok || bad "Post releases exactly one lock"

# Case 4: cap re-opens after the release: the next Pre call is admitted.
out="$(run_pre 2)"
is_deny "$out" && bad "cap should re-open after a Post release" || ok
[ "$(live_count)" = "2" ] && ok || bad "re-admitted call plants a new lock"

# Case 5: TTL-based staleness prunes an old lock even without a matching Post
# (defense in depth against a Post that never fires, e.g. a crashed child).
clear_locks
mkdir -p "$parent_dir"
stale="$parent_dir/child-stale.lock"
printf 'since=%s\n' "$(( $(date +%s) - 999999 ))" > "$stale"
out="$(SUBAGENT_CAP_TTL_SECS=60 run_pre 1)"
is_deny "$out" && bad "stale lock past TTL must be pruned, not counted toward the cap" || ok

# Case 6: a corrupt/unreadable lock (no parseable since=) is dropped, not
# treated as permanently live.
clear_locks
printf 'garbage\n' > "$parent_dir/child-corrupt.lock"
out="$(run_pre 1)"
is_deny "$out" && bad "corrupt lock must not count toward the cap" || ok

# Case 7: malformed JSON payload fails open (allow), never denies.
out="$(printf 'not json' | bash "$HOOK" 2>/dev/null)" || true
is_deny "$out" && bad "malformed payload must fail open" || ok

# Case 8: a different parent session gets its own independent cap counter.
clear_locks
other_dir="$RUNTIME_HOOK_STATE_DIR/parent-sess-2"
run_pre_other() {
  python3 -c '
import json,sys
print(json.dumps({"hook_event_name":"PreToolUse","session_id":"parent-sess-2",
                  "tool_input":{"subagent_type":"reviewer"}}))
' | SUBAGENT_CAP_PER_PARENT=1 bash "$HOOK"
}
out="$(run_pre_other)"; is_deny "$out" && bad "a fresh parent must not inherit another parent's count" || ok
out2="$(run_pre_other)"
is_deny "$out2" && ok || bad "second call for parent-sess-2 alone should hit its own cap of 1"
[ -d "$other_dir" ] && ok || bad "second parent gets its own state directory"

# Disable-guard contract.
hook_test_disable_guard "$HOOK"; ok

# Case 9: a real deny event logs a guard-events record carrying the join keys
# and the capability, proving the deny() call site's ${payload:-} is set by
# the time it runs (payload is assigned once, as a plain variable in this
# script's own shell, well before deny is ever reached).
clear_locks
run_pre 1 >/dev/null
out="$(run_pre 1)"
is_deny "$out" && ok || bad "setup for the logging case must deny"
logged="$(find "$GUARD_LOG_DIR" -name 'guard-events-*.jsonl' 2>/dev/null | head -1)"
if [ -n "$logged" ]; then
  record="$(tail -1 "$logged")"
  case "$record" in *'"tool_name": "Task"'*) ok ;; *) bad "deny event carries tool_name" "$record" ;; esac
  case "$record" in *"\"session_id\": \"$PARENT\""*) ok ;; *) bad "deny event carries session_id" "$record" ;; esac
  case "$record" in *'"capability": "reviewer"'*) ok ;; *) bad "deny event carries capability" "$record" ;; esac
else
  bad "deny event was not logged to GUARD_LOG_DIR" "no file under $GUARD_LOG_DIR"
fi

# Case 10 (WI-94, G-01): Claude Code sends tool_name "Agent" for a subagent
# dispatch (observed on 2.1.273 and 2.1.280); "Task" is the name this guard
# was registered under when it was written. The guard never reads tool_name,
# so an Agent payload is counted, denied and released exactly like a Task
# payload, and the deny record carries the name the runtime sent.
clear_locks
out="$(TOOL_NAME=Agent run_pre 1)"
is_deny "$out" && bad "first Agent call under cap 1 must be admitted" || ok
[ "$(live_count)" = "1" ] && ok || bad "an admitted Agent call plants one lock"
out="$(TOOL_NAME=Agent run_pre 1)"
is_deny "$out" && ok || bad "second Agent call at cap 1 must deny"
[ "$(live_count)" = "1" ] && ok || bad "a denied Agent call plants no lock"
# The newest dated log file, in case UTC midnight fell between case 9 and here.
logged="$(find "$GUARD_LOG_DIR" -name 'guard-events-*.jsonl' 2>/dev/null | sort | tail -1)"
if [ -n "$logged" ]; then
  record="$(tail -1 "$logged")"
  case "$record" in *'"tool_name": "Agent"'*) ok ;; *) bad "deny event carries the Agent tool name" "$record" ;; esac
else
  bad "Agent deny event was not logged to GUARD_LOG_DIR" "no file under $GUARD_LOG_DIR"
fi
TOOL_NAME=Agent run_post 1 >/dev/null
[ "$(live_count)" = "0" ] && ok || bad "an Agent PostToolUse releases the lock"

# Case 11 (WI-94, G-01): the registration itself. hooks.json must register
# this guard once for PreToolUse and once for PostToolUse with a matcher that
# names "Agent" as well as "Task"; a matcher naming only "Task" leaves the cap
# depending on an alias Claude Code has not documented. A matcher is a regular
# expression, and "*" or an omitted matcher means every tool, so each one is
# checked by full match against both names with those two forms passing. Any
# failure inside the check (unreadable file, bad regex) is reported as a FAIL,
# never allowed to abort the script under set -e.
HOOKS_JSON="$DIR/hooks.json"
registration="$(python3 -c '
import json, re, sys
try:
    groups = json.load(open(sys.argv[1]))
    bad = []
    # G-02: PostToolUseFailure releases an unconfirmed lock like PostToolUse;
    # SubagentStart confirms and SubagentStop finishes, for every agent type.
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop"):
        hits = [g.get("matcher", "") for g in groups.get(event, []) for h in g.get("hooks", []) if h.get("command", "").endswith("/subagent-cap-guard.sh")]
        if len(hits) != 1:
            bad.append(f"{event}: registered {len(hits)} times")
            continue
        matcher = hits[0]
        if matcher in ("", "*"):
            continue
        if event.startswith("Subagent"):
            bad.append(f"{event}: matcher {matcher!r} narrows the agent types")
            continue
        for name in ("Task", "Agent"):
            if not re.fullmatch(matcher, name):
                bad.append(f"{event}: matcher {matcher!r} does not match {name}")
    print("ok" if not bad else "bad " + "; ".join(bad))
except Exception as error:
    print(f"bad {type(error).__name__}: {error}")
' "$HOOKS_JSON" 2>&1)" || true
[ "$registration" = "ok" ] && ok || bad "hooks.json registers the guard for Task and Agent on both events" "$registration"

# G-02: the lock is
# a reservation made at PreToolUse, keyed by tool_use_id, confirmed by the
# runtime's start signal and finished by its finish signal.
# ev <event> <tool_use_id> <agent_id> <agent_type>: a Claude Code payload for
# the parent session; an empty value is left out.
ev() {
  python3 -c '
import json, sys
event, use_id, agent_id, agent_type = sys.argv[1:5]
data = {"hook_event_name": event, "session_id": sys.argv[5], "tool_name": "Agent",
        "tool_input": {"subagent_type": "reviewer", "prompt": "p"}}
if event.startswith("Subagent"):
    data.pop("tool_name"); data.pop("tool_input")
for key, value in (("tool_use_id", use_id), ("agent_id", agent_id), ("agent_type", agent_type)):
    if value:
        data[key] = value
print(json.dumps(data))
' "$1" "${2:-}" "${3:-}" "${4:-}" "$PARENT"
}
send() { printf '%s' "$1" | SUBAGENT_CAP_PER_PARENT="${CAP_N:-16}" bash "$HOOK"; }
has_lock() { [ -e "$parent_dir/$1" ]; }

# Case 12 (the record's acceptance line): an Agent call this guard admitted is
# denied by a sibling hook, here the tool-call budget. No PostToolUse follows;
# the budget's deny record names the tool_use_id, and this guard's next run
# releases the reservation, so the denied call leaves no subagent lock.
clear_locks
agent_call="$(ev PreToolUse toolu_A)"
send "$agent_call" >/dev/null
has_lock pending-toolu_A.lock && ok || bad "an admitted Agent call reserves a pending lock under its tool_use_id"
printf '{"effective": {"tool_calls.total": 0}}' >"$scratch/zero-budget.json"
budget_rc=0
printf '%s' "$agent_call" | LLM_ROOT_BUDGET_FILE="$scratch/zero-budget.json" TOOL_BUDGET_STATE_DIR="$scratch/budget-state" \
  bash "$DIR/tool-budget-guard.sh" >/dev/null 2>&1 || budget_rc=$?
[ "$budget_rc" = "2" ] && ok || bad "setup: the budget denies the Agent call" "rc=$budget_rc"
send "$(ev PreToolUse toolu_B)" >/dev/null
has_lock pending-toolu_A.lock && bad "the denied Agent call left its subagent lock" || ok
[ "$(live_count)" = "1" ] && ok || bad "only the next call's lock is held" "$(live_count)"

# Case 13: a pending lock counts at once, so two calls issued together at a
# cap of 1 let exactly one through, before either has started.
clear_locks
CAP_N=1
first_out="$(send "$(ev PreToolUse toolu_C)")"
second_out="$(send "$(ev PreToolUse toolu_D)")"
is_deny "$first_out" && bad "cap 1: the first call is admitted" || ok
is_deny "$second_out" && ok || bad "cap 1: the second call, issued before the first started, is denied"
CAP_N=16

# Case 14: SubagentStart confirms the pending lock; a PostToolUse for that
# call (a background subagent returns at once) leaves it held; SubagentStop
# for its agent_id releases it.
clear_locks
send "$(ev PreToolUse toolu_E)" >/dev/null
send "$(ev SubagentStart "" agent_e reviewer)" >/dev/null
has_lock child-agent_e.lock && ok || bad "SubagentStart confirms the pending lock as the started agent"
has_lock pending-toolu_E.lock && bad "the confirmed lock is no longer pending" || ok
send "$(ev PostToolUse toolu_E)" >/dev/null
has_lock child-agent_e.lock && ok || bad "PostToolUse does not end a subagent that was seen started"
send "$(ev SubagentStop "" agent_e reviewer)" >/dev/null
[ "$(live_count)" = "0" ] && ok || bad "SubagentStop releases the confirmed lock" "$(live_count)"

# Case 15: PostToolUseFailure for a call never seen started releases it.
send "$(ev PreToolUse toolu_F)" >/dev/null
send "$(ev PostToolUseFailure toolu_F)" >/dev/null
[ "$(live_count)" = "0" ] && ok || bad "PostToolUseFailure releases an unconfirmed lock" "$(live_count)"

# Case 15b: a failed call already confirmed as a child must not release the
# other call's pending reservation. Both children can still start at cap 2.
clear_locks
CAP_N=2
send "$(ev PreToolUse toolu_A1)" >/dev/null
send "$(ev SubagentStart "" agent_a1 reviewer)" >/dev/null
send "$(ev PreToolUse toolu_B1)" >/dev/null
send "$(ev PostToolUseFailure toolu_A1)" >/dev/null
has_lock pending-toolu_B1.lock && ok || bad "failure for a confirmed child preserves the other call's reservation"
[ "$(live_count)" = "2" ] && ok || bad "the confirmed child and unrelated pending call both count" "$(live_count)"
third_out="$(send "$(ev PreToolUse toolu_C1)")"
is_deny "$third_out" && ok || bad "a third call cannot enter while another call remains pending"
send "$(ev SubagentStart "" agent_b1 reviewer)" >/dev/null
[ "$(live_count)" = "2" ] && ok || bad "both started children remain counted at the cap" "$(live_count)"
CAP_N=16

# Case 15a: starts carry no tool_use_id. If the second call starts first,
# it can consume the first call's reservation. The failure cannot prove which
# child started, so retain the pending slot until its own signal or timeout.
clear_locks
CAP_N=2
send "$(ev PreToolUse toolu_F1)" >/dev/null
send "$(ev PreToolUse toolu_F2)" >/dev/null
send "$(ev SubagentStart "" agent_f2 reviewer)" >/dev/null
has_lock child-agent_f2.lock && ok || bad "the out-of-order start confirms a child"
send "$(ev PostToolUseFailure toolu_F1)" >/dev/null
[ "$(live_count)" = "2" ] && ok || bad "the ambiguous failure keeps the remaining pending slot" "$(live_count)"
has_lock child-agent_f2.lock && ok || bad "the out-of-order child remains live"
third_out="$(send "$(ev PreToolUse toolu_F3)")"
is_deny "$third_out" && ok || bad "the ambiguous failure cannot admit another call"
has_lock pending-toolu_F2.lock && ok || bad "the remaining call keeps its reservation"
send "$(ev SubagentStop "" agent_f2 reviewer)" >/dev/null
CAP_N=16

# Case 16: an unconfirmed reservation is released after the confirm window on
# a runtime with a start signal, and kept on one without, where PostToolUse
# stays the release path.
age_pending() {
  local f
  for f in "$parent_dir"/pending-*.lock; do
    [ -e "$f" ] || continue
    python3 -c '
import sys, time
path = sys.argv[1]
lines = [l for l in open(path).read().splitlines() if not l.startswith("since=")]
open(path, "w").write("since=%d\n" % (time.time() - 120) + "".join(l + "\n" for l in lines))
' "$f"
  done
}
clear_locks
send "$(ev PreToolUse toolu_G)" >/dev/null
age_pending
send "$(ev PreToolUse toolu_H)" >/dev/null
has_lock pending-toolu_G.lock && bad "claude: an unconfirmed lock past the window is released" || ok
clear_locks
printf '%s' "$(ev PreToolUse toolu_I)" | SUBAGENT_CAP_RUNTIME=codex bash "$HOOK" >/dev/null
age_pending
printf '%s' "$(ev PreToolUse toolu_J)" | SUBAGENT_CAP_RUNTIME=codex bash "$HOOK" >/dev/null
has_lock pending-toolu_I.lock && ok || bad "codex: with no start signal the window does not release a lock"
printf '%s' "$(ev PostToolUse toolu_I)" | SUBAGENT_CAP_RUNTIME=codex bash "$HOOK" >/dev/null
has_lock pending-toolu_I.lock && bad "codex: PostToolUse releases the call's lock" || ok

# Case 17: OpenCode reads no hooks.json; its bridge sends the start signal on
# a child session's session.created and the finish signal when that child
# goes idle or is deleted, and passes each task call's callID.
BRIDGE="$DIR/opencode-runtime-hooks.ts"
grep -qF 'subagentPayload("SubagentStart", info.parentID, info.id ?? "", cwd)' "$BRIDGE" \
  && ok || bad "OpenCode bridge confirms a reservation on a child session's creation" "$BRIDGE"
grep -qF 'event.type === "session.idle" || event.type === "session.deleted"' "$BRIDGE" \
  && ok || bad "OpenCode bridge finishes a subagent when its child session goes idle or is deleted" "$BRIDGE"
grep -qF 'subagentPayload("SubagentStop", parentID, childID, cwd)' "$BRIDGE" \
  && ok || bad "OpenCode bridge sends SubagentStop for the child" "$BRIDGE"
grep -qF 'payload("PreToolUse", id, cwd, "Task", output.args, call)' "$BRIDGE" \
  && ok || bad "OpenCode bridge passes the task call's callID to the cap" "$BRIDGE"
# The bridge's SubagentStart payload drives this guard end to end.
clear_locks
printf '%s' "$(ev PreToolUse call_oc)" | SUBAGENT_CAP_RUNTIME=opencode bash "$HOOK" >/dev/null
printf '{"hook_event_name":"SubagentStart","session_id":"%s","agent_id":"ses_child","cwd":"/tmp"}' "$PARENT" | SUBAGENT_CAP_RUNTIME=opencode bash "$HOOK" >/dev/null
has_lock child-ses_child.lock && ok || bad "opencode: the child session confirms the reservation"
printf '{"hook_event_name":"SubagentStop","session_id":"%s","agent_id":"ses_child","cwd":"/tmp"}' "$PARENT" | SUBAGENT_CAP_RUNTIME=opencode bash "$HOOK" >/dev/null
[ "$(live_count)" = "0" ] && ok || bad "opencode: the child going idle releases it" "$(live_count)"

# The live telemetry directory gained no record across this whole run: every
# guard invocation above ran under GUARD_LOG_DIR, never the real default.
LIVE_AFTER="$(live_lines)"
[ "$LIVE_AFTER" = "$LIVE_BEFORE" ] && ok || bad "live telemetry directory gained a record during this test run" "before=$LIVE_BEFORE after=$LIVE_AFTER"

if [ "$FAIL" -gt 0 ]; then
  printf 'subagent-cap-guard.test: FAIL (%d passed, %d failed)\n' "$PASS" "$FAIL" >&2
  exit 1
fi
printf 'subagent-cap-guard.test: passed (%d cases)\n' "$PASS"
