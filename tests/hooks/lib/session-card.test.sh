#!/usr/bin/env bash
# Behavioral tests for session-card.py
#
# Derivation layer for session resume cards. Every case below that is marked
# "regression" failed against real transcripts before the fix it guards, so a
# test that passes with the fix reverted is not doing its job.
#
# Fixtures are synthetic transcripts built from the record shapes observed in
# ~/.claude/projects: ai-title, cost-state, last-prompt, user, assistant, and
# the single-line bridge-session stub.

set -euo pipefail

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../agentrc/data/hooks/lib" && pwd)"
ENGINE="$LIB/session-card.py"

PASS=0
FAIL=0

WORK="$(mktemp -d "${TMPDIR:-/tmp}/session-card-test-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

ok() {
  PASS=$((PASS + 1))
}

bad() {
  FAIL=$((FAIL + 1))
  printf 'FAIL: %s\n' "$1" >&2
  printf '      %s\n' "${2:-}" >&2
}

# assert_field <label> <transcript> <jq-ish python key path> <expected>
assert_field() {
  local label="$1" transcript="$2" key="$3" expect="$4"
  shift 4
  local got
  got="$(python3 "$ENGINE" card "$transcript" --json "$@" 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$key'))" 2>/dev/null || true)"
  if [ "$got" = "$expect" ]; then
    ok
  else
    bad "$label" "expected '$expect', got '$got'"
  fi
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*) ok ;;
    *) bad "$label" "expected to contain '$needle', got '$haystack'" ;;
  esac
}

SID="11111111-2222-3333-4444-555555555555"

# --- a full session transcript ------------------------------------------------
FULL="$WORK/$SID.jsonl"
{
  printf '%s\n' '{"type":"user","uuid":"u1","sessionId":"'"$SID"'","cwd":"/tmp/demo-project","gitBranch":"main","timestamp":"2026-09-18T10:00:00Z","message":{"role":"user","content":"Build the thing I asked for"}}'
  # An injected skill body. isMeta marks it, and it must not become the goal.
  printf '%s\n' '{"type":"user","uuid":"u2","isMeta":true,"sessionId":"'"$SID"'","timestamp":"2026-09-18T10:00:01Z","message":{"role":"user","content":"# Some Skill\n\nDocumentation that is not a prompt."}}'
  # A tool result. Also a user record, also not a prompt.
  printf '%s\n' '{"type":"user","uuid":"u3","sessionId":"'"$SID"'","timestamp":"2026-09-18T10:00:02Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_x","content":"output"}]}}'
  printf '%s\n' '{"type":"assistant","uuid":"a1","sessionId":"'"$SID"'","timestamp":"2026-09-18T10:00:03Z","message":{"role":"assistant","model":"claude-opus-5","usage":{"input_tokens":10,"output_tokens":20},"content":[{"type":"text","text":"Here is where it stopped."}]}}'
  printf '%s\n' '{"type":"assistant","uuid":"a2","sessionId":"'"$SID"'","timestamp":"2026-09-18T10:00:04Z","message":{"role":"assistant","model":"<synthetic>","usage":{"input_tokens":1,"output_tokens":1},"content":[{"type":"text","text":"Synthetic filler."}]}}'
  printf '%s\n' '{"type":"ai-title","aiTitle":"Demo session title","sessionId":"'"$SID"'"}'
  printf '%s\n' '{"type":"last-prompt","lastPrompt":"the latest ask","leafUuid":"u1","sessionId":"'"$SID"'"}'
  printf '%s\n' '{"type":"cost-state","sessionId":"'"$SID"'","totalCostUSD":1.5,"totalDuration":1000,"totalLinesAdded":3,"totalLinesRemoved":1,"modelUsage":{"claude-opus-5":{"inputTokens":100,"outputTokens":200,"cacheReadInputTokens":300,"cacheCreationInputTokens":400,"thinkingTokens":42}}}'
} > "$FULL"

assert_field "goal is the ai-title in summary mode" "$FULL" goal "Demo session title"
assert_field "project comes from cwd" "$FULL" project "demo-project"
assert_field "branch is carried" "$FULL" branch "main"
assert_field "cost is parsed" "$FULL" cost_usd "1.5"
assert_field "cost_known is true when cost-state exists" "$FULL" cost_known "True"
# B-06: tokens come from the transcript's turns (10+20 and 1+1), never from
# cost-state's modelUsage (1000), so the card agrees with the budget report.
assert_field "tokens come from transcript turns, not cost-state" "$FULL" tokens "32"
assert_field "kind is session for a top level transcript" "$FULL" kind "session"
# cost-state has no timestamp of its own; the card dates it by the last
# timestamped record written before it.
assert_field "cost_as_of dates the last cost-state" "$FULL" cost_as_of "2026-09-18T10:00:04+00:00"

# Reasoning tokens are the only recorded proxy for effort. They are reported
# beside the output count rather than added to it, so the total must not move.
# These turns report none, so the count falls back to cost-state.
assert_field "thinking tokens fall back to cost-state" "$FULL" thinking_tokens "42"
assert_field "thinking tokens are not added to the total" "$FULL" tokens "32"
assert_field "last_prompt is carried" "$FULL" last_prompt "the latest ask"

# regression: tool results and isMeta records were counted as turns, so a six
# prompt session reported ninety three.
assert_field "turns counts only real prompts" "$FULL" turns "1"

# regression: an injected skill body became first_prompt, so clean and original
# goal modes described the skill rather than the request.
assert_field "first_prompt ignores injected content" "$FULL" first_prompt \
  "Build the thing I asked for"
assert_field "clean mode uses the real prompt" "$FULL" goal \
  "Build the thing I asked for" --goal-mode clean
assert_field "original mode is verbatim" "$FULL" goal \
  "Build the thing I asked for" --goal-mode original

# regression: the placeholder model id leaked into the model list.
MODELS="$(python3 "$ENGINE" card "$FULL" --json 2>/dev/null \
  | python3 -c "import json,sys;print(','.join(json.load(sys.stdin)['models']))")"
if [ "$MODELS" = "opus" ]; then
  ok
else
  bad "synthetic model id is filtered" "expected 'opus', got '$MODELS'"
fi

# the resume command must be complete enough to paste and run
RESUME="$(python3 "$ENGINE" card "$FULL" --json 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['resume_command'])")"
assert_contains "resume command carries the full session id" "$RESUME" "$SID"
assert_contains "resume command is directory scoped" "$RESUME" "cd /tmp/demo-project"

# --- json written by a standard encoder ---------------------------------------
# regression: the line prefilter matched '"type":"user"' literally, so records
# encoded with a space after the colon, which is what json.dumps produces, were
# skipped in silence and the card came back empty rather than wrong.
SPACED="$WORK/44444444-5555-6666-7777-888888888888.jsonl"
python3 -c "
import json, pathlib
sid = '44444444-5555-6666-7777-888888888888'
rows = [
  {'type':'user','uuid':'u1','sessionId':sid,'cwd':'/tmp/spaced-project','timestamp':'2026-09-18T10:00:00Z','message':{'role':'user','content':'Spaced encoding prompt'}},
  {'type':'ai-title','aiTitle':'Spaced encoding title','sessionId':sid},
]
pathlib.Path('$SPACED').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
"
assert_field "standard json encoding is parsed" "$SPACED" goal "Spaced encoding title"
assert_field "standard json encoding counts turns" "$SPACED" turns "1"
assert_field "standard json encoding finds the cwd" "$SPACED" project "spaced-project"

SPACED_GOAL="$(python3 "$ENGINE" goal "$SPACED" 2>/dev/null || true)"
if [ "$SPACED_GOAL" = "Spaced encoding title" ]; then
  ok
else
  bad "bounded read tolerates standard json encoding" "got '$SPACED_GOAL'"
fi

# --- a session with no cost-state ---------------------------------------------
NOCOST="$WORK/66666666-7777-8888-9999-000000000000.jsonl"
{
  printf '%s\n' '{"type":"user","uuid":"u1","sessionId":"66666666-7777-8888-9999-000000000000","cwd":"/tmp/demo-project","timestamp":"2026-09-18T10:00:00Z","message":{"role":"user","content":"No cost recorded here"}}'
  printf '%s\n' '{"type":"assistant","uuid":"a1","sessionId":"66666666-7777-8888-9999-000000000000","timestamp":"2026-09-18T10:00:01Z","message":{"role":"assistant","model":"claude-sonnet-5","usage":{"input_tokens":5,"output_tokens":7}}}'
} > "$NOCOST"

# regression: a missing cost-state rendered as $0.00, which reads as free.
assert_field "cost_known is false without cost-state" "$NOCOST" cost_known "False"
assert_field "tokens fall back to message usage" "$NOCOST" tokens "12"

# Without cost-state, reasoning tokens come from the per-record nesting, which
# is where the runtime puts them on an assistant message.
THINK="$WORK/aaaaaaaa-0000-0000-0000-000000000000.jsonl"
{
  printf '%s\n' '{"type":"user","uuid":"u1","sessionId":"aaaaaaaa-0000-0000-0000-000000000000","cwd":"/tmp/demo-project","timestamp":"2026-09-18T10:00:00Z","message":{"role":"user","content":"think about it"}}'
  printf '%s\n' '{"type":"assistant","uuid":"a1","sessionId":"aaaaaaaa-0000-0000-0000-000000000000","timestamp":"2026-09-18T10:00:01Z","message":{"role":"assistant","model":"claude-opus-5","usage":{"input_tokens":5,"output_tokens":7,"output_tokens_details":{"thinking_tokens":99}}}}'
} > "$THINK"
assert_field "thinking falls back to output_tokens_details" "$THINK" thinking_tokens "99"

# --- a dispatched agent transcript --------------------------------------------
AGENT_DIR="$WORK/$SID/subagents"
mkdir -p "$AGENT_DIR"
AGENT="$AGENT_DIR/agent-abc123.jsonl"
printf '%s\n' '{"type":"user","uuid":"s1","sessionId":"'"$SID"'","cwd":"/tmp/demo-project","timestamp":"2026-09-18T11:00:00Z","message":{"role":"user","content":"You are a dispatched agent briefing."}}' > "$AGENT"
printf '%s\n' '{"agentType":"general-purpose","description":"A dispatch","toolUseId":"toolu_join_me","spawnDepth":1,"model":"haiku"}' > "$AGENT_DIR/agent-abc123.meta.json"

# regression: a dispatched agent reported its parent's sessionId, so distinct
# agents rendered as the same session.
assert_field "agent identity is its own, not the parent's" "$AGENT" session_id "agent-abc123"
assert_field "agent records its parent" "$AGENT" parent_session_id "$SID"
assert_field "agent kind is agent" "$AGENT" kind "agent"
assert_field "agent carries the joining tool_use id" "$AGENT" tool_use_id "toolu_join_me"
assert_field "agent carries the requested model" "$AGENT" requested_model "haiku"

# scan excludes dispatched agents unless asked, so an agent never appears as a
# session beside its own parent
AGENTS_IN_SCAN="$(python3 "$ENGINE" scan --root "$WORK" --json 2>/dev/null \
  | python3 -c "import json,sys;print(sum(1 for c in json.load(sys.stdin) if c['kind']=='agent'))")"
if [ "$AGENTS_IN_SCAN" = "0" ]; then
  ok
else
  bad "scan excludes dispatched agents" "found $AGENTS_IN_SCAN agent cards in a session scan"
fi

SESSIONS_IN_SCAN="$(python3 "$ENGINE" scan --root "$WORK" --json 2>/dev/null \
  | python3 -c "import json,sys;print(sum(1 for c in json.load(sys.stdin) if c['kind']=='session'))")"
if [ "$SESSIONS_IN_SCAN" -ge 3 ]; then
  ok
else
  bad "scan returns the real sessions" "expected at least 3, got $SESSIONS_IN_SCAN"
fi

# --- a bridge-session stub ----------------------------------------------------
STUB="$WORK/99999999-0000-1111-2222-333333333333.jsonl"
printf '%s\n' '{"type":"bridge-session","sessionId":"99999999-0000-1111-2222-333333333333","lastSequenceNum":0}' > "$STUB"
if python3 "$ENGINE" card "$STUB" --json >/dev/null 2>&1; then
  ok
else
  bad "bridge-session stub does not crash" "card build exited non-zero"
fi

# --- the bounded goal read ----------------------------------------------------
# The status line uses this path, so it must agree with the full read.
GOAL="$(python3 "$ENGINE" goal "$FULL" 2>/dev/null || true)"
if [ "$GOAL" = "Demo session title" ]; then
  ok
else
  bad "bounded goal read agrees with the full read" "got '$GOAL'"
fi

GOAL_CLEAN="$(python3 "$ENGINE" goal "$FULL" --goal-mode clean 2>/dev/null || true)"
if [ "$GOAL_CLEAN" = "Build the thing I asked for" ]; then
  ok
else
  bad "bounded goal read honours clean mode" "got '$GOAL_CLEAN'"
fi

MISSING="$(python3 "$ENGINE" goal "$WORK/does-not-exist.jsonl" 2>/dev/null || true)"
if [ -z "$MISSING" ]; then
  ok
else
  bad "bounded goal read is silent on a missing file" "got '$MISSING'"
fi

# --- shouted prompts ----------------------------------------------------------
SHOUT="$(python3 -c "
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('sc', '$ENGINE')
m = importlib.util.module_from_spec(spec)
sys.modules['sc'] = m
spec.loader.exec_module(m)
print(m.clean_prompt('PLEASE BUILD THE THING NOW'))
" 2>/dev/null || true)"
if [ "$SHOUT" = "Build the thing now" ]; then
  ok
else
  bad "clean mode calms a shouted prompt" "got '$SHOUT'"
fi

printf 'session-card: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
