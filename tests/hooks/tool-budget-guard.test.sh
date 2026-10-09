#!/usr/bin/env bash
# Functional test for tool-budget-guard.sh: the live tool-call count and the
# budget deny behind it.
# Counts with and without a budget, denies past a total and a per-tool limit
# with exit 2, warns once at warn_at, keeps sessions apart, writes the
# work-item join line once, handles the Claude Code, Codex and Gemini CLI
# payload shapes, and fails open on every internal error.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hook-test.sh"

HOOK="$DIR/tool-budget-guard.sh"
PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*" >&2; }

scratch="$(mktemp -d "${TMPDIR:-/tmp}/tool-budget-guard-XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
export HOME="$scratch/home"; mkdir -p "$HOME"
export TOOL_BUDGET_STATE_DIR="$scratch/state"
export LLM_ROOT_TELEMETRY_DIR="$scratch/telemetry"
export GUARD_LOG_DIR="$scratch/guard-log"
# A test run inside a launched session must not inherit that session's budget.
unset LLM_ROOT_WORK_ITEM LLM_ROOT_LAUNCH_ID LLM_ROOT_SESSION_DIR LLM_ROOT_BUDGET_FILE RUNTIME_HOOKS_DISABLE
JOIN="$LLM_ROOT_TELEMETRY_DIR/work-item-sessions.jsonl"

BUDGET_FILE=""
WORK_ITEM=""
LAUNCH_ID=""

# payload <runtime> <session_id> <tool>: a pre-tool payload in the shape each
# runtime documents. An empty session_id is left out.
payload() {
  python3 -c '
import json, sys
runtime, session, tool = sys.argv[1:4]
if runtime == "gemini":
    data = {"hook_event_name": "BeforeTool", "timestamp": "2026-09-23T00:00:00Z",
            "transcript_path": "/tmp/t.json", "cwd": "/tmp", "tool_name": tool,
            "tool_input": {"command": "ls"}}
elif runtime == "codex":
    data = {"hook_event_name": "PreToolUse", "turn_id": "turn-1", "model": "fixture-model",
            "transcript_path": "/tmp/t.jsonl", "cwd": "/tmp", "tool_name": tool,
            "tool_use_id": "call-1", "tool_input": {"command": "ls"}}
else:
    data = {"hook_event_name": "PreToolUse", "transcript_path": "/tmp/t.jsonl",
            "cwd": "/tmp", "permission_mode": "default", "tool_name": tool,
            "tool_input": {"command": "ls"}}
if session:
    data["session_id"] = session
print(json.dumps(data))
' "$1" "$2" "$3"
}

# run <payload>: run the hook under the current BUDGET_FILE, WORK_ITEM and
# LAUNCH_ID, leaving stdout, stderr and the exit status in scratch files. An
# empty value is passed as empty, which the hook treats as unset.
run() {
  printf '%s' "$1" >"$scratch/payload"
  rc=0
  LLM_ROOT_BUDGET_FILE="$BUDGET_FILE" LLM_ROOT_WORK_ITEM="$WORK_ITEM" LLM_ROOT_LAUNCH_ID="$LAUNCH_ID" \
    TOOL_BUDGET_RUNTIME="${RT:-}" bash "$HOOK" <"$scratch/payload" >"$scratch/out" 2>"$scratch/err" || rc=$?
  printf '%s' "$rc" >"$scratch/rc"
}
call() { run "$(payload "$1" "$2" "$3")"; }
rc_is() { [ "$(cat "$scratch/rc")" = "$1" ]; }
out() { cat "$scratch/out"; }
err() { cat "$scratch/err"; }
system_message() { out | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["systemMessage"])' 2>/dev/null || true; }

# state <session_id> <field>: one field of a session's state file, with
# per_tool.<name> reaching into the per-tool map. Prints "missing" when absent.
state() {
  python3 -c '
import json, os, re, sys
path = os.path.join(os.environ["TOOL_BUDGET_STATE_DIR"], re.sub(r"[^A-Za-z0-9._-]", "_", sys.argv[1]) + ".json")
try:
    data = json.load(open(path))
except Exception:
    print("missing"); sys.exit(0)
field = sys.argv[2]
if field.startswith("per_tool."):
    print(data.get("per_tool", {}).get(field[len("per_tool."):], "missing"))
elif field.startswith("per_agent."):
    print(data.get("per_agent", {}).get(field[len("per_agent."):], "missing"))
elif field == "warned":
    print(",".join(data.get("warned", [])))
elif field == "reserved":
    print(",".join(sorted(data.get("reserved", {}))))
else:
    print(data.get(field, "missing"))
' "$1" "$2"
}

# budget <name> <effective json>: write a launcher-shaped budget file.
budget() {
  python3 -c '
import json, sys
json.dump({"work_item": "WI-30", "launch_id": "launch-1", "runtime": "claude",
           "effective": json.loads(sys.argv[2]), "levels": [], "errors": []},
          open(sys.argv[1], "w"))
' "$scratch/$1.json" "$2"
  printf '%s' "$scratch/$1.json"
}

# join_lines <session_id>: how many join lines name that session.
join_lines() {
  [ -f "$JOIN" ] || { printf '0'; return 0; }
  python3 -c '
import json, sys
n = 0
for line in open(sys.argv[1]):
    try:
        if json.loads(line).get("session_id") == sys.argv[2]:
            n += 1
    except ValueError:
        pass
print(n)
' "$JOIN" "$1"
}

[ -x "$HOOK" ] && ok || bad "hook is executable"

# Case 1: no budget file. Every call is counted, per tool and in total, and
# every call is allowed silently.
for tool in Bash Bash Read Bash Read; do
  call claude s1 "$tool"
  rc_is 0 && ok || bad "no budget: $tool allowed" "rc=$(cat "$scratch/rc")"
  [ -z "$(err)" ] && ok || bad "no budget: $tool silent on stderr" "$(err)"
  [ -z "$(out)" ] && ok || bad "no budget: $tool silent on stdout" "$(out)"
done
[ "$(state s1 total)" = "5" ] && ok || bad "no budget: total counted" "$(state s1 total)"
[ "$(state s1 per_tool.Bash)" = "3" ] && ok || bad "no budget: Bash counted" "$(state s1 per_tool.Bash)"
[ "$(state s1 per_tool.Read)" = "2" ] && ok || bad "no budget: Read counted" "$(state s1 per_tool.Read)"
[ "$(state s1 denied)" = "0" ] && ok || bad "no budget: nothing denied" "$(state s1 denied)"

# Case 2: a total limit of 3 allows three calls and denies the fourth with
# exit 2, naming the budget, the count, the limit and the work item. The
# denied call is not counted in total; it is counted as denied.
BUDGET_FILE="$(budget total3 '{"tool_calls.total": 3, "warn_at": 1.0}')"
WORK_ITEM="WI-30"
for n in 1 2 3; do
  call claude s2 Read
  rc_is 0 && ok || bad "total limit: call $n allowed" "rc=$(cat "$scratch/rc") $(err)"
done
call claude s2 Read
rc_is 2 && ok || bad "total limit: call 4 denied with exit 2" "rc=$(cat "$scratch/rc")"
case "$(err)" in *tool_calls.total*) ok ;; *) bad "deny names tool_calls.total" "$(err)" ;; esac
case "$(err)" in *WI-30*) ok ;; *) bad "deny names the work item" "$(err)" ;; esac
case "$(err)" in *4*3*) ok ;; *) bad "deny names the count and the limit" "$(err)" ;; esac
case "$(err)" in *loadouts/WI-30.json*) ok ;; *) bad "deny names where to raise the limit" "$(err)" ;; esac
[ "$(state s2 total)" = "3" ] && ok || bad "denied call not counted in total" "$(state s2 total)"
[ "$(state s2 denied)" = "1" ] && ok || bad "denied call counted as denied" "$(state s2 denied)"
call claude s2 Read
rc_is 2 && ok || bad "total limit: call 5 still denied"
[ "$(state s2 denied)" = "2" ] && ok || bad "second denial counted" "$(state s2 denied)"

# Case 3: a per-tool limit denies only that tool.
BUDGET_FILE="$(budget perbash '{"tool_calls.total": 100, "tool_calls.per_tool.Bash": 2, "warn_at": 1.0}')"
call claude s3 Bash; rc_is 0 && ok || bad "per tool: Bash 1 allowed"
call claude s3 Bash; rc_is 0 && ok || bad "per tool: Bash 2 allowed"
call claude s3 Bash
rc_is 2 && ok || bad "per tool: Bash 3 denied" "rc=$(cat "$scratch/rc")"
case "$(err)" in *tool_calls.per_tool.Bash*) ok ;; *) bad "per-tool deny names tool_calls.per_tool.Bash" "$(err)" ;; esac
call claude s3 Read; rc_is 0 && ok || bad "per tool: Read still allowed"
[ "$(state s3 per_tool.Bash)" = "2" ] && ok || bad "per tool: denied Bash not counted" "$(state s3 per_tool.Bash)"
[ "$(state s3 total)" = "3" ] && ok || bad "per tool: total counts the allowed calls" "$(state s3 total)"

# Case 4: the first call to reach warn_at of a limit is allowed with a
# systemMessage on stdout, once per key per session.
BUDGET_FILE="$(budget warn '{"tool_calls.total": 10, "warn_at": 0.5}')"
for n in 1 2 3 4; do
  call claude s4 Grep
  [ -z "$(out)" ] && ok || bad "warn: call $n below warn_at is silent" "$(out)"
done
call claude s4 Grep
rc_is 0 && ok || bad "warn: the warning call is allowed"
message="$(system_message)"
case "$message" in *tool_calls.total*) ok ;; *) bad "warn: systemMessage names the budget" "$(out)" ;; esac
case "$message" in *"5 left"*) ok ;; *) bad "warn: systemMessage says how much is left" "$(out)" ;; esac
[ "$(state s4 warned)" = "tool_calls.total" ] && ok || bad "warn: key recorded" "$(state s4 warned)"
call claude s4 Grep
[ -z "$(out)" ] && ok || bad "warn: not repeated on the next call" "$(out)"

# Case 4b: warn_at defaults to 0.8 when the budget does not set it.
BUDGET_FILE="$(budget warndefault '{"tool_calls.total": 5}')"
for n in 1 2 3; do call claude s4b Grep; done
[ -z "$(out)" ] && ok || bad "default warn: call 3 of 5 is silent" "$(out)"
call claude s4b Grep
case "$(out)" in *systemMessage*) ok ;; *) bad "default warn: call 4 of 5 warns" "$(out)" ;; esac

# Case 5: each runtime's fixture payload is counted and denied at the limit.
BUDGET_FILE="$(budget total2 '{"tool_calls.total": 2, "warn_at": 1.0}')"
for pair in claude:Bash codex:Bash gemini:run_shell_command; do
  runtime="${pair%%:*}"
  tool="${pair#*:}"
  # Each runtime is counted under its own label, as its installed wrapper
  # would name it: only Claude Code confirms at PostToolUse (G-02), so these
  # PreToolUse-only fixtures count at PreToolUse on the other runtimes.
  RT="$runtime"
  call "$runtime" "rt-$runtime" "$tool"; rc_is 0 && ok || bad "$runtime: call 1 allowed" "$(err)"
  call "$runtime" "rt-$runtime" "$tool"; rc_is 0 && ok || bad "$runtime: call 2 allowed" "$(err)"
  call "$runtime" "rt-$runtime" "$tool"
  rc_is 2 && ok || bad "$runtime: call 3 denied with exit 2" "rc=$(cat "$scratch/rc")"
  RT=""
  [ "$(state "rt-$runtime" total)" = "2" ] && ok || bad "$runtime: counted" "$(state "rt-$runtime" total)"
  [ "$(state "rt-$runtime" "per_tool.$tool")" = "2" ] && ok || bad "$runtime: counted per tool"
done

# Case 6: two sessions keep separate counts.
BUDGET_FILE=""
WORK_ITEM=""
call claude s6a Bash; call claude s6a Bash; call claude s6b Bash
[ "$(state s6a total)" = "2" ] && ok || bad "session a counted alone" "$(state s6a total)"
[ "$(state s6b total)" = "1" ] && ok || bad "session b counted alone" "$(state s6b total)"

# Case 7: the work-item join line is written once per session when
# LLM_ROOT_WORK_ITEM is set, never when unset, and not again when the
# SessionStart hook already joined that session.
WORK_ITEM="WI-30"
LAUNCH_ID="launch-7"
call claude j1 Bash; call claude j1 Read; call claude j1 Bash
[ "$(join_lines j1)" = "1" ] && ok || bad "join written once" "$(join_lines j1)"
record="$(grep '"j1"' "$JOIN" 2>/dev/null || true)"
case "$record" in *'"source": "tool-budget"'*) ok ;; *) bad "join source is tool-budget" "$record" ;; esac
case "$record" in *'"work_item": "WI-30"'*) ok ;; *) bad "join names the work item" "$record" ;; esac
case "$record" in *'"launch_id": "launch-7"'*) ok ;; *) bad "join names the launch" "$record" ;; esac
case "$record" in *'"runtime": "claude"'*) ok ;; *) bad "join names the runtime" "$record" ;; esac
case "$record" in *'"started": "'*Z'"'*) ok ;; *) bad "join carries a UTC start time" "$record" ;; esac
mkdir -p "$LLM_ROOT_TELEMETRY_DIR"
printf '%s\n' '{"work_item": "WI-30", "session_id": "j3", "runtime": "claude", "launch_id": "launch-7", "started": "2026-09-23T00:00:00Z", "source": "session-start"}' >>"$JOIN"
call claude j3 Bash
[ "$(join_lines j3)" = "1" ] && ok || bad "join not repeated after session-start" "$(join_lines j3)"
WORK_ITEM=""
LAUNCH_ID=""
call claude j2 Bash
[ "$(join_lines j2)" = "0" ] && ok || bad "no join without a work item" "$(join_lines j2)"

# Case 8: a denial is logged to the guard event log with the join keys.
BUDGET_FILE="$(budget zero '{"tool_calls.total": 0}')"
call claude logged Bash
rc_is 2 && ok || bad "a zero limit denies the first call"
logged="$(find "$GUARD_LOG_DIR" -name 'guard-events-*.jsonl' 2>/dev/null | head -1 || true)"
if [ -n "$logged" ]; then
  record="$(tail -1 "$logged")"
  case "$record" in *'"guard": "tool-budget-guard"'*) ok ;; *) bad "deny event names the guard" "$record" ;; esac
  case "$record" in *'"session_id": "logged"'*) ok ;; *) bad "deny event carries session_id" "$record" ;; esac
  case "$record" in *'"tool_name": "Bash"'*) ok ;; *) bad "deny event carries tool_name" "$record" ;; esac
else
  bad "deny event was not logged to GUARD_LOG_DIR"
fi

# Case 9: fail open. A malformed payload, an unreadable or malformed budget
# file, and an unwritable state directory all allow the call.
BUDGET_FILE="$(budget zero2 '{"tool_calls.total": 0}')"
for bad_payload in 'not json' '[1, 2]' ''; do
  run "$bad_payload"
  rc_is 0 && ok || bad "malformed payload '$bad_payload' allowed" "rc=$(cat "$scratch/rc")"
done
BUDGET_FILE="$scratch/no-such-budget.json"
call claude f1 Bash
rc_is 0 && ok || bad "missing budget file allowed" "rc=$(cat "$scratch/rc")"
[ "$(state f1 total)" = "1" ] && ok || bad "missing budget file still counts" "$(state f1 total)"
printf 'not json' >"$scratch/garbage.json"
BUDGET_FILE="$scratch/garbage.json"
call claude f2 Bash
rc_is 0 && ok || bad "malformed budget file allowed" "rc=$(cat "$scratch/rc")"
BUDGET_FILE="$(budget badlimit '{"tool_calls.total": "ten"}')"
call claude f3 Bash
rc_is 0 && ok || bad "a non-numeric limit is ignored" "rc=$(cat "$scratch/rc")"
BUDGET_FILE="$(budget zero3 '{"tool_calls.total": 0}')"
TOOL_BUDGET_STATE_DIR="$scratch/garbage.json/state" call claude f4 Bash
rc_is 0 && ok || bad "unwritable state directory allowed" "rc=$(cat "$scratch/rc")"

# Case 10: RUNTIME_HOOKS_DISABLE=1 wins over a budget that would deny.
printf '%s' "$(payload claude f5 Bash)" >"$scratch/payload"
rc=0
RUNTIME_HOOKS_DISABLE=1 LLM_ROOT_BUDGET_FILE="$BUDGET_FILE" bash "$HOOK" <"$scratch/payload" >/dev/null 2>&1 || rc=$?
[ "$rc" = "0" ] && ok || bad "RUNTIME_HOOKS_DISABLE allows" "rc=$rc"
[ "$(state f5 total)" = "missing" ] && ok || bad "RUNTIME_HOOKS_DISABLE does not count" "$(state f5 total)"
hook_test_disable_guard "$HOOK" && ok || bad "disable guard contract"

# Case 11: OpenCode reads no hooks.json, so its bridge must call this hook for
# every tool itself, and only when the hook is deployed: a missing script
# would exit 127 and the bridge would block every OpenCode tool call.
BRIDGE="$(cd "$(dirname "$HOOK")" && pwd)/opencode-runtime-hooks.ts"
grep -qF 'invokeIfPresent("tool-budget-guard.sh", payload("PreToolUse", id, cwd, input.tool, output.args, call), cwd)' "$BRIDGE" \
  && ok || bad "OpenCode bridge calls the budget hook for every tool, with its callID" "$BRIDGE"
# G-02: the bridge carries OpenCode's callID as tool_use_id, so a deny record
# names the call it denied.
grep -qF 'tool_input: toolInput, tool_use_id: callID' "$BRIDGE" \
  && ok || bad "OpenCode bridge payload carries tool_use_id" "$BRIDGE"
grep -qF 'await Bun.file(`${hookRoot}/${script}`).exists()' "$BRIDGE" \
  && ok || bad "OpenCode bridge skips a hook that is not deployed" "$BRIDGE"

# Case 12 (B-01): started is written on a session's first counted call and
# never changed after, so the baseline clock can start at the oldest one.
BUDGET_FILE=""
WORK_ITEM=""
LAUNCH_ID=""
call claude st1 Bash
first_started="$(state st1 started)"
case "$first_started" in 20*Z) ok ;; *) bad "started written on the first call" "$first_started" ;; esac
python3 -c '
import json, os, sys
path = os.path.join(os.environ["TOOL_BUDGET_STATE_DIR"], "st1.json")
data = json.load(open(path))
data["started"] = "2026-01-01T00:00:00Z"
json.dump(data, open(path, "w"))
'
call claude st1 Read
[ "$(state st1 started)" = "2026-01-01T00:00:00Z" ] && ok || bad "started kept across later calls" "$(state st1 started)"
[ "$(state st1 total)" = "2" ] && ok || bad "started case still counts" "$(state st1 total)"
# A state written before started existed takes its last update, the earliest
# time the hook is known to have counted it, rather than the time of this call.
python3 -c '
import json, os
path = os.path.join(os.environ["TOOL_BUDGET_STATE_DIR"], "st2.json")
json.dump({"session_id": "st2", "total": 4, "per_tool": {"Bash": 4}, "updated": "2026-02-02T00:00:00Z"}, open(path, "w"))
'
call claude st2 Bash
[ "$(state st2 started)" = "2026-02-02T00:00:00Z" ] && ok || bad "legacy state starts at its last update" "$(state st2 started)"

# Case 13 (B-05): per_agent counts calls under the payload's agent_id, and
# under main when the payload carries none. A denied call is not counted.
agent_payload() {
  python3 -c '
import json, sys
print(json.dumps({"hook_event_name": "PreToolUse", "session_id": sys.argv[1], "agent_id": sys.argv[2],
                  "agent_type": "general-purpose", "tool_name": sys.argv[3], "tool_input": {}}))
' "$1" "$2" "$3"
}
call claude pa1 Bash
run "$(agent_payload pa1 agent-x Read)"
run "$(agent_payload pa1 agent-x Bash)"
run "$(agent_payload pa1 agent-y Grep)"
[ "$(state pa1 per_agent.main)" = "1" ] && ok || bad "per_agent counts main" "$(state pa1 per_agent.main)"
[ "$(state pa1 per_agent.agent-x)" = "2" ] && ok || bad "per_agent counts agent-x" "$(state pa1 per_agent.agent-x)"
[ "$(state pa1 per_agent.agent-y)" = "1" ] && ok || bad "per_agent counts agent-y" "$(state pa1 per_agent.agent-y)"
[ "$(state pa1 total)" = "4" ] && ok || bad "agent calls count toward the session total" "$(state pa1 total)"
BUDGET_FILE="$(budget pa2 '{"tool_calls.total": 1, "warn_at": 1.0}')"
run "$(agent_payload pa2 agent-z Bash)"
run "$(agent_payload pa2 agent-z Bash)"
rc_is 2 && ok || bad "agent call past the session limit is denied" "rc=$(cat "$scratch/rc")"
[ "$(state pa2 per_agent.agent-z)" = "1" ] && ok || bad "denied agent call not counted per agent" "$(state pa2 per_agent.agent-z)"
BUDGET_FILE=""
# Codex review on PR 46: a state counted before per_agent existed keeps its
# earlier calls in an unattributed bucket, so the breakdown still sums to total.
python3 -c '
import json, os
path = os.path.join(os.environ["TOOL_BUDGET_STATE_DIR"], "pa3.json")
json.dump({"session_id": "pa3", "total": 100, "per_tool": {"Read": 100}, "updated": "2026-02-02T00:00:00Z"}, open(path, "w"))
'
run "$(agent_payload pa3 agent-x Read)"
[ "$(state pa3 per_agent.unattributed)" = "100" ] && ok || bad "legacy calls kept as unattributed" "$(state pa3 per_agent.unattributed)"
[ "$(state pa3 per_agent.agent-x)" = "1" ] && ok || bad "legacy state still counts the new agent call" "$(state pa3 per_agent.agent-x)"
run "$(agent_payload pa3 agent-x Read)"
[ "$(state pa3 per_agent.unattributed)" = "100" ] && ok || bad "unattributed is seeded once" "$(state pa3 per_agent.unattributed)"

# launch_state <launch_id> <field>: one field of a launch's shared count under
# launches/, with sessions joined by commas. Prints "missing" when absent.
launch_state() {
  python3 -c '
import json, os, sys
path = os.path.join(os.environ["TOOL_BUDGET_STATE_DIR"], "launches", sys.argv[1] + ".json")
try:
    data = json.load(open(path))
except Exception:
    print("missing"); sys.exit(0)
value = data.get(sys.argv[2], "missing")
if isinstance(value, dict):
    value = sorted(value)
print(",".join(value) if isinstance(value, list) else value)
' "$1" "$2"
}
# pointer <session_id>: the launch id on a pointer's first line; pointer_budget
# the budget file on its second.
pointer() { head -n 1 "$TOOL_BUDGET_STATE_DIR/$1.launch" 2>/dev/null || printf 'missing'; }
pointer_budget() { sed -n 2p "$TOOL_BUDGET_STATE_DIR/$1.launch" 2>/dev/null || printf 'missing'; }

# Case 14: the
# launch is the budget unit. Two session ids under one LLM_ROOT_LAUNCH_ID, as
# a resume that changes the session id produces, share one count, and the
# limit denies the call that passes it whichever session makes it. Each
# session keeps its own state file too, so readers keyed on the session id
# still see that session's calls, and each gets a pointer to its launch.
BUDGET_FILE="$(budget launch14 '{"tool_calls.total": 3, "warn_at": 1.0}')"
WORK_ITEM="WI-30"
LAUNCH_ID="L14"
call claude l14a Bash; rc_is 0 && ok || bad "launch: first session call 1 allowed" "$(err)"
call claude l14a Read; rc_is 0 && ok || bad "launch: first session call 2 allowed" "$(err)"
call claude l14b Bash; rc_is 0 && ok || bad "launch: resumed session call 3 allowed" "$(err)"
call claude l14b Bash
rc_is 2 && ok || bad "launch: call 4 in the resumed session is denied against the launch count" "rc=$(cat "$scratch/rc") $(err)"
case "$(err)" in *"number 4 against a limit of 3"*) ok ;; *) bad "launch: deny counts the whole launch" "$(err)" ;; esac
[ "$(launch_state L14 total)" = "3" ] && ok || bad "launch: shared total" "$(launch_state L14 total)"
[ "$(launch_state L14 denied)" = "1" ] && ok || bad "launch: shared denied" "$(launch_state L14 denied)"
[ "$(launch_state L14 sessions)" = "l14a,l14b" ] && ok || bad "launch: lists every session id seen" "$(launch_state L14 sessions)"
[ "$(state l14a total)" = "2" ] && ok || bad "launch: first session keeps its own count" "$(state l14a total)"
[ "$(state l14b total)" = "1" ] && ok || bad "launch: resumed session keeps its own count" "$(state l14b total)"
[ "$(pointer l14a)" = "L14" ] && ok || bad "launch: pointer for the first session" "$(pointer l14a)"
[ "$(pointer l14b)" = "L14" ] && ok || bad "launch: pointer for the resumed session" "$(pointer l14b)"
# A warning is given once per launch, not once per session.
BUDGET_FILE="$(budget launch14w '{"tool_calls.total": 10, "warn_at": 0.2}')"
LAUNCH_ID="L14w"
call claude l14c Bash
call claude l14c Bash
case "$(out)" in *systemMessage*) ok ;; *) bad "launch warn: call 2 of 10 warns" "$(out)" ;; esac
call claude l14d Bash
[ -z "$(out)" ] && ok || bad "launch warn: a second session of the launch is not warned again" "$(out)"
call claude l14d Bash
[ -z "$(out)" ] && ok || bad "launch warn: nor when its own count reaches warn_at" "$(out)"

# Case 15 (B-08 fallback): a plain resume carries no LLM_ROOT_* variables. A
# session id with a .launch pointer is held to that launch's budget.json
# under LLM_ROOT_SESSIONS_DIR and counted under the launch; a session id
# without one is counted with no limits, as before.
export LLM_ROOT_SESSIONS_DIR="$scratch/sessions"
mkdir -p "$LLM_ROOT_SESSIONS_DIR/L15" "$TOOL_BUDGET_STATE_DIR"
python3 -c '
import json, sys
json.dump({"work_item": "WI-30", "launch_id": "L15", "runtime": "claude",
           "effective": {"tool_calls.total": 2, "warn_at": 1.0}, "levels": [], "errors": []},
          open(sys.argv[1], "w"))
' "$LLM_ROOT_SESSIONS_DIR/L15/budget.json"
printf 'L15\n' >"$TOOL_BUDGET_STATE_DIR/p15.launch"
BUDGET_FILE=""
WORK_ITEM=""
LAUNCH_ID=""
call claude p15 Bash; rc_is 0 && ok || bad "pointer: call 1 allowed" "$(err)"
call claude p15 Bash; rc_is 0 && ok || bad "pointer: call 2 allowed" "$(err)"
call claude p15 Bash
rc_is 2 && ok || bad "pointer: call 3 denied by the launch's budget" "rc=$(cat "$scratch/rc") $(err)"
case "$(err)" in *WI-30*) ok ;; *) bad "pointer: deny names the launch's work item" "$(err)" ;; esac
[ "$(launch_state L15 total)" = "2" ] && ok || bad "pointer: counted under the launch" "$(launch_state L15 total)"
for n in 1 2 3; do call claude p15none Bash; done
rc_is 0 && ok || bad "no pointer: counted with no limits" "rc=$(cat "$scratch/rc")"
[ "$(state p15none total)" = "3" ] && ok || bad "no pointer: still counted" "$(state p15none total)"
[ "$(pointer p15none)" = "missing" ] && ok || bad "no pointer: none written without a launch" "$(pointer p15none)"
unset LLM_ROOT_SESSIONS_DIR

# Case 16 (B-08, S-14): session-start prints the work item and its limits on a
# resume as well as a fresh start, and a resume that keeps its session id
# writes no second join line; one with a new id is joined with source resume.
BUDGET_FILE="$(budget resume16 '{"tool_calls.total": 20}')"
session_start() {
  printf '%s' "$1" | LLM_ROOT_WORK_ITEM="WI-30" LLM_ROOT_LAUNCH_ID="L16" LLM_ROOT_BUDGET_FILE="$BUDGET_FILE" \
    python3 "$DIR/lib/tool-budget.py" session-start >"$scratch/out" 2>"$scratch/err"
}
session_start '{"source": "startup", "session_id": "r16"}'
session_start '{"source": "resume", "session_id": "r16"}'
case "$(out)" in *"tool_calls.total: 20"*) ok ;; *) bad "resume: session-start prints the limits" "$(out) $(err)" ;; esac
case "$(out)" in *"Work item WI-30"*) ok ;; *) bad "resume: session-start names the work item" "$(out)" ;; esac
[ "$(join_lines r16)" = "1" ] && ok || bad "resume: same session id joined once" "$(join_lines r16)"
session_start '{"source": "resume", "session_id": "r16b"}'
record="$(grep '"r16b"' "$JOIN" 2>/dev/null || true)"
case "$record" in *'"source": "resume"'*) ok ;; *) bad "resume: a new session id is joined with source resume" "$record" ;; esac
session_start '{"source": "clear", "session_id": "r16c"}'
[ -z "$(out)" ] && ok || bad "clear: session-start stays silent" "$(out)"

# Case 17 (Codex review of WI-38): a session launched before the launch-wide
# count existed already has calls in its own state and no launches/ file.
# The launch count starts from every session state of that launch, so the
# hook arriving mid-launch does not reset the budget to zero.
mkdir -p "$TOOL_BUDGET_STATE_DIR"
python3 -c '
import json, os, sys
d = os.environ["TOOL_BUDGET_STATE_DIR"]
json.dump({"session_id": "s17a", "work_item": "WI-30", "launch_id": "L17", "runtime": "claude",
           "total": 15, "per_tool": {"Bash": 15}, "denied": 0, "warned": ["tool_calls.total"]},
          open(os.path.join(d, "s17a.json"), "w"))
json.dump({"session_id": "s17b", "work_item": "WI-30", "launch_id": "L17", "runtime": "claude",
           "total": 5, "per_tool": {"Read": 5}, "denied": 0, "warned": []},
          open(os.path.join(d, "s17b.json"), "w"))
json.dump({"session_id": "s17x", "work_item": "WI-30", "launch_id": "OTHER", "runtime": "claude",
           "total": 9, "per_tool": {"Bash": 9}, "denied": 0, "warned": []},
          open(os.path.join(d, "s17x.json"), "w"))
'
BUDGET_FILE="$(budget seed17 '{"tool_calls.total": 20}')"
WORK_ITEM="WI-30"
LAUNCH_ID="L17"
call claude s17a Bash
rc_is 2 && ok || bad "seed: call 21 of a launch with 20 already counted is denied" "rc=$(cat "$scratch/rc") $(err)"
[ "$(launch_state L17 total)" = "20" ] && ok || bad "seed: the launch total starts from its sessions' states" "$(launch_state L17 total)"
[ "$(launch_state L17 sessions)" = "s17a,s17b" ] && ok || bad "seed: the launch lists the sessions it was seeded from" "$(launch_state L17 sessions)"

# Case 18 (Codex review of WI-38): a launch made with --session-root keeps its
# budget.json outside the default sessions directory. The pointer records the
# budget file, so a plain resume with no LLM_ROOT_* variables still finds it.
custom="$scratch/custom-root/L18"
mkdir -p "$custom"
printf '%s' '{"work_item": "WI-30", "launch_id": "L18", "effective": {"tool_calls.total": 2, "warn_at": 1.0}}' >"$custom/budget.json"
BUDGET_FILE="$custom/budget.json"
WORK_ITEM="WI-30"
LAUNCH_ID="L18"
call claude p18 Bash; rc_is 0 && ok || bad "custom root: launched call 1 allowed" "$(err)"
[ "$(pointer_budget p18)" = "$custom/budget.json" ] && ok || bad "custom root: the pointer names the budget file" "$(pointer_budget p18)"
BUDGET_FILE=""
WORK_ITEM=""
LAUNCH_ID=""
call claude p18 Bash; rc_is 0 && ok || bad "custom root: resumed call 2 allowed" "$(err)"
call claude p18 Bash
rc_is 2 && ok || bad "custom root: resumed call 3 denied by the launch's own budget file" "rc=$(cat "$scratch/rc") $(err)"

# Case 25 (WI-101a): an interactive session is warned once at warn_at of
# wall_minutes, measured from its first counted call (the launch's under a
# launch), with the same systemMessage the tool-call limits use. Wall time
# is warned, never denied: only a headless launch is stopped at wall_minutes.
# iso_ago <minutes>: an ISO time that many minutes ago, in the hook's format.
iso_ago() {
  python3 -c '
import datetime, sys
moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=float(sys.argv[1]))
print(moment.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
' "$1"
}
# started_ago <state file> <minutes>: rewrite a state's started to that many minutes ago.
started_ago() {
  python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
data["started"] = sys.argv[2]
json.dump(data, open(sys.argv[1], "w"))
' "$1" "$(iso_ago "$2")"
}
BUDGET_FILE="$(budget wall19 '{"wall_minutes": 15}')"
WORK_ITEM="WI-30"
LAUNCH_ID=""
call claude w19 Bash
[ -z "$(out)" ] && ok || bad "wall: the first call is silent" "$(out)"
started_ago "$TOOL_BUDGET_STATE_DIR/w19.json" 5
call claude w19 Bash
[ -z "$(out)" ] && ok || bad "wall: 5 of 15 minutes is silent" "$(out)"
started_ago "$TOOL_BUDGET_STATE_DIR/w19.json" 12.5
call claude w19 Bash
rc_is 0 && ok || bad "wall: the warning call is allowed" "rc=$(cat "$scratch/rc")"
message="$(system_message)"
case "$message" in *wall_minutes*) ok ;; *) bad "wall: 12 of 15 minutes warns naming wall_minutes" "$(out)" ;; esac
case "$message" in *"of 15"*) ok ;; *) bad "wall: the warning names the limit" "$(out)" ;; esac
case "$message" in *WI-30*) ok ;; *) bad "wall: the warning names the work item" "$(out)" ;; esac
expected="Budget for WI-30: wall_minutes is at 12 of 15 minutes (3 left). Plan to finish within it."
[ "$message" = "$expected" ] && ok || bad "wall: the exact warning text, prefixed Budget" "$message"
case "$(state w19 warned)" in *wall_minutes*) ok ;; *) bad "wall: key recorded as warned" "$(state w19 warned)" ;; esac
call claude w19 Bash
[ -z "$(out)" ] && ok || bad "wall: not repeated on the next call" "$(out)"
started_ago "$TOOL_BUDGET_STATE_DIR/w19.json" 30
call claude w19 Bash
rc_is 0 && ok || bad "wall: an interactive call past wall_minutes is allowed, not denied" "rc=$(cat "$scratch/rc") $(err)"
# Under a launch the clock is the launch's, so a resumed session with a new
# id is warned from the launch's start, once per launch.
LAUNCH_ID="L19"
call claude w19a Bash
[ -z "$(out)" ] && ok || bad "wall launch: the first call is silent" "$(out)"
started_ago "$TOOL_BUDGET_STATE_DIR/launches/L19.json" 13
call claude w19b Bash
case "$(system_message)" in *wall_minutes*) ok ;; *) bad "wall launch: a new session of the launch is warned on the launch's clock" "$(out)" ;; esac
case "$(state w19b warned)" in *wall_minutes*) ok ;; *) bad "wall launch: the warned key is mirrored into the session's own state" "$(state w19b warned)" ;; esac
call claude w19a Bash
[ -z "$(out)" ] && ok || bad "wall launch: warned once per launch" "$(out)"
# A tool-call note alone keeps the Tool-call budget prefix; a call that
# carries both a tool-call note and the wall note is prefixed Budget.
BUDGET_FILE="$(budget wall19both '{"tool_calls.total": 10, "wall_minutes": 15, "warn_at": 0.5}')"
LAUNCH_ID=""
for n in 1 2 3 4; do call claude w19c Bash; done
[ -z "$(out)" ] && ok || bad "wall both: call 4 of 10 at under a minute is silent" "$(out)"
started_ago "$TOOL_BUDGET_STATE_DIR/w19c.json" 8
call claude w19c Bash
expected="Budget for WI-30: tool_calls.total is at 5 of 10 (5 left); wall_minutes is at 8 of 15 minutes (7 left). Plan to finish within it."
[ "$(system_message)" = "$expected" ] && ok || bad "wall both: one message carries both notes, prefixed Budget" "$(out)"
BUDGET_FILE="$(budget wall19calls '{"tool_calls.total": 2, "wall_minutes": 15, "warn_at": 0.5}')"
call claude w19d Bash
expected="Tool-call budget for WI-30: tool_calls.total is at 1 of 2 (1 left). Plan to finish within it."
[ "$(system_message)" = "$expected" ] && ok || bad "wall both: a tool-call note alone keeps the Tool-call budget prefix" "$(out)"
# A launch seeded from its session states (case 17) keeps the earliest
# session start, so the hook arriving mid launch does not restart its clock.
# A state written before started existed counts from its updated.
early="$(iso_ago 14)"
earliest="$(iso_ago 16)"
python3 -c '
import json, os, sys
d = os.environ["TOOL_BUDGET_STATE_DIR"]
early, late, earliest = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({"session_id": "s19s", "work_item": "WI-30", "launch_id": "L19s", "runtime": "claude",
           "total": 2, "per_tool": {"Bash": 2}, "started": early, "updated": late, "warned": []},
          open(os.path.join(d, "s19s.json"), "w"))
json.dump({"session_id": "s19t", "work_item": "WI-30", "launch_id": "L19s", "runtime": "claude",
           "total": 1, "per_tool": {"Bash": 1}, "started": late, "updated": late, "warned": []},
          open(os.path.join(d, "s19t.json"), "w"))
json.dump({"session_id": "s19u", "work_item": "WI-30", "launch_id": "L19s", "runtime": "claude",
           "total": 1, "per_tool": {"Bash": 1}, "updated": earliest, "warned": []},
          open(os.path.join(d, "s19u.json"), "w"))
' "$early" "$(iso_ago 1)" "$earliest"
BUDGET_FILE="$(budget wall19seed '{"wall_minutes": 15}')"
LAUNCH_ID="L19s"
call claude s19t Bash
[ "$(launch_state L19s started)" = "$earliest" ] && ok || bad "wall seed: a seeded launch keeps its sessions' earliest start" "$(launch_state L19s started)"
case "$(system_message)" in *wall_minutes*) ok ;; *) bad "wall seed: warned on the earliest session's clock" "$(out)" ;; esac
# A launch file written before started existed takes its updated, as a
# session state does, rather than the time of this call.
updated="$(iso_ago 13)"
python3 -c '
import json, os, sys
d = os.path.join(os.environ["TOOL_BUDGET_STATE_DIR"], "launches")
json.dump({"launch_id": "L19L", "work_item": "WI-30", "sessions": ["s19l"], "total": 3,
           "per_tool": {"Bash": 3}, "denied": 0, "warned": [], "updated": sys.argv[1]},
          open(os.path.join(d, "L19L.json"), "w"))
' "$updated"
LAUNCH_ID="L19L"
call claude s19m Bash
[ "$(launch_state L19L started)" = "$updated" ] && ok || bad "wall legacy launch: a launch without started starts at its last update" "$(launch_state L19L started)"
case "$(system_message)" in *wall_minutes*) ok ;; *) bad "wall legacy launch: warned on the launch's last update" "$(out)" ;; esac
# The launcher records when it started the runtime as started in budget.json,
# the clock a headless launch is stopped by, so the warning runs on that
# clock too, not from the first tool call, which can come minutes later.
spawned="$(iso_ago 13)"
python3 -c '
import json, sys
json.dump({"work_item": "WI-30", "launch_id": "L19b", "runtime": "claude", "started": sys.argv[2],
           "effective": {"wall_minutes": 15}, "levels": [], "errors": []}, open(sys.argv[1], "w"))
' "$scratch/wall19spawn.json" "$spawned"
BUDGET_FILE="$scratch/wall19spawn.json"
LAUNCH_ID="L19b"
call claude s19b Bash
case "$(system_message)" in *"wall_minutes is at 13 of 15"*) ok ;; *) bad "wall spawn: the first call of a launch is warned on the launcher's clock" "$(out)" ;; esac
[ "$(launch_state L19b started)" = "$spawned" ] && ok || bad "wall spawn: the launch count starts at the launcher's started" "$(launch_state L19b started)"
# A first warning that comes past the limit says 0 left, never a negative
# count. A zero-minute limit warns on the first interactive call.
BUDGET_FILE="$(budget wall19late '{"wall_minutes": 15}')"
LAUNCH_ID=""
call claude w19z Bash
started_ago "$TOOL_BUDGET_STATE_DIR/w19z.json" 30
call claude w19z Bash
case "$(system_message)" in *"wall_minutes is at 30 of 15 minutes (0 left)"*) ok ;; *) bad "wall late: a first warning past the limit says 0 left" "$(out)" ;; esac
BUDGET_FILE="$(budget wall19zero '{"wall_minutes": 0}')"
call claude w19o Bash
expected="Budget for WI-30: wall_minutes is at 0 of 0 minutes (0 left). Plan to finish within it."
[ "$(system_message)" = "$expected" ] && ok || bad "wall zero: the first call warns that no time remains" "$(out)"
call claude w19o Bash
[ -z "$(out)" ] && ok || bad "wall zero: the warning is not repeated" "$(out)"
# With no wall_minutes set nothing about wall time is said, however old the session.
BUDGET_FILE="$(budget wall19none '{"tool_calls.total": 1000}')"
LAUNCH_ID=""
call claude w19n Bash
started_ago "$TOOL_BUDGET_STATE_DIR/w19n.json" 600
call claude w19n Bash
[ -z "$(out)" ] && ok || bad "wall: no wall_minutes, no wall warning" "$(out)"
BUDGET_FILE=""
WORK_ITEM=""

# G-02: on Claude
# Code a call is counted when it ran, not when this hook admitted it. A
# PreToolUse that carries a tool_use_id reserves a slot; PostToolUse or
# PostToolUseFailure with the same id counts it, once; a deny record naming
# the id, or age past the tool timeout, releases it uncounted.
# ev <event> <session> <tool> <tool_use_id> [content]: a Claude Code payload.
ev() {
  python3 -c '
import json, sys
event, session, tool, use_id = sys.argv[1:5]
data = {"hook_event_name": event, "session_id": session, "cwd": "/tmp", "tool_name": tool,
        "tool_use_id": use_id, "tool_input": {"command": "ls"}}
if len(sys.argv) > 5:
    data["tool_input"] = {"file_path": "/tmp/g02.md", "content": sys.argv[5]}
print(json.dumps(data))
' "$@"
}
BUDGET_FILE=""
WORK_ITEM=""
LAUNCH_ID=""

# Case 19: reserve at PreToolUse, count at PostToolUse, count once.
run "$(ev PreToolUse r19 Bash toolu_a)"; rc_is 0 && ok || bad "reserve: PreToolUse allowed" "$(err)"
[ "$(state r19 total)" = "0" ] && ok || bad "reserve: an admitted call is not counted before it runs" "$(state r19 total)"
[ "$(state r19 reserved)" = "toolu_a" ] && ok || bad "reserve: the call holds a reservation" "$(state r19 reserved)"
run "$(ev PostToolUse r19 Bash toolu_a)"; rc_is 0 && ok || bad "confirm: PostToolUse allowed" "$(err)"
[ "$(state r19 total)" = "1" ] && ok || bad "confirm: PostToolUse counts the call" "$(state r19 total)"
[ "$(state r19 per_tool.Bash)" = "1" ] && ok || bad "confirm: counted per tool" "$(state r19 per_tool.Bash)"
[ "$(state r19 per_agent.main)" = "1" ] && ok || bad "confirm: counted per agent" "$(state r19 per_agent.main)"
[ "$(state r19 reserved)" = "" ] && ok || bad "confirm: the reservation is gone" "$(state r19 reserved)"
run "$(ev PostToolUse r19 Bash toolu_a)"
[ "$(state r19 total)" = "1" ] && ok || bad "confirm: a repeated PostToolUse is not counted twice" "$(state r19 total)"
run "$(ev PreToolUse r19 Read toolu_b)"
run "$(ev PostToolUseFailure r19 Read toolu_b)"; rc_is 0 && ok || bad "confirm: PostToolUseFailure allowed" "$(err)"
[ "$(state r19 total)" = "2" ] && ok || bad "confirm: a call that ran and failed still ran" "$(state r19 total)"

# Case 20 (the record's acceptance line): the prose guard denies a Write that
# this hook admitted. No PostToolUse follows, so the total is unchanged, and
# the prose guard's deny record, which names the tool_use_id, releases the
# reservation on this hook's next run.
content="$(printf 'a sentence \342\200\224 with an em dash')"
write_payload="$(ev PreToolUse r20 Write toolu_w "$content")"
run "$write_payload"; rc_is 0 && ok || bad "prose: the budget admits the Write" "$(err)"
prose_rc=0
printf '%s' "$write_payload" | bash "$DIR/prose-guard.sh" >"$scratch/prose-out" 2>/dev/null || prose_rc=$?
case "$(cat "$scratch/prose-out")" in *deny*) ok ;; *) [ "$prose_rc" = "2" ] && ok || bad "prose: the prose guard denies the Write" "rc=$prose_rc $(cat "$scratch/prose-out")" ;; esac
[ "$(state r20 total)" = "0" ] && ok || bad "prose: the denied Write is not counted" "$(state r20 total)"
run "$(ev PreToolUse r20 Read toolu_r)"
[ "$(state r20 reserved)" = "toolu_r" ] && ok || bad "prose: the deny record released the Write's reservation" "$(state r20 reserved)"
[ "$(state r20 total)" = "0" ] && ok || bad "prose: the total is still unchanged" "$(state r20 total)"

# Case 21: a call still in flight holds its reservation past the former
# timeout. Only its post event turns that slot into a counted call.
BUDGET_FILE="$(budget reserve21 '{"tool_calls.total": 1, "warn_at": 1.0}')"
run "$(ev PreToolUse r21 Bash toolu_c)"; rc_is 0 && ok || bad "limit: the first call is admitted" "$(err)"
run "$(ev PreToolUse r21 Bash toolu_d)"
rc_is 2 && ok || bad "limit: an open reservation fills the last slot" "rc=$(cat "$scratch/rc")"
python3 -c '
import json, os
path = os.path.join(os.environ["TOOL_BUDGET_STATE_DIR"], "r21.json")
data = json.load(open(path))
for entry in data["reserved"].values():
    entry["since"] = 0
json.dump(data, open(path, "w"))
'
run "$(ev PreToolUse r21 Bash toolu_e)"
rc_is 2 && ok || bad "limit: an old in-flight reservation still fills the slot" "rc=$(cat "$scratch/rc") $(err)"
[ "$(state r21 reserved)" = "toolu_c" ] && ok || bad "limit: the in-flight reservation is retained" "$(state r21 reserved)"
run "$(ev PostToolUse r21 Bash toolu_c)"
[ "$(state r21 total)" = "1" ] && ok || bad "limit: the original call is counted when it finishes" "$(state r21 total)"
run "$(ev PreToolUse r21 Bash toolu_f)"
rc_is 2 && ok || bad "limit: the confirmed call still fills the slot" "rc=$(cat "$scratch/rc") $(err)"

# Case 22 (the record's acceptance line): with one call left under a limit,
# two parallel calls let exactly one through, under a launch as well.
BUDGET_FILE="$(budget parallel22 '{"tool_calls.total": 3, "warn_at": 1.0}')"
WORK_ITEM="WI-95"
LAUNCH_ID="L22"
for id in toolu_p1 toolu_p2; do
  run "$(ev PreToolUse r22 Bash "$id")"
  run "$(ev PostToolUse r22 Bash "$id")"
done
[ "$(launch_state L22 total)" = "2" ] && ok || bad "parallel: two calls confirmed under the launch" "$(launch_state L22 total)"
for id in toolu_q1 toolu_q2; do
  ev PreToolUse r22 Bash "$id" >"$scratch/p-$id"
  ( rc=0; LLM_ROOT_BUDGET_FILE="$BUDGET_FILE" LLM_ROOT_WORK_ITEM="$WORK_ITEM" LLM_ROOT_LAUNCH_ID="$LAUNCH_ID" \
      bash "$HOOK" <"$scratch/p-$id" >/dev/null 2>&1 || rc=$?; printf '%s' "$rc" >"$scratch/rc-$id" ) &
done
wait
allowed=0
for id in toolu_q1 toolu_q2; do [ "$(cat "$scratch/rc-$id")" = "0" ] && allowed=$((allowed + 1)); done
[ "$allowed" = "1" ] && ok || bad "parallel: exactly one of two parallel calls is let through" "allowed=$allowed"
[ "$(launch_state L22 reserved)" = "toolu_q1" ] || [ "$(launch_state L22 reserved)" = "toolu_q2" ] \
  && ok || bad "parallel: the launch holds the one reservation" "$(launch_state L22 reserved)"
BUDGET_FILE=""
WORK_ITEM=""
LAUNCH_ID=""

# Case 23: a runtime that is not confirmed at PostToolUse keeps counting at
# PreToolUse, whatever its payload carries, and ignores a post event.
RT=codex
run "$(ev PreToolUse r23 Bash toolu_x)"
[ "$(state r23 total)" = "1" ] && ok || bad "codex: counted at PreToolUse" "$(state r23 total)"
run "$(ev PostToolUse r23 Bash toolu_x)"
[ "$(state r23 total)" = "1" ] && ok || bad "codex: a post event is not counted again" "$(state r23 total)"
RT=""

# Case 24: hooks.json registers this hook for PostToolUse and
# PostToolUseFailure with no matcher, so every call that ran is confirmed.
registration="$(python3 -c '
import json, sys
groups = json.load(open(sys.argv[1]))
bad = []
for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
    hits = [g.get("matcher", "") for g in groups.get(event, []) for h in g.get("hooks", []) if h.get("command", "").endswith("/tool-budget-guard.sh")]
    if hits != [""]:
        bad.append(f"{event}: {hits}")
print("ok" if not bad else "bad " + "; ".join(bad))
' "$DIR/hooks.json" 2>&1)" || true
[ "$registration" = "ok" ] && ok || bad "hooks.json registers the budget hook on every tool event" "$registration"

# Case 25: a
# warning is delayed until the call that reaches warn_at is actually confirmed
# to have run, never fired on a reservation that a sibling guard can still
# deny and prune uncounted before it ever really runs.
BUDGET_FILE="$(budget delaywarn '{"tool_calls.total": 4, "warn_at": 0.25}')"
content="$(printf 'a sentence \342\200\224 with an em dash')"
run "$(ev PreToolUse r25 Write toolu_w "$content")"
[ -z "$(system_message)" ] && ok || bad "delay warn: a bare reservation is not warned, even one that crosses warn_at" "$(out)"
[ "$(state r25 warned)" = "" ] && ok || bad "delay warn: nothing is marked warned by a reservation alone" "$(state r25 warned)"
prose_rc=0
printf '%s' "$(ev PreToolUse r25 Write toolu_w "$content")" | bash "$DIR/prose-guard.sh" >"$scratch/prose-out" 2>/dev/null || prose_rc=$?
case "$(cat "$scratch/prose-out")" in *deny*) ok ;; *) [ "$prose_rc" = "2" ] && ok || bad "delay warn: the prose guard denies the Write" "rc=$prose_rc $(cat "$scratch/prose-out")" ;; esac
run "$(ev PreToolUse r25 Bash toolu_z)"
[ "$(state r25 reserved)" = "toolu_z" ] && ok || bad "delay warn: the denied Write's reservation is released" "$(state r25 reserved)"
[ "$(state r25 warned)" = "" ] && ok || bad "delay warn: still nothing warned after the second reservation" "$(state r25 warned)"
run "$(ev PostToolUse r25 Bash toolu_z)"
[ "$(state r25 total)" = "1" ] && ok || bad "delay warn: the confirmed call, the only one that ever really ran, is counted" "$(state r25 total)"
message="$(system_message)"
case "$message" in *"tool_calls.total is at 1 of 4"*) ok ;; *) bad "delay warn: the confirmed call gets the warning the pruned reservation never earned" "$message" ;; esac
[ "$(state r25 warned)" = "tool_calls.total" ] && ok || bad "delay warn: the key is marked only once a call is actually confirmed" "$(state r25 warned)"
BUDGET_FILE=""

# Case 26 (#162): the post-confirm warn check reads the launch's shared
# count (unit_count_total = launch_total when launch_path is set), not the
# confirming session's own, smaller count, when the call is under a launch.
BUDGET_FILE="$(budget delaywarnlaunch '{"tool_calls.total": 4, "warn_at": 0.5}')"
WORK_ITEM="WI-30"
LAUNCH_ID="L26"
run "$(ev PreToolUse r26a Bash toolu_la)"
run "$(ev PostToolUse r26a Bash toolu_la)"
[ "$(launch_state L26 total)" = "1" ] && ok || bad "delay warn launch: first session's confirm counted on the launch" "$(launch_state L26 total)"
[ -z "$(system_message)" ] && ok || bad "delay warn launch: launch total 1 of 4 is below warn_at 2" "$(out)"
[ "$(launch_state L26 warned)" = "" ] && ok || bad "delay warn launch: nothing warned yet" "$(launch_state L26 warned)"
run "$(ev PreToolUse r26b Bash toolu_lb)"
[ -z "$(system_message)" ] && ok || bad "delay warn launch: a second session's bare reservation is not warned" "$(out)"
run "$(ev PostToolUse r26b Bash toolu_lb)"
[ "$(launch_state L26 total)" = "2" ] && ok || bad "delay warn launch: the shared launch total reaches 2" "$(launch_state L26 total)"
message="$(system_message)"
case "$message" in *"tool_calls.total is at 2 of 4"*) ok ;; *) bad "delay warn launch: warns on the launch's shared confirmed count (2), not either session's own count (1)" "$message" ;; esac
[ "$(launch_state L26 warned)" = "tool_calls.total" ] && ok || bad "delay warn launch: the key is marked warned on the launch" "$(launch_state L26 warned)"
[ "$(state r26b warned)" = "tool_calls.total" ] && ok || bad "delay warn launch: mark_warned also records on the confirming session" "$(state r26b warned)"
BUDGET_FILE=""
WORK_ITEM=""
LAUNCH_ID=""

if [ "$FAIL" -gt 0 ]; then
  printf 'tool-budget-guard.test: FAIL (%d passed, %d failed)\n' "$PASS" "$FAIL" >&2
  exit 1
fi
printf 'tool-budget-guard.test: passed (%d cases)\n' "$PASS"
