#!/usr/bin/env bash
# Functional test for hooks/lib/fleet-cap.py and its call from
# subagent-cap-guard.sh (WI-101b): the machine-wide cap on live agents of the
# fleet model. Fixture transcripts stand in for ~/.claude/projects; the live
# directory and components.json are never read.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../stratarc/data/hooks" && pwd)"
LIB="$DIR/lib/fleet-cap.py"
GUARD="$DIR/subagent-cap-guard.sh"
PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1" >&2; [ -n "${2:-}" ] && printf '       %s\n' "$2" >&2; return 0; }

scratch="$(mktemp -d "${TMPDIR:-/tmp}/fleet-cap-XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
export HOME="$scratch/home"; mkdir -p "$HOME"
export LLM_ROOT="$scratch/root"; mkdir -p "$LLM_ROOT"
export FLEET_CAP_PROJECTS_DIR="$scratch/projects"
export RUNTIME_HOOK_STATE_DIR="$scratch/state"
export GUARD_LOG_DIR="$scratch/telemetry"
unset FLEET_CAP_TTL_SECS CLAUDE_CODE_SUBAGENT_MODEL RUNTIME_HOOKS_DISABLE

# fleet <model> <concurrent>: write components.json budgets.fleet; "none" writes a null block.
fleet() {
  python3 -c '
import json, sys
model, cap = sys.argv[2], sys.argv[3]
block = {"model": None, "concurrent": None} if model == "none" else {"model": model, "concurrent": int(cap)}
json.dump({"budgets": {"wall_minutes": 15, "fleet": block}}, open(sys.argv[1], "w"))
' "$LLM_ROOT/components.json" "$1" "${2:-0}"
}

# agents <count> <model> <kind> [age seconds]: plant <count> subagent
# transcripts under one fixture session. kind is "running" (its last entry is
# a tool result) or "finished" (its last entry ended its turn). age backdates
# the files' mtime.
agents() {
  python3 -c '
import json, os, sys, time, uuid
base, count, model, kind, age = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5])
directory = os.path.join(base, "-fixture-project", "session-" + uuid.uuid4().hex[:8], "subagents")
os.makedirs(directory, exist_ok=True)
for _ in range(count):
    path = os.path.join(directory, "agent-" + uuid.uuid4().hex[:12] + ".jsonl")
    lines = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"model": model, "stop_reason": "tool_use", "content": []}},
    ]
    if kind == "finished":
        lines.append({"type": "assistant", "message": {"model": model, "stop_reason": "end_turn", "content": []}})
    elif kind == "structured":
        lines.append({"type": "user", "toolEndsTurn": True, "message": {"role": "user", "content": [{"type": "tool_result"}]}})
    elif kind == "interrupted":
        lines.append({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}})
    elif kind == "meta":
        lines.append({"type": "assistant", "message": {"model": model, "stop_reason": "end_turn", "content": []}})
        lines.append({"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-stdout>ok</local-command-stdout>"}})
    else:
        lines.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result"}]}})
    with open(path, "w") as handle:
        handle.write("\n".join(json.dumps(line) for line in lines) + "\n")
    moment = time.time() - age
    os.utime(path, (moment, moment))
' "$FLEET_CAP_PROJECTS_DIR" "$1" "$2" "$3" "${4:-0}"
}

reset() { rm -rf "$FLEET_CAP_PROJECTS_DIR" "$RUNTIME_HOOK_STATE_DIR"; mkdir -p "$FLEET_CAP_PROJECTS_DIR"; }

# dispatch <model or ""> [transcript]: a PreToolUse payload for an Agent call.
dispatch() {
  python3 -c '
import json, sys
tool_input = {"subagent_type": "general-purpose", "prompt": "x"}
if sys.argv[1]:
    tool_input["model"] = sys.argv[1]
payload = {"hook_event_name": "PreToolUse", "session_id": "fleet-parent", "tool_name": "Agent", "tool_input": tool_input}
if sys.argv[2]:
    payload["transcript_path"] = sys.argv[2]
print(json.dumps(payload))
' "$1" "${2:-}"
}

# check <payload>: run the library's check mode, leaving stdout and the status in scratch files.
check() {
  rc=0
  printf '%s' "$1" | python3 "$LIB" check >"$scratch/out" 2>"$scratch/err" || rc=$?
  printf '%s' "$rc" >"$scratch/rc"
}
rc() { cat "$scratch/rc"; }
out() { cat "$scratch/out"; }
live() { python3 "$LIB" count | python3 -c 'import json,sys; print(json.load(sys.stdin)["live"])'; }

fleet claude-fable-5-1 20

# Case 1: 19 live Fable agents are under the cap of 20, so a Fable dispatch is allowed.
reset
agents 19 claude-fable-5-1 running
[ "$(live)" = "19" ] && ok || bad "19: count mode sees 19 live" "$(python3 "$LIB" count)"
check "$(dispatch fable)"
[ "$(rc)" = "0" ] && ok || bad "19: a Fable dispatch is allowed below the cap" "rc=$(rc) $(out)"

# Case 2: 20 live Fable agents are at the cap, so the next Fable dispatch is
# reported over the cap (exit 3) with a warning naming the count, the model
# and the cap. The cap is advisory: the warning says the dispatch still runs.
agents 1 claude-fable-5-1 running
[ "$(live)" = "20" ] && ok || bad "20: count mode sees 20 live"
check "$(dispatch fable)"
[ "$(rc)" = "3" ] && ok || bad "20: a Fable dispatch at the cap is reported over the cap with exit 3" "rc=$(rc) $(out)"
case "$(out)" in *"20 agents are already running on claude-fable-5-1"*"fleet cap of 20"*) ok ;; *) bad "20: the warning names the count, the model and the cap" "$(out)" ;; esac
case "$(out)" in *"advisory"*) ok ;; *) bad "20: the warning says the cap is advisory and the dispatch still runs" "$(out)" ;; esac
case "$(out)" in *denied*) bad "20: the warning never says denied" "$(out)" ;; *) ok ;; esac
check "$(dispatch claude-fable-5-1)"
[ "$(rc)" = "3" ] && ok || bad "20: a dispatch naming the full id is reported over the cap" "rc=$(rc)"
check "$(dispatch 'claude-fable-5-1[1m]')"
[ "$(rc)" = "3" ] && ok || bad "20: a dispatch naming the id with a context suffix is denied" "rc=$(rc)"
check "$(dispatch 'fable[1m]')"
[ "$(rc)" = "3" ] && ok || bad "20: a dispatch naming the alias with a context suffix is denied" "rc=$(rc)"
check "$(dispatch sonnet)"
[ "$(rc)" = "0" ] && ok || bad "20: a dispatch on another model is allowed" "rc=$(rc) $(out)"

# Case 3: an agent on another model is not counted.
reset
agents 19 claude-fable-5-1 running
agents 1 claude-sonnet-5 running
[ "$(live)" = "19" ] && ok || bad "non-Fable: the Sonnet agent is not counted" "$(live)"
check "$(dispatch fable)"
[ "$(rc)" = "0" ] && ok || bad "non-Fable: 19 Fable and 1 Sonnet allow a Fable dispatch" "rc=$(rc) $(out)"

# Case 4: a stale agent, its transcript unwritten past the TTL, is not counted.
reset
agents 19 claude-fable-5-1 running
agents 1 claude-fable-5-1 running 1000
[ "$(live)" = "19" ] && ok || bad "stale: a transcript written 1000 seconds ago is not counted" "$(live)"
check "$(dispatch fable)"
[ "$(rc)" = "0" ] && ok || bad "stale: 19 live and 1 stale allow a Fable dispatch" "rc=$(rc) $(out)"
FLEET_CAP_TTL_SECS=2000 check "$(dispatch fable)"
[ "$(rc)" = "3" ] && ok || bad "stale: the TTL is FLEET_CAP_TTL_SECS" "rc=$(rc)"

# Case 5: an agent whose last entry ended its turn is not counted.
reset
agents 19 claude-fable-5-1 running
agents 1 claude-fable-5-1 finished
[ "$(live)" = "19" ] && ok || bad "finished: an agent that ended its turn is not counted" "$(live)"
agents 1 claude-fable-5-1 structured
[ "$(live)" = "19" ] && ok || bad "finished: a workflow agent whose tool result ended its turn is not counted" "$(live)"
agents 1 claude-fable-5-1 interrupted
[ "$(live)" = "19" ] && ok || bad "finished: an agent the user interrupted is not counted" "$(live)"
agents 1 claude-fable-5-1 meta
[ "$(live)" = "19" ] && ok || bad "finished: slash command output after a finished turn does not revive it" "$(live)"

# Case 6: with no tool_input.model the new agent runs on the caller's model,
# read from the caller's own transcript; a "<synthetic>" entry is skipped.
reset
agents 20 claude-fable-5-1 running
caller="$scratch/caller.jsonl"
printf '%s\n' '{"type":"assistant","message":{"model":"claude-fable-5-1","stop_reason":"tool_use"}}' '{"type":"assistant","message":{"model":"<synthetic>","stop_reason":"tool_use"}}' >"$caller"
check "$(dispatch '' "$caller")"
[ "$(rc)" = "3" ] && ok || bad "caller: a dispatch from a Fable caller inherits Fable and is denied" "rc=$(rc) $(out)"
printf '%s\n' '{"type":"assistant","message":{"model":"claude-sonnet-5","stop_reason":"tool_use"}}' >"$caller"
check "$(dispatch '' "$caller")"
[ "$(rc)" = "0" ] && ok || bad "caller: a dispatch from a Sonnet caller is allowed" "rc=$(rc) $(out)"
CLAUDE_CODE_SUBAGENT_MODEL=fable check "$(dispatch '' "$caller")"
[ "$(rc)" = "3" ] && ok || bad "caller: CLAUDE_CODE_SUBAGENT_MODEL names the subagent model before the caller's" "rc=$(rc)"

CLAUDE_CODE_SUBAGENT_MODEL=fable check "$(dispatch sonnet "$caller")"
[ "$(rc)" = "0" ] && ok || bad "caller: tool_input.model wins over CLAUDE_CODE_SUBAGENT_MODEL" "rc=$(rc)"
check "$(dispatch inherit "$caller")"
[ "$(rc)" = "0" ] && ok || bad "caller: model inherit falls through to the Sonnet caller" "rc=$(rc)"
printf '%s\n' '{"type":"assistant","message":{"model":"claude-fable-5-1","stop_reason":"tool_use"}}' >"$caller"
check "$(dispatch inherit "$caller")"
[ "$(rc)" = "3" ] && ok || bad "caller: model inherit falls through to the Fable caller" "rc=$(rc)"

# Case 6b: a subagent dispatching is judged on its own transcript, found from
# the payload's agent_id beside the session transcript it names.
session_file="$scratch/sess/abc.jsonl"
mkdir -p "$scratch/sess/abc/subagents"
printf '%s\n' '{"type":"assistant","message":{"model":"claude-sonnet-5","stop_reason":"tool_use"}}' >"$session_file"
printf '%s\n' '{"type":"assistant","message":{"model":"claude-fable-5-1","stop_reason":"tool_use"}}' >"$scratch/sess/abc/subagents/agent-sub1.jsonl"
payload="$(dispatch '' "$session_file" | python3 -c 'import json,sys; d=json.load(sys.stdin); d["agent_id"]="sub1"; print(json.dumps(d))')"
check "$payload"
[ "$(rc)" = "3" ] && ok || bad "subagent caller: a Fable subagent of a Sonnet session inherits Fable" "rc=$(rc) $(out)"

# Case 6c: a transcript whose last model entry is further back than the
# first tail window is still read, and a main session's own transcript counts.
reset
agents 19 claude-fable-5-1 running
python3 -c '
import json, os, sys
path = os.path.join(sys.argv[1], "-fixture-project", "long-session.jsonl")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as handle:
    handle.write(json.dumps({"type": "assistant", "message": {"model": "claude-fable-5-1", "stop_reason": "tool_use"}}) + "\n")
    padding = "x" * 1000
    for _ in range(200):
        handle.write(json.dumps({"type": "progress", "data": padding}) + "\n")
    handle.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result"}]}}) + "\n")
' "$FLEET_CAP_PROJECTS_DIR"
[ "$(live)" = "20" ] && ok || bad "long: a main session whose model is 200 KB back is counted" "$(live)"

# Case 7: no fleet block, or a null one, is no cap.
fleet none
check "$(dispatch fable)"
[ "$(rc)" = "0" ] && ok || bad "unset: a null fleet block is no cap" "rc=$(rc) $(out)"
fleet claude-fable-5-1 20

# Case 8: through the guard. The fleet cap is advisory (the operator's
# decision of 2026-09-24): at the cap the subagent dispatch guard admits the
# dispatch, counts it against the parent like any other, and returns the
# library's warning as a systemMessage. Below the cap it admits silently.
reset
agents 20 claude-fable-5-1 running
guard_out="$(dispatch fable | bash "$GUARD")"
case "$guard_out" in *deny*) bad "guard: at the fleet cap the dispatch is still admitted" "$guard_out" ;; *) ok ;; esac
case "$guard_out" in *'"systemMessage"'*"fleet cap of 20"*) ok ;; *) bad "guard: at the fleet cap it warns with a systemMessage naming the cap" "$guard_out" ;; esac
# Since G-02 an admitted dispatch holds a pending lock until its start
# signal confirms it as a child lock; either one counts against the parent.
locks="$(find "$RUNTIME_HOOK_STATE_DIR" \( -name 'pending-*.lock' -o -name 'child-*.lock' \) 2>/dev/null | wc -l | tr -d ' ')"
[ "$locks" = "1" ] && ok || bad "guard: a dispatch admitted over the fleet cap is counted against its parent" "$locks"
reset
agents 19 claude-fable-5-1 running
guard_out="$(dispatch fable | bash "$GUARD")"
[ -z "$guard_out" ] && ok || bad "guard: below the fleet cap the dispatch is admitted silently" "$guard_out"
guard_out="$(printf 'not json' | bash "$GUARD" 2>/dev/null)" || true
case "$guard_out" in *deny*) bad "guard: a malformed payload still fails open" "$guard_out" ;; *) ok ;; esac

# Case 9: the source root is STRATARC_SOURCE, else LLM_ROOT, and nothing else:
# a copy that sits beside a components.json finds no cap by location, and with
# neither variable there is no cap.
configured() { env -u LLM_ROOT -u STRATARC_SOURCE "$@" python3 "$LIB" count | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["model"], d["concurrent"])'; }
tree="$scratch/tree"; mkdir -p "$tree/hooks/lib"
cp "$LIB" "$tree/hooks/lib/fleet-cap.py"
printf '%s\n' '{"budgets": {"fleet": {"model": "claude-tree-1", "concurrent": 7}}}' >"$tree/components.json"
[ "$(env -u LLM_ROOT -u STRATARC_SOURCE python3 "$tree/hooks/lib/fleet-cap.py" count | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["model"], d["concurrent"])')" = "None None" ] && ok || bad "derived: a copy under <tree>/hooks/lib does not read <tree>/components.json by location"
[ "$(configured)" = "None None" ] && ok || bad "derived: with neither variable there is no cap"
[ "$(configured STRATARC_SOURCE="$tree")" = "claude-tree-1 7" ] && ok || bad "derived: STRATARC_SOURCE names the source root"
[ "$(configured LLM_ROOT="$tree")" = "claude-tree-1 7" ] && ok || bad "derived: LLM_ROOT names the source root when STRATARC_SOURCE is unset"
decoy="$scratch/decoy"; mkdir -p "$decoy"
printf '%s\n' '{"budgets": {"fleet": {"model": "claude-decoy-9", "concurrent": 3}}}' >"$decoy/components.json"
[ "$(configured STRATARC_SOURCE="$tree" LLM_ROOT="$decoy")" = "claude-tree-1 7" ] && ok || bad "derived: STRATARC_SOURCE wins over LLM_ROOT"

if [ "$FAIL" -gt 0 ]; then
  printf 'fleet-cap.test: FAIL (%d passed, %d failed)\n' "$PASS" "$FAIL" >&2
  exit 1
fi
printf 'fleet-cap.test: passed (%d cases)\n' "$PASS"
