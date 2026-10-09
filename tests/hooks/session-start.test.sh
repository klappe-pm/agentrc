#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)"
HOOK="$DIR/session-start.sh"
LLM_ROOT_UNDER_TEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

scratch="$(mktemp -d "${TMPDIR:-/tmp}/session-start-hook-XXXXXX")"
trap 'rm -rf "$scratch"' EXIT

test_home="$scratch/home"
transcripts="$scratch/transcripts"
mkdir -p "$test_home" "$transcripts"

# A test run inside a launched session must not inherit that session's work
# item; the work-item scenarios set these themselves.
unset LLM_ROOT_WORK_ITEM LLM_ROOT_LAUNCH_ID LLM_ROOT_SESSION_DIR LLM_ROOT_BUDGET_FILE LLM_ROOT_HEADLESS
# The program summary (O-06) reads the event log under the test HOME unless a
# scenario points it at a fixture; an inherited override must not leak in.
unset PROGRAM_EVENTS_FILE PROGRAM_SUMMARY_CACHE PROGRAM_SUMMARY_TIMEOUT
export LLM_ROOT_TELEMETRY_DIR="$scratch/telemetry"
join_file="$LLM_ROOT_TELEMETRY_DIR/work-item-sessions.jsonl"

pass=0
fail=0

check_empty() {
  label="$1"
  output="$2"
  if [ -z "$output" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'session-start.test: %s: expected silence, got: %s\n' "$label" "$output" >&2
  fi
}

check_not_contains() {
  label="$1"
  output="$2"
  unexpected="$3"
  if grep -qF "$unexpected" <<< "$output"; then
    fail=$((fail + 1))
    printf 'session-start.test: %s: expected no %s\n' "$label" "$unexpected" >&2
    printf '  got: %s\n' "$output" >&2
  else
    pass=$((pass + 1))
  fi
}

check_contains() {
  label="$1"
  output="$2"
  expected="$3"
  if grep -qF "$expected" <<< "$output"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'session-start.test: %s: expected to find %s\n' "$label" "$expected" >&2
    printf '  got: %s\n' "$output" >&2
  fi
}

# write_transcript <path> <session_id> <cwd> <timestamp> <interrupted 0|1>
write_transcript() {
  path="$1"
  session_id="$2"
  cwd="$3"
  timestamp="$4"
  interrupted="$5"
  SS_PATH="$path" SS_SESSION="$session_id" SS_CWD="$cwd" SS_TS="$timestamp" SS_INTERRUPTED="$interrupted" python3 -c '
import json
import os

session_id = os.environ["SS_SESSION"]
rows = [
    {
        "type": "user",
        "sessionId": session_id,
        "cwd": os.environ["SS_CWD"],
        "gitBranch": "main",
        "timestamp": os.environ["SS_TS"],
        "message": {"role": "user", "content": "do the thing"},
    },
    {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": os.environ["SS_TS"],
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 5, "output_tokens": 6},
            "content": [{"type": "text", "text": "stopped here"}],
        },
    },
    {"type": "ai-title", "aiTitle": "A goal", "sessionId": session_id},
    {
        "type": "cost-state",
        "sessionId": session_id,
        "totalCostUSD": 1.0,
        "totalDuration": 1000,
        "modelUsage": {"claude-opus-5": {"inputTokens": 10, "outputTokens": 20}},
    },
]
if os.environ["SS_INTERRUPTED"] == "1":
    rows.append(
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": os.environ["SS_TS"],
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }
    )
with open(os.environ["SS_PATH"], "w", encoding="utf-8") as handle:
    for row in rows:
        handle.write(json.dumps(row) + "\n")
'
}

hours_ago() {
  python3 -c "
import datetime
print((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=$1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
"
}

payload_for() {
  printf '{"cwd":%s}' "$(printf '%s' "$1" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))')"
}

# payload_with <cwd> <source> <session_id>: a SessionStart payload with the
# fields the runtime sends; an empty source or session_id is left out.
payload_with() {
  SS_CWD="$1" SS_SOURCE="$2" SS_SESSION="$3" python3 -c '
import json, os
data = {"cwd": os.environ["SS_CWD"]}
if os.environ["SS_SOURCE"]:
    data["source"] = os.environ["SS_SOURCE"]
if os.environ["SS_SESSION"]:
    data["session_id"] = os.environ["SS_SESSION"]
print(json.dumps(data))
'
}

# run_raw <payload> <home> [hook]: run the hook, print its stdout, and record
# its exit status in $scratch/rc. The payload is written to a file first so
# a hook that exits before draining stdin cannot break a pipe.
run_raw() {
  printf '%s' "$1" >"$scratch/payload"
  rc=0
  HOME="$2" LLM_ROOT="${run_root:-$LLM_ROOT_UNDER_TEST}" SESSION_CARD_ROOT="$transcripts" COLUMNS=100 NO_COLOR=1 \
    bash "${3:-$HOOK}" <"$scratch/payload" 2>"$scratch/stderr" || rc=$?
  printf '%s' "$rc" >"$scratch/rc"
}

# check_fail_open <label>: the last run exited 0 and wrote nothing to stderr.
check_fail_open() {
  if [ "$(cat "$scratch/rc")" = "0" ] && [ ! -s "$scratch/stderr" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'session-start.test: %s: expected exit 0 and no stderr, got exit %s, stderr: %s\n' \
      "$1" "$(cat "$scratch/rc")" "$(cat "$scratch/stderr")" >&2
  fi
}

run_hook() {
  run_raw "$(payload_for "$1")" "$2"
  check_fail_open "exit status for cwd $1"
}

# --- fixture-free: the source root comes from AGENTRC_SOURCE, then LLM_ROOT, and nowhere else ---
free_cwd="$scratch/projects/free"
mkdir -p "$free_cwd" "$scratch/stub-src/scripts" "$scratch/stub-other/scripts" "$scratch/stub-empty"
printf 'print("STUB-PICKER-SOURCE")\n' >"$scratch/stub-src/scripts/session-resume.py"
printf 'print("STUB-PICKER-OTHER")\n' >"$scratch/stub-other/scripts/session-resume.py"
free_run() {
  printf '%s' "$(payload_with "$free_cwd" startup "")" >"$scratch/payload"
  rc=0
  env -u LLM_ROOT -u AGENTRC_SOURCE HOME="$test_home" COLUMNS=100 NO_COLOR=1 "$@" \
    bash "$HOOK" <"$scratch/payload" 2>"$scratch/stderr" || rc=$?
  printf '%s' "$rc" >"$scratch/rc"
}
out="$(free_run)"
check_fail_open 'no source root set'
check_empty 'no source root set stays silent, with no file-location fallback' "$out"
out="$(free_run AGENTRC_SOURCE="$scratch/stub-empty")"
check_fail_open 'a source root without the picker'
check_empty 'a source root without the picker stays silent' "$out"
out="$(free_run AGENTRC_SOURCE="$scratch/stub-src")"
check_fail_open 'AGENTRC_SOURCE set'
check_contains 'AGENTRC_SOURCE names the source root' "$out" "STUB-PICKER-SOURCE"
out="$(free_run LLM_ROOT="$scratch/stub-other")"
check_contains 'LLM_ROOT names the source root when AGENTRC_SOURCE is unset' "$out" "STUB-PICKER-OTHER"
out="$(free_run AGENTRC_SOURCE="$scratch/stub-src" LLM_ROOT="$scratch/stub-other")"
check_contains 'AGENTRC_SOURCE wins over LLM_ROOT' "$out" "STUB-PICKER-SOURCE"
check_not_contains 'AGENTRC_SOURCE wins over LLM_ROOT, so the other picker does not run' "$out" "STUB-PICKER-OTHER"

# The scenarios below run the operator's own session scripts (session-resume,
# session-launch, program-status). A checkout without them skips those.
if [ ! -f "$LLM_ROOT_UNDER_TEST/scripts/session-resume.py" ]; then
  printf 'session-start.test: %d passed, %d failed (session script scenarios skipped: no scripts/session-resume.py)\n' "$pass" "$fail"
  [ "$fail" -eq 0 ]
  exit $?
fi

# --- scenario 1: home directory prints the roster and asks for a project ---
alpha_dir="$scratch/projects/alpha"
mkdir -p "$alpha_dir"
alpha_id="11111111-1111-1111-1111-111111111111"
write_transcript "$transcripts/alpha.jsonl" "$alpha_id" "$alpha_dir" "$(hours_ago 2)" 0

out="$(run_hook "$test_home" "$test_home")"
# --compact renders only the short id (session_card.Card.short_id, 8 chars).
check_contains 'home directory shows the project roster' "$out" "${alpha_id:0:8}"
check_contains 'home directory names the project' "$out" "alpha"
check_contains 'home directory asks for a project' "$out" "Pick a project"

# --- scenario 2: a project directory with one interrupted card ---
beta_dir="$scratch/projects/beta"
mkdir -p "$beta_dir"
beta_id="22222222-2222-2222-2222-222222222222"
write_transcript "$transcripts/beta.jsonl" "$beta_id" "$beta_dir" "$(hours_ago 3)" 1

out="$(run_hook "$beta_dir" "$test_home")"
check_contains 'a project with an interrupted session prints its resume command' "$out" "claude --resume $beta_id"

# --- scenario 3: a project directory with no interrupted card is silent ---
gamma_dir="$scratch/projects/gamma"
mkdir -p "$gamma_dir"
gamma_id="33333333-3333-3333-3333-333333333333"
write_transcript "$transcripts/gamma.jsonl" "$gamma_id" "$gamma_dir" "$(hours_ago 3)" 0

out="$(run_hook "$gamma_dir" "$test_home")"
check_empty 'a project with no interrupted session is silent' "$out"

# --- scenario 4: a project directory with nothing recorded at all is silent ---
delta_dir="$scratch/projects/delta"
mkdir -p "$delta_dir"

out="$(run_hook "$delta_dir" "$test_home")"
check_empty 'a project with no sessions at all is silent' "$out"

# --- scenario 5: a project name contained in another is not a match ---
# Review finding: --project was a substring match, so zeta showed xzeta's card.
xzeta_dir="$scratch/projects/xzeta"
zeta_dir="$scratch/projects/zeta"
mkdir -p "$xzeta_dir" "$zeta_dir"
write_transcript "$transcripts/xzeta.jsonl" "44444444-4444-4444-4444-444444444444" "$xzeta_dir" "$(hours_ago 3)" 1
out="$(run_raw "$(payload_with "$zeta_dir" startup "")" "$test_home")"
check_fail_open 'another project containing the name'
check_empty 'another project containing the name is not shown' "$out"

# --- scenario 6: only a fresh start orients the session ---
# Review finding: SessionStart also fires on resume, clear and compact, which
# re-injected the project prompt mid-session.
for source in resume clear compact; do
  out="$(run_raw "$(payload_with "$test_home" "$source" "")" "$test_home")"
  check_fail_open "home directory on $source"
  check_empty "home directory on $source is silent" "$out"
done
out="$(run_raw "$(payload_with "$test_home" startup "")" "$test_home")"
check_fail_open 'home directory on startup'
check_contains 'home directory on startup asks for a project' "$out" "Pick a project"

# --- scenario 7: the session being started is never offered to itself ---
out="$(run_raw "$(payload_with "$beta_dir" startup "$beta_id")" "$test_home")"
check_fail_open 'the current session id'
check_empty 'the current session is not offered to itself' "$out"

# --- scenario 8: a recent interruption is offered once the caller is excluded ---
# Review finding: the one-hour live window hid the common case.
epsilon_dir="$scratch/projects/epsilon"
mkdir -p "$epsilon_dir"
epsilon_id="55555555-5555-5555-5555-555555555555"
write_transcript "$transcripts/epsilon.jsonl" "$epsilon_id" "$epsilon_dir" "$(hours_ago 0.2)" 1
out="$(run_raw "$(payload_with "$epsilon_dir" startup "some-other-session")" "$test_home")"
check_fail_open 'a recent interruption'
check_contains 'a recent interruption is offered' "$out" "claude --resume $epsilon_id"

# --- scenario 9: the hook speaks only for Claude Code sessions ---
# Review finding: it scans ~/.claude/projects and prints claude --resume, so a
# Codex or Gemini session was told how to resume a Claude session.
for runtime_dir in .codex .gemini; do
  mkdir -p "$scratch/$runtime_dir/hooks"
  cp "$HOOK" "$scratch/$runtime_dir/hooks/session-start.sh"
  out="$(run_raw "$(payload_with "$beta_dir" startup "")" "$test_home" "$scratch/$runtime_dir/hooks/session-start.sh")"
  check_fail_open "a $runtime_dir session"
  check_empty "a $runtime_dir session is silent" "$out"
done

# --- scenario 10: malformed payloads fail open and silent ---
for bad in 'not json' '[1, 2]' '' '{"cwd": 7}'; do
  out="$(run_raw "$bad" "$test_home")"
  check_fail_open "payload $bad"
  check_empty "payload $bad is silent" "$out"
done

# --- scenario 11: without a work item, no join line is ever written ---
if [ ! -e "$join_file" ]; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  printf 'session-start.test: a session without a work item wrote a join line: %s\n' "$(cat "$join_file")" >&2
fi

# join_count <session_id> <source> <runtime>: join lines matching all three.
join_count() {
  [ -f "$join_file" ] || { printf '0'; return 0; }
  python3 -c '
import json, sys
n = 0
for line in open(sys.argv[1]):
    row = json.loads(line)
    if (row.get("session_id"), row.get("source"), row.get("runtime")) == tuple(sys.argv[2:5]) and row.get("work_item") == "WI-30":
        n += 1
print(n)
' "$join_file" "$1" "$2" "$3"
}

check_equal() {
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'session-start.test: %s: expected %s, got %s\n' "$1" "$3" "$2" >&2
  fi
}

# --- scenario 12: a work-item session is joined and told its goals and limits ---
wi_dir="$scratch/session-wi"
mkdir -p "$wi_dir"
printf '%s' '{"work_item": "WI-30", "project": "llm-root", "runtime": "claude", "goals": ["Ship the tool-call budget hook", "Keep the counts honest"]}' >"$wi_dir/loadout.json"
printf '%s' '{"work_item": "WI-30", "launch_id": "launch-12", "runtime": "claude", "effective": {"tool_calls.total": 400, "tool_calls.per_tool.Bash": 150, "warn_at": 0.8}, "levels": [], "errors": []}' >"$wi_dir/budget.json"
export LLM_ROOT_WORK_ITEM="WI-30" LLM_ROOT_LAUNCH_ID="launch-12" LLM_ROOT_SESSION_DIR="$wi_dir" LLM_ROOT_BUDGET_FILE="$wi_dir/budget.json"

wi_id="66666666-6666-6666-6666-666666666666"
out="$(run_raw "$(payload_with "$delta_dir" startup "$wi_id")" "$test_home")"
check_fail_open 'a work-item session'
check_contains 'a work-item session names the work item' "$out" "WI-30"
check_contains 'a work-item session lists its first goal' "$out" "Ship the tool-call budget hook"
check_contains 'a work-item session lists its second goal' "$out" "Keep the counts honest"
check_contains 'a work-item session shows its total limit' "$out" "tool_calls.total: 400"
check_contains 'a work-item session shows its per-tool limit' "$out" "tool_calls.per_tool.Bash: 150"
check_equal 'a work-item session is joined once' "$(join_count "$wi_id" session-start claude)" "1"
check_contains 'the join line carries the launch id' "$(cat "$join_file" 2>/dev/null || true)" '"launch_id": "launch-12"'

# The existing behavior still follows the work-item block.
out="$(run_raw "$(payload_with "$beta_dir" startup "another-wi-session")" "$test_home")"
check_fail_open 'a work-item session with an interrupted card'
check_contains 'a work-item session still names the work item' "$out" "WI-30"
check_contains 'a work-item session still offers the interrupted card' "$out" "claude --resume $beta_id"

# A resume continues the launch (the design record,
# B-08, S-14): it prints the work item and its limits again, and a resume
# that keeps its session id is not joined a second time.
out="$(run_raw "$(payload_with "$delta_dir" resume "$wi_id")" "$test_home")"
check_fail_open 'a resumed work-item session'
check_contains 'a resumed work-item session names the work item again' "$out" "Work item WI-30"
check_contains 'a resumed work-item session prints its limits again' "$out" "tool_calls.total: 400"
check_equal 'a resumed work-item session is not joined again' "$(join_count "$wi_id" session-start claude)" "1"
check_equal 'a resumed work-item session writes no resume join for a known id' "$(join_count "$wi_id" resume claude)" "0"
# A clear is not a start: it stays silent.
out="$(run_raw "$(payload_with "$delta_dir" clear "$wi_id")" "$test_home")"
check_empty 'a cleared work-item session is silent' "$out"

# No limits set: the baseline period is named instead.
printf '%s' '{"work_item": "WI-30", "effective": {}, "levels": [], "errors": []}' >"$wi_dir/budget.json"
out="$(run_raw "$(payload_with "$delta_dir" startup "wi-baseline")" "$test_home")"
check_fail_open 'a work-item session with no limits'
check_contains 'a work-item session with no limits names the baseline period' "$out" "unset during the baseline period"

# A Codex install acts on the work item too, and then stays silent as before.
mkdir -p "$scratch/.codex/hooks/lib"
cp "$HOOK" "$scratch/.codex/hooks/session-start.sh"
cp "$DIR/lib/tool-budget.py" "$scratch/.codex/hooks/lib/tool-budget.py"
out="$(run_raw "$(payload_with "$beta_dir" startup "wi-codex")" "$test_home" "$scratch/.codex/hooks/session-start.sh")"
check_fail_open 'a work-item session under .codex'
check_contains 'a work-item session under .codex names the work item' "$out" "WI-30"
case "$out" in
  *"claude --resume"*) fail=$((fail + 1)); printf 'session-start.test: .codex work-item session offered a Claude card\n' >&2 ;;
  *) pass=$((pass + 1)) ;;
esac
check_equal 'a work-item session under .codex is joined as codex' "$(join_count wi-codex session-start codex)" "1"

# Fail open: a malformed payload and a missing session directory still exit 0.
out="$(run_raw 'not json' "$test_home")"
check_fail_open 'a work-item session with a malformed payload'
LLM_ROOT_SESSION_DIR="$scratch/no-such-dir" LLM_ROOT_BUDGET_FILE="$scratch/no-such-dir/budget.json"
out="$(run_raw "$(payload_with "$delta_dir" startup "wi-missing")" "$test_home")"
check_fail_open 'a work-item session with no session directory'
check_contains 'a work-item session with no session directory still names the work item' "$out" "WI-30"
unset LLM_ROOT_WORK_ITEM LLM_ROOT_LAUNCH_ID LLM_ROOT_SESSION_DIR LLM_ROOT_BUDGET_FILE

# --- scenario 13: a launched session in the home directory is not told to pick a project ---
# S-13: the
# work item already names the project, so the home prompt contradicted it.
printf '%s' '{"work_item": "WI-30", "effective": {"tool_calls.total": 400}, "levels": [], "errors": []}' >"$wi_dir/budget.json"
export LLM_ROOT_WORK_ITEM="WI-30" LLM_ROOT_LAUNCH_ID="launch-13" LLM_ROOT_SESSION_DIR="$wi_dir" LLM_ROOT_BUDGET_FILE="$wi_dir/budget.json"
out="$(run_raw "$(payload_with "$test_home" startup "wi-home")" "$test_home")"
check_fail_open 'a work-item session in the home directory'
check_contains 'a work-item session in the home directory names the work item' "$out" "WI-30"
check_not_contains 'a work-item session in the home directory is not told to pick a project' "$out" "Pick a project"
check_not_contains 'a work-item session in the home directory prints no roster' "$out" "${alpha_id:0:8}"
unset LLM_ROOT_WORK_ITEM LLM_ROOT_LAUNCH_ID LLM_ROOT_SESSION_DIR LLM_ROOT_BUDGET_FILE

# --- scenario 14: a plain home session lists the open work items it can launch ---
# A fixture llm-root, so the list does not follow the live PLAN.md: its
# scripts are the real ones, its PLAN.md and loadouts are the fixture's.
mkdir -p "$scratch/llm-root/loadouts"
# Physical path: the launcher prints its resolved root in each command.
fixture_root="$(cd "$scratch/llm-root" && pwd -P)"
ln -s "$LLM_ROOT_UNDER_TEST/scripts" "$fixture_root/scripts"
for item in WI-90 WI-91 WI-92; do
  printf '{"work_item": "%s", "project": "global"}' "$item" >"$fixture_root/loadouts/$item.json"
done
printf '%s\n' '| item | build | status |' '|---|---|---|' '| WI-90 | loadouts/WI-90.json | QUEUED |' '| WI-91 | loadouts/WI-91.json | DONE |' '| WI-92 | loadouts/WI-92.json | REVIEW |' >"$fixture_root/PLAN.md"
run_root="$fixture_root"
out="$(run_raw "$(payload_with "$test_home" startup "")" "$test_home")"
check_fail_open 'a plain home session with open work items'
check_contains 'a plain home session still shows the roster' "$out" "${alpha_id:0:8}"
check_contains 'a plain home session lists an open work item' "$out" "WI-90"
check_contains 'a plain home session prints its launch command' "$out" "python3 $fixture_root/scripts/session-launch.py WI-92"
check_not_contains 'a plain home session leaves out a DONE work item' "$out" "WI-91"
check_contains 'a plain home session still asks for a project' "$out" "Pick a project"
unset run_root

# --- scenario 14b: without AGENTRC_SOURCE or LLM_ROOT the hook has no root ---
# A copy of the hook under <tree>/hooks/ with <tree>/scripts/ beside it does
# not derive that tree as its root: the location of the hook decides nothing.
mkdir -p "$fixture_root/hooks"
cp "$HOOK" "$fixture_root/hooks/session-start.sh"
printf '%s' "$(payload_with "$test_home" startup "")" >"$scratch/payload"
rc=0
out="$(env -u LLM_ROOT -u AGENTRC_SOURCE HOME="$test_home" SESSION_CARD_ROOT="$transcripts" COLUMNS=100 NO_COLOR=1 bash "$fixture_root/hooks/session-start.sh" <"$scratch/payload" 2>"$scratch/stderr")" || rc=$?
printf '%s' "$rc" >"$scratch/rc"
check_fail_open 'a hook run from its own tree without a source root'
check_empty 'a hook run from its own tree without a source root stays silent' "$out"

# --- scenario 15: an interactive session is told what the program is doing (O-06) ---
# the design record, O-06:
# one line with the runs in flight, the pull requests awaiting review and the
# items waiting on a decision, counted by hooks/lib/program-summary.py from
# program-status.py snapshot --json over this fixture event log.
events_file="$scratch/program-events.jsonl"
event_line() {
  SS_ITEM="$1" SS_EVENT="$2" SS_PR="${3:-}" python3 -c '
import datetime, json, os
print(json.dumps({
    "contract_version": "1",
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "run": "fixture-run", "lane": "autonomy", "item": os.environ["SS_ITEM"], "event": os.environ["SS_EVENT"],
    "pr": int(os.environ["SS_PR"]) if os.environ["SS_PR"] else None,
    "branch": None, "commit": None, "detail": "Fixture event.",
}))
'
}
{
  event_line WI-1 item_started
  event_line WI-2 item_started
  event_line WI-3 handoff_posted 12
  event_line WI-4 needs_decision
} >"$events_file"
export PROGRAM_EVENTS_FILE="$events_file" PROGRAM_SUMMARY_CACHE="$scratch/program-summary.json"
summary='Program: 2 runs in flight, 1 pull request awaiting your review, 1 item waiting on your decision.'

out="$(run_raw "$(payload_with "$delta_dir" startup "")" "$test_home")"
check_fail_open 'a project session with program activity'
check_equal 'a project session prints the program summary as its only line' "$out" "$summary"

out="$(run_raw "$(payload_with "$beta_dir" startup "summary-with-card")" "$test_home")"
check_fail_open 'a project session with program activity and a card'
check_contains 'the summary comes before the interrupted card' "$(printf '%s\n' "$out" | head -n 1)" "$summary"
check_contains 'the interrupted card still follows the summary' "$out" "claude --resume $beta_id"

out="$(run_raw "$(payload_with "$test_home" startup "")" "$test_home")"
check_fail_open 'a home session with program activity'
check_contains 'a home session prints the program summary' "$out" "$summary"
check_contains 'a home session still asks for a project' "$out" "Pick a project"

# A headless session without a work item (codex-deliver's claude -p rounds)
# is marked the same way and prints nothing either.
out="$(LLM_ROOT_HEADLESS=1 run_raw "$(payload_with "$delta_dir" startup "")" "$test_home")"
check_fail_open 'a headless session without a work item'
check_empty 'a headless session without a work item is silent' "$out"

# An interactive work-item session is a person too.
export LLM_ROOT_WORK_ITEM="WI-30" LLM_ROOT_LAUNCH_ID="launch-15" LLM_ROOT_SESSION_DIR="$wi_dir" LLM_ROOT_BUDGET_FILE="$wi_dir/budget.json"
out="$(run_raw "$(payload_with "$delta_dir" startup "wi-interactive-summary")" "$test_home")"
check_fail_open 'an interactive work-item session with program activity'
check_contains 'an interactive work-item session prints the program summary' "$out" "$summary"

# A headless run has nobody to tell: the launcher marks it and the line is left out.
export LLM_ROOT_HEADLESS=1
out="$(run_raw "$(payload_with "$delta_dir" startup "wi-headless-summary")" "$test_home")"
check_fail_open 'a headless session with program activity'
check_not_contains 'a headless session prints no program summary' "$out" "Program:"
check_contains 'a headless session still names its work item' "$out" "WI-30"
unset LLM_ROOT_HEADLESS LLM_ROOT_WORK_ITEM LLM_ROOT_LAUNCH_ID LLM_ROOT_SESSION_DIR LLM_ROOT_BUDGET_FILE

# Only a fresh start: a resume, clear or compact does not repeat it.
out="$(run_raw "$(payload_with "$delta_dir" resume "")" "$test_home")"
check_empty 'a resumed session does not repeat the program summary' "$out"

# An empty stream prints nothing.
: >"$events_file"
out="$(run_raw "$(payload_with "$delta_dir" startup "")" "$test_home")"
check_fail_open 'a project session with an empty event stream'
check_empty 'an empty event stream prints no summary' "$out"

# A snapshot that cannot answer in time is cut off and the session starts silently.
slow_root="$scratch/slow-root"
mkdir -p "$slow_root/scripts"
cp "$LLM_ROOT_UNDER_TEST/scripts/session-resume.py" "$slow_root/scripts/session-resume.py"
printf '%s\n' 'import time' 'time.sleep(30)' >"$slow_root/scripts/program-status.py"
event_line WI-1 item_started >"$events_file"
run_root="$slow_root"
started=$SECONDS
out="$(PROGRAM_SUMMARY_TIMEOUT=1 run_raw "$(payload_with "$delta_dir" startup "")" "$test_home")"
elapsed=$((SECONDS - started))
unset run_root
check_fail_open 'a session whose snapshot times out'
check_not_contains 'a timed out snapshot prints no summary' "$out" "Program:"
if [ "$elapsed" -lt 10 ]; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  printf 'session-start.test: a timed out snapshot held the session start for %s seconds\n' "$elapsed" >&2
fi
unset PROGRAM_EVENTS_FILE PROGRAM_SUMMARY_CACHE

# --- disabled guard: RUNTIME_HOOKS_DISABLE silences the hook entirely ---
out="$(printf '%s' "$(payload_for "$beta_dir")" | HOME="$test_home" LLM_ROOT="$LLM_ROOT_UNDER_TEST" SESSION_CARD_ROOT="$transcripts" RUNTIME_HOOKS_DISABLE=1 bash "$HOOK" || true)"
check_empty 'RUNTIME_HOOKS_DISABLE silences the hook' "$out"

printf 'session-start.test: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
