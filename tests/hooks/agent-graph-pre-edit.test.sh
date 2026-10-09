#!/usr/bin/env bash
# Behavioral tests for agent-graph-pre-edit.sh
#
# The launcher runs on every Edit and Write in every project, so its contract is
# cost and safety, not features: print agent-graph's context only for an indexed
# project and only inside the time budget, and in every other case print nothing,
# exit 0 and never block the edit. The wiring script is a stub checkout here; the
# last case runs the real one when the agent-graph checkout carries it.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../stratarc/data/hooks" && pwd)"
SCRIPT="$HERE/agent-graph-pre-edit.sh"

PASS=0
FAIL=0
SKIP=0

WORK="$(mktemp -d "${TMPDIR:-/tmp}/agent-graph-pre-edit-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

ok() { PASS=$((PASS + 1)); }
bad() {
  FAIL=$((FAIL + 1))
  printf '  agent-graph-pre-edit.sh FAIL  %s\n' "$1" >&2
  [ -n "${2:-}" ] && printf '      %s\n' "$2" >&2
  return 0
}

FHOME="$WORK/home"
CHECKOUT="$WORK/agent-graph"
CACHE_HOME="$WORK/cache"
CACHE="$CACHE_HOME/agent-graph/indexed-repos"
BIN="$WORK/bin/agent-graph"
STDIN_SEEN="$WORK/stdin-seen"
RAN="$WORK/wiring-ran"
SLOW_DONE="$WORK/slow-done"
mkdir -p "$FHOME/projects/active" "$FHOME/projects/_worktrees" "$CHECKOUT/wiring/hooks" "$CACHE_HOME/agent-graph" "$WORK/bin"
printf '#!/bin/bash\nexit 0\n' > "$BIN"
chmod +x "$BIN"
printf 'alpha\nbeta\n' > "$CACHE"

CONTEXT='{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"agent-graph: 3 graph rows reference README.md."}}'

# set_wiring <body>: the stub pre-edit.sh the launcher is expected to run.
set_wiring() {
  printf '#!/bin/bash\n%s\n' "$1" > "$CHECKOUT/wiring/hooks/pre-edit.sh"
}
set_wiring "cat > '$STDIN_SEEN'; : >> '$RAN'; printf '%s\n' '$CONTEXT'"

payload() {
  printf '{"hook_event_name":"PreToolUse","tool_name":"Edit","cwd":"%s","tool_input":{"file_path":"%s"}}' "$(dirname "$1")" "$1"
}

# run <payload> [env pairs...]: stdout of the launcher; a non-zero exit is a failure.
run() {
  local input="$1" out rc=0
  shift
  out="$(printf '%s' "$input" | env -u LLM_ROOT_PROJECTS_DIR -u LLM_WORKTREE_ROOT HOME="$FHOME" AGENT_GRAPH_ROOT="$CHECKOUT" AGENT_GRAPH_BIN="$BIN" XDG_CACHE_HOME="$CACHE_HOME" "$@" bash "$SCRIPT" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 0 ] || bad "the launcher always exits 0" "got $rc"
  printf '%s' "$out"
}

reset_marks() { rm -f "$STDIN_SEEN" "$RAN" "$SLOW_DONE"; }

expect_silent() {
  local label="$1" out="$2"
  [ -z "$out" ] && ok || bad "$label" "printed: $out"
}

ALPHA="$FHOME/projects/active/alpha/src/main.py"

# --- the indexed project gets the context --------------------------------------
reset_marks
OUT="$(run "$(payload "$ALPHA")")"
[ "$OUT" = "$CONTEXT" ] && ok || bad "an indexed project prints the wiring output" "got: $OUT"
[ -f "$STDIN_SEEN" ] && ok || bad "the wiring script ran"
grep -q "alpha/src/main.py" "$STDIN_SEEN" 2>/dev/null && ok || bad "the payload reaches the wiring script on stdin"

reset_marks
OUT="$(run "$(payload "$FHOME/projects/_worktrees/beta/feature/x.py")")"
[ "$OUT" = "$CONTEXT" ] && ok || bad "a file under _worktrees is attributed to its project" "got: $OUT"

reset_marks
OUT="$(run "$(payload "$FHOME/projects/active/alpha/.worktrees/topic/a.py")")"
[ "$OUT" = "$CONTEXT" ] && ok || bad "a file in a repository's own worktree is attributed to the repository" "got: $OUT"

# --- every other case is silent and cheap ---------------------------------------
reset_marks
OUT="$(run "$(payload "$FHOME/projects/active/gamma/a.py")")"
expect_silent "a project the graph does not index prints nothing" "$OUT"
[ ! -f "$RAN" ] && ok || bad "a non-indexed project never runs the wiring script"

OUT="$(run "$(payload "/tmp/elsewhere/a.py")")"
expect_silent "a file outside any project prints nothing" "$OUT"
[ ! -f "$RAN" ] && ok || bad "a file outside any project never runs the wiring script"

OUT="$(run "$(payload "$ALPHA")" AGENT_GRAPH_ROOT="$WORK/missing-checkout")"
expect_silent "a missing checkout prints nothing" "$OUT"

rm -f "$CHECKOUT/wiring/hooks/pre-edit.sh"
OUT="$(run "$(payload "$ALPHA")")"
expect_silent "a checkout without the wiring script prints nothing" "$OUT"
set_wiring "cat > '$STDIN_SEEN'; : >> '$RAN'; printf '%s\n' '$CONTEXT'"

reset_marks
OUT="$(run "$(payload "$ALPHA")" XDG_CACHE_HOME="$WORK/no-cache")"
expect_silent "a missing indexed list prints nothing" "$OUT"
[ ! -f "$RAN" ] && ok || bad "a missing indexed list never runs the wiring script"

OUT="$(run "$(payload "$ALPHA")" AGENT_GRAPH_BIN="$WORK/bin/missing")"
expect_silent "a missing binary prints nothing" "$OUT"
[ ! -f "$RAN" ] && ok || bad "a missing binary never runs the wiring script"

OUT="$(printf '%s' "$(payload "$ALPHA")" | env -u LLM_ROOT_PROJECTS_DIR -u LLM_WORKTREE_ROOT HOME="$WORK/empty-home" AGENT_GRAPH_ROOT="$CHECKOUT" XDG_CACHE_HOME="$CACHE_HOME" bash "$SCRIPT" 2>/dev/null)" && ok || bad "no binary anywhere still exits 0"
expect_silent "no binary anywhere prints nothing" "$OUT"

OUT="$(run "$(payload "$ALPHA")" RUNTIME_HOOKS_DISABLE=1)"
expect_silent "RUNTIME_HOOKS_DISABLE silences the launcher" "$OUT"

for input in '' 'not json' '[]' 'null' '{' '{"tool_input":{}}' '{"tool_input":{"file_path":""}}' '{"tool_input":{"file_path":7}}'; do
  OUT="$(run "$input")"
  expect_silent "a malformed payload prints nothing: $input" "$OUT"
done

# --- fail open ------------------------------------------------------------------
reset_marks
set_wiring "cat >/dev/null; printf 'partial'; exit 3"
OUT="$(run "$(payload "$ALPHA")")"
expect_silent "a failing wiring script prints nothing" "$OUT"

set_wiring "cat >/dev/null; printf 'not json at all\n'"
OUT="$(run "$(payload "$ALPHA")")"
[ "$OUT" = "not json at all" ] && ok || bad "the launcher passes the wiring output through unchanged" "got: $OUT"

# --- the time budget -------------------------------------------------------------
# A wiring script that outlives the budget is killed and nothing is printed. It
# would finish at one second; the launcher must return well before that, and the
# script must not finish after it.
reset_marks
set_wiring "cat >/dev/null; sleep 1; : > '$SLOW_DONE'; printf '%s\n' '$CONTEXT'"
START="$(python3 -c 'import time; print(time.time())')"
OUT="$(run "$(payload "$ALPHA")" AGENT_GRAPH_PRE_EDIT_BUDGET_MS=300)"
ELAPSED_MS="$(python3 -c 'import sys, time; print(int((time.time() - float(sys.argv[1])) * 1000))' "$START")"
expect_silent "a wiring script past the budget prints nothing" "$OUT"
[ "$ELAPSED_MS" -lt 700 ] && ok || bad "the launcher returns near the budget" "took ${ELAPSED_MS} ms for a 300 ms budget"
sleep 1.3
[ ! -f "$SLOW_DONE" ] && ok || bad "a wiring script past the budget is killed"

# The same script inside a longer budget is let finish.
reset_marks
OUT="$(run "$(payload "$ALPHA")" AGENT_GRAPH_PRE_EDIT_BUDGET_MS=3000)"
[ "$OUT" = "$CONTEXT" ] && ok || bad "a longer budget lets a slower script finish" "got: $OUT"

# A budget that is not a number falls back to the default instead of failing.
set_wiring "cat >/dev/null; printf '%s\n' '$CONTEXT'"
OUT="$(run "$(payload "$ALPHA")" AGENT_GRAPH_PRE_EDIT_BUDGET_MS=soon)"
[ "$OUT" = "$CONTEXT" ] && ok || bad "a non-numeric budget uses the default" "got: $OUT"

# --- the real wiring --------------------------------------------------------------
# When the agent-graph checkout carries wiring/hooks/pre-edit.sh, run it end to end
# with a stub binary that answers `where-is` the way the CLI does.
REAL_WIRING="${AGENT_GRAPH_REAL_ROOT:-}/wiring/hooks/pre-edit.sh"
if [ -n "${AGENT_GRAPH_REAL_ROOT:-}" ] && [ -f "$REAL_WIRING" ]; then
  REAL_ROOT="$(dirname "$(dirname "$(dirname "$REAL_WIRING")")")"
  printf '#!/bin/bash\nprintf '"'"'{"empty": false, "meta": {}, "query": "where-is", "rows": [], "shown": 2, "total": 2, "truncated": 0}'"'"'\n' > "$BIN"
  OUT="$(run "$(payload "$FHOME/projects/active/alpha/docs/PLAN.md")" AGENT_GRAPH_ROOT="$REAL_ROOT" AGENT_GRAPH_PRE_EDIT_BUDGET_MS=3000)"
  # The wiring script encodes the JSON with Perl, whose key order is not fixed, so each
  # part is checked on its own.
  case "$OUT" in
    *'"hookEventName":"PreToolUse"'*) ok ;;
    *) bad "the real wiring script names the PreToolUse event" "got: $OUT" ;;
  esac
  case "$OUT" in
    *'2 graph rows reference'*) ok ;;
    *) bad "the real wiring script turns a where-is answer into context" "got: $OUT" ;;
  esac
else
  SKIP=$((SKIP + 1))
  printf '  agent-graph-pre-edit.sh skipped the real wiring case: set AGENT_GRAPH_REAL_ROOT to an agent-graph checkout\n'
fi

printf 'agent-graph-pre-edit.sh: %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
