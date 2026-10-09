#!/usr/bin/env bash
# Behavioral tests for session-statusline.py
#
# A status line runs on every frame of a live session, so its contract is
# narrow: print something useful, print nothing rather than an error, and never
# exit non-zero. Each case here is one way that contract can break.
#
# The payload shape follows the published statusLine schema: session_id,
# transcript_path, model.display_name, workspace.current_dir,
# workspace.git_worktree, cost.total_cost_usd, context_window.used_percentage.

set -euo pipefail

# A session launched from a work item exports this; the cases below set it
# themselves, so an inherited value must not leak into the unset cases.
unset LLM_ROOT_BUDGET_FILE

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/hooks/lib" && pwd)"
SCRIPT="$LIB/session-statusline.py"

PASS=0
FAIL=0

# An explicit XXXXXX template: BSD mktemp accepts "-t name" alone, GNU mktemp
# refuses it ("too few X's"), so the old form never ran on Linux.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/statusline-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# The program segment (O-06) reads program-summary.py's cache; every case
# below starts without one, and a refresh it asks for finds no checkout.
export PROGRAM_SUMMARY_CACHE="$WORK/program-summary.json" LLM_ROOT="$WORK/no-llm-root"
unset PROGRAM_EVENTS_FILE

# The agent-graph segment reads a cache under XDG_CACHE_HOME and may start a detached
# refresh from AGENT_GRAPH_ROOT; every case starts with neither, so no run here can
# reach the real checkout, the real cache or the real CLI.
export AGENT_GRAPH_ROOT="$WORK/no-agent-graph" XDG_CACHE_HOME="$WORK/cache"
unset AGENT_GRAPH_BIN

ok() { PASS=$((PASS + 1)); }
bad() {
  FAIL=$((FAIL + 1))
  printf 'FAIL: %s\n' "$1" >&2
  printf '      %s\n' "${2:-}" >&2
}

SID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TRANSCRIPT="$WORK/$SID.jsonl"
{
  printf '%s\n' '{"type":"user","uuid":"u1","sessionId":"'"$SID"'","cwd":"/tmp/demo","timestamp":"2026-09-18T10:00:00Z","message":{"role":"user","content":"do the work"}}'
  printf '%s\n' '{"type":"ai-title","aiTitle":"Statusline demo goal","sessionId":"'"$SID"'"}'
} > "$TRANSCRIPT"

payload_with_transcript() {
  printf '%s' '{"session_id":"'"$SID"'","transcript_path":"'"$TRANSCRIPT"'","model":{"display_name":"Opus"},"workspace":{"current_dir":"/tmp/demo-project","git_worktree":"main"},"cost":{"total_cost_usd":1.234},"context_window":{"used_percentage":42}}'
}

payload_minimal() {
  printf '%s' '{"session_id":"deadbeef-0000-0000-0000-000000000000","model":{"display_name":"Opus"},"workspace":{"current_dir":"/tmp/other"}}'
}

# run <payload> [env pairs...] -> stdout, and record a failure on non-zero exit
run_line() {
  local payload="$1"
  shift
  local out rc=0
  out="$(printf '%s' "$payload" | env "$@" python3 "$SCRIPT" 2>/dev/null)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    bad "exit status is always zero" "got $rc"
  fi
  printf '%s' "$out"
}

contains() {
  local label="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*) ok ;;
    *) bad "$label" "expected '$needle' in: $haystack" ;;
  esac
}

lacks() {
  local label="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*) bad "$label" "did not expect '$needle' in: $haystack" ;;
    *) ok ;;
  esac
}

# --- the full render ----------------------------------------------------------
OUT="$(run_line "$(payload_with_transcript)")"
contains "project name is shown" "$OUT" "demo-project"
contains "git worktree is shown" "$OUT" "main"
contains "model is shown" "$OUT" "Opus"
contains "context percentage is shown" "$OUT" "42% ctx"
contains "cost is shown" "$OUT" '$1.23'
contains "short session id is shown" "$OUT" "aaaaaaaa"
contains "the goal is shown" "$OUT" "Statusline demo goal"

LINES="$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')"
if [ "$LINES" = "2" ]; then
  ok
else
  bad "identity and goal render as two lines" "got $LINES lines"
fi

# the full session id would crowd out everything else
lacks "the full session id is not shown" "$OUT" "$SID"

# --- degrading ----------------------------------------------------------------
# No transcript path: the identity line is still useful and must survive.
OUT="$(run_line "$(payload_minimal)")"
contains "identity survives without a transcript" "$OUT" "other"
contains "model survives without a transcript" "$OUT" "Opus"
LINES="$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')"
[ "$LINES" = "1" ] && ok || bad "no goal line without a transcript" "got $LINES lines"

# A transcript path that does not exist is not an error.
OUT="$(run_line '{"session_id":"x1234567","transcript_path":"/nope/missing.jsonl","model":{"display_name":"Opus"},"workspace":{"current_dir":"/tmp/p"}}')"
contains "a missing transcript degrades to identity" "$OUT" "Opus"

# Cost of zero is noise on a fresh session, so it is left out.
OUT="$(run_line '{"session_id":"x1234567","model":{"display_name":"Opus"},"workspace":{"current_dir":"/tmp/p"},"cost":{"total_cost_usd":0}}')"
lacks "a zero cost is omitted" "$OUT" '$0.00'

# --- malformed input ----------------------------------------------------------
for payload in 'not json at all' '' '[]' 'null' '{'; do
  OUT="$(run_line "$payload")"
  if [ -z "$OUT" ]; then
    ok
  else
    bad "malformed payload prints nothing" "payload '$payload' produced: $OUT"
  fi
done

# --- switches -----------------------------------------------------------------
OUT="$(run_line "$(payload_with_transcript)" RUNTIME_HOOKS_DISABLE=1)"
[ -z "$OUT" ] && ok || bad "RUNTIME_HOOKS_DISABLE silences the line" "got: $OUT"

OUT="$(run_line "$(payload_with_transcript)" SESSION_STATUSLINE_DISABLE=1)"
[ -z "$OUT" ] && ok || bad "SESSION_STATUSLINE_DISABLE silences the line" "got: $OUT"

OUT="$(run_line "$(payload_with_transcript)" SESSION_STATUSLINE_NO_GOAL=1)"
contains "NO_GOAL keeps the identity line" "$OUT" "demo-project"
lacks "NO_GOAL drops the goal line" "$OUT" "Statusline demo goal"

OUT="$(run_line "$(payload_with_transcript)" SESSION_STATUSLINE_GOAL_MODE=clean)"
contains "clean goal mode uses the prompt" "$OUT" "Do the work"

# session_name, when set, is friendlier than an id
OUT="$(run_line '{"session_id":"x1234567","session_name":"my-session","model":{"display_name":"Opus"},"workspace":{"current_dir":"/tmp/p"}}')"
contains "session name replaces the id when present" "$OUT" "my-session"

# --- budget segment -----------------------------------------------------------
# Unset, the output is byte for byte what it was before the segment existed.
BASE="$(run_line "$(payload_with_transcript)")"
OUT="$(run_line "$(payload_with_transcript)" TOOL_BUDGET_STATE_DIR="$WORK/state")"
[ "$OUT" = "$BASE" ] && ok || bad "no budget file leaves the line unchanged" "got: $OUT"
lacks "no budget segment without a budget file" "$OUT" "tools "

mkdir -p "$WORK/state"
BUDGET="$WORK/budget.json"
printf '%s' '{"work_item":"WI-30","launch_id":"L1","runtime":"claude","effective":{"tool_calls.total":10,"warn_at":0.8},"levels":[],"errors":[],"loaded":{}}' > "$BUDGET"
state_total() {
  printf '%s' '{"session_id":"'"$SID"'","work_item":"WI-30","launch_id":"L1","runtime":"claude","total":'"$1"',"per_tool":{"Bash":'"$1"'},"denied":0,"warned":[],"updated":"2026-09-22T10:00:00Z"}' > "$WORK/state/$SID.json"
}

state_total 3
OUT="$(run_line "$(payload_with_transcript)" LLM_ROOT_BUDGET_FILE="$BUDGET" TOOL_BUDGET_STATE_DIR="$WORK/state")"
contains "budget segment names the work item and calls against the limit" "$OUT" "WI-30 · tools 3/10"
lacks "no mark under the warning threshold" "$OUT" "!"
contains "identity line keeps its fields beside the segment" "$OUT" "42% ctx"
contains "goal line survives the segment" "$OUT" "Statusline demo goal"
LINES="$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')"
[ "$LINES" = "2" ] && ok || bad "the segment adds no line" "got $LINES lines"

state_total 8
OUT="$(run_line "$(payload_with_transcript)" LLM_ROOT_BUDGET_FILE="$BUDGET" TOOL_BUDGET_STATE_DIR="$WORK/state")"
contains "warn state is marked with one !" "$OUT" "! WI-30 · tools 8/10"
lacks "warn state is not marked as over" "$OUT" "!!"

state_total 11
OUT="$(run_line "$(payload_with_transcript)" LLM_ROOT_BUDGET_FILE="$BUDGET" TOOL_BUDGET_STATE_DIR="$WORK/state")"
contains "over state is marked with !!" "$OUT" "!! WI-30 · tools 11/10"

# No state file yet: the session has made no counted call.
rm -f "$WORK/state/$SID.json"
OUT="$(run_line "$(payload_with_transcript)" LLM_ROOT_BUDGET_FILE="$BUDGET" TOOL_BUDGET_STATE_DIR="$WORK/state")"
contains "no state file counts zero calls" "$OUT" "WI-30 · tools 0/10"

# No tool call limit: the count alone.
printf '%s' '{"work_item":"WI-31","effective":{}}' > "$BUDGET"
state_total 4
OUT="$(run_line "$(payload_with_transcript)" LLM_ROOT_BUDGET_FILE="$BUDGET" TOOL_BUDGET_STATE_DIR="$WORK/state")"
contains "without a limit the count stands alone" "$OUT" "WI-31 · tools 4"
lacks "without a limit there is no denominator" "$OUT" "tools 4/"

# A budget file that cannot be read degrades to the unchanged line.
OUT="$(run_line "$(payload_with_transcript)" LLM_ROOT_BUDGET_FILE="$WORK/missing.json" TOOL_BUDGET_STATE_DIR="$WORK/state")"
[ "$OUT" = "$BASE" ] && ok || bad "an unreadable budget file leaves the line unchanged" "got: $OUT"
printf '%s' 'not json' > "$BUDGET"
OUT="$(run_line "$(payload_with_transcript)" LLM_ROOT_BUDGET_FILE="$BUDGET" TOOL_BUDGET_STATE_DIR="$WORK/state")"
[ "$OUT" = "$BASE" ] && ok || bad "a malformed budget file leaves the line unchanged" "got: $OUT"

# --- program segment (O-06) ---------------------------------------------------
# The counts program-summary.py cached from program-status.py snapshot --json;
# the status line reads that cache and never runs the snapshot on a frame.
program_cache() {
  local age_minutes="$1" runs="$2" reviews="$3" decisions="$4"
  PS_AGE="$age_minutes" PS_RUNS="$runs" PS_REVIEWS="$reviews" PS_DECISIONS="$decisions" python3 -c '
import datetime, json, os
when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=float(os.environ["PS_AGE"]))
counts = {"runs": int(os.environ["PS_RUNS"]), "reviews": int(os.environ["PS_REVIEWS"]), "decisions": int(os.environ["PS_DECISIONS"])}
print(json.dumps({"generated_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "counts": counts}))
' > "$PROGRAM_SUMMARY_CACHE"
}

program_cache 1 3 1 2
OUT="$(run_line "$(payload_with_transcript)")"
contains "the program counts join the identity line" "$(printf '%s\n' "$OUT" | head -n 1)" "3 running · 1 to review · 2 to decide"
contains "the goal line survives the program segment" "$OUT" "Statusline demo goal"
LINES="$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')"
[ "$LINES" = "2" ] && ok || bad "the program segment adds no line" "got $LINES lines"

program_cache 1 0 0 0
OUT="$(run_line "$(payload_with_transcript)")"
[ "$OUT" = "$BASE" ] && ok || bad "all zero counts leave the line unchanged" "got: $OUT"

program_cache 120 3 1 2
OUT="$(run_line "$(payload_with_transcript)")"
[ "$OUT" = "$BASE" ] && ok || bad "counts older than an hour are not shown" "got: $OUT"

printf '%s' 'not json' > "$PROGRAM_SUMMARY_CACHE"
OUT="$(run_line "$(payload_with_transcript)")"
[ "$OUT" = "$BASE" ] && ok || bad "a malformed cache leaves the line unchanged" "got: $OUT"
rm -f "$PROGRAM_SUMMARY_CACHE" "$PROGRAM_SUMMARY_CACHE.lock"

# --- agent-graph segment ------------------------------------------------------
# The node count wiring/statusline.sh caches. The stub checkout below stands in for
# that script: it records each run and writes the cache the way the real one does.
AG="$WORK/agent-graph"
AG_CACHE="$WORK/cache/agent-graph/statusline-nodes"
AG_RUNS="$WORK/agent-graph-runs"
mkdir -p "$AG/wiring" "$WORK/cache/agent-graph"
cat > "$AG/wiring/statusline.sh" <<STUB
#!/bin/bash
printf 'ran\n' >> "$AG_RUNS"
printf '7' > "$AG_CACHE.tmp" && mv "$AG_CACHE.tmp" "$AG_CACHE"
STUB
chmod +x "$AG/wiring/statusline.sh"

first_line() { printf '%s\n' "$1" | head -n 1; }

wait_for_runs() {
  local want="$1" n=0
  while [ "$n" -lt 50 ]; do
    [ -f "$AG_RUNS" ] && [ "$(wc -l < "$AG_RUNS" | tr -d ' ')" -ge "$want" ] && return 0
    sleep 0.1
    n=$((n + 1))
  done
  return 1
}

# A missing checkout adds nothing, even beside a cache that holds a number.
printf '42' > "$AG_CACHE"
OUT="$(run_line "$(payload_with_transcript)")"
[ "$OUT" = "$BASE" ] && ok || bad "a missing checkout adds no segment" "got: $OUT"

# A fresh cache shows the count and starts nothing.
OUT="$(run_line "$(payload_with_transcript)" AGENT_GRAPH_ROOT="$AG")"
contains "the node count joins the identity line" "$(first_line "$OUT")" "agent-graph 42"
contains "the goal line survives the agent-graph segment" "$OUT" "Statusline demo goal"
LINES="$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')"
[ "$LINES" = "2" ] && ok || bad "the agent-graph segment adds no line" "got $LINES lines"
sleep 0.3
[ ! -f "$AG_RUNS" ] && ok || bad "a fresh cache starts no refresh" "runs: $(cat "$AG_RUNS")"

# A stale cache still shows the count and starts exactly one detached refresh a minute.
touch -t 202001010000 "$AG_CACHE"
OUT="$(run_line "$(payload_with_transcript)" AGENT_GRAPH_ROOT="$AG")"
contains "a stale cache still shows its count" "$(first_line "$OUT")" "agent-graph 42"
wait_for_runs 1 && ok || bad "a stale cache starts a refresh" "no run recorded"
touch -t 202001010000 "$AG_CACHE"
OUT="$(run_line "$(payload_with_transcript)" AGENT_GRAPH_ROOT="$AG")"
sleep 0.5
RUNS="$(wc -l < "$AG_RUNS" | tr -d ' ')"
[ "$RUNS" = "1" ] && ok || bad "refreshes are limited to one a minute" "got $RUNS runs"

# The refresh wrote the cache, so the next frame shows the new count and starts nothing.
OUT="$(run_line "$(payload_with_transcript)" AGENT_GRAPH_ROOT="$AG")"
contains "the refreshed count is shown" "$(first_line "$OUT")" "agent-graph 7"

# A missing cache shows nothing and starts a refresh; the frame never waits for it.
rm -f "$AG_CACHE" "$AG_CACHE.refresh" "$AG_RUNS"
OUT="$(run_line "$(payload_with_transcript)" AGENT_GRAPH_ROOT="$AG")"
[ "$OUT" = "$BASE" ] && ok || bad "a missing cache adds no segment" "got: $OUT"
wait_for_runs 1 && ok || bad "a missing cache starts a refresh" "no run recorded"
# The stub logs its run before it writes the cache; wait for the write so a late rename
# cannot replace the cache the next case sets up.
wait_for_cache() {
  local n=0
  while [ "$n" -lt 50 ]; do
    [ -f "$AG_CACHE" ] && return 0
    sleep 0.1
    n=$((n + 1))
  done
  return 1
}
wait_for_cache && ok || bad "the refresh writes the cache" "no cache written"

# A cache that is not a plain integer adds nothing.
printf 'agent-graph: offline' > "$AG_CACHE"
OUT="$(run_line "$(payload_with_transcript)" AGENT_GRAPH_ROOT="$AG")"
[ "$OUT" = "$BASE" ] && ok || bad "a non-integer cache adds no segment" "got: $OUT"
printf '12 nodes' > "$AG_CACHE"
OUT="$(run_line "$(payload_with_transcript)" AGENT_GRAPH_ROOT="$AG")"
[ "$OUT" = "$BASE" ] && ok || bad "an integer with trailing text adds no segment" "got: $OUT"

printf 'session-statusline: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
