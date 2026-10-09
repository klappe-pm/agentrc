#!/usr/bin/env bash
# Behavioral tests for agent-graph-session-start.sh
#
# The launcher runs agent-graph's session-start wiring when the checkout is present
# and is silent when it is not, and it keeps the indexed project list that
# agent-graph-pre-edit.sh reads fresh with one detached refresh an hour. The
# wiring script and the CLI are stubs here, so no case reaches the real graph.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)"
SCRIPT="$HERE/agent-graph-session-start.sh"

PASS=0
FAIL=0

WORK="$(mktemp -d "${TMPDIR:-/tmp}/agent-graph-session-start-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

ok() { PASS=$((PASS + 1)); }
bad() {
  FAIL=$((FAIL + 1))
  printf '  agent-graph-session-start.sh FAIL  %s\n' "$1" >&2
  [ -n "${2:-}" ] && printf '      %s\n' "$2" >&2
  return 0
}

FHOME="$WORK/home"
CHECKOUT="$WORK/agent-graph"
CACHE_HOME="$WORK/cache"
CACHE="$CACHE_HOME/agent-graph/indexed-repos"
LOCK="$CACHE.lock"
BIN="$WORK/bin/agent-graph"
BIN_CALLS="$WORK/bin-calls"
STDIN_SEEN="$WORK/stdin-seen"
mkdir -p "$FHOME" "$CHECKOUT/wiring/hooks" "$WORK/bin"

CONTEXT='{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"agent-graph is available: 9 nodes indexed."}}'
printf '#!/bin/bash\ncat > '"'%s'"'\nprintf '"'"'%%s\\n'"'"' '"'%s'"'\n' "$STDIN_SEEN" "$CONTEXT" > "$CHECKOUT/wiring/hooks/session-start.sh"

LIST='{"empty": false, "meta": {"kind": "Project"}, "query": "list-nodes", "rows": [{"id": "project:zeta", "kind": "Project"}, {"id": "project:alpha", "kind": "Project"}, {"id": "project:0-example", "kind": "Project"}], "shown": 3, "total": 3, "truncated": 0}'

# set_bin <body>: the stub CLI, which records every call.
set_bin() {
  printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s"\n%s\n' "$BIN_CALLS" "$1" > "$BIN"
  chmod +x "$BIN"
}
set_bin "printf '%s' '$LIST'"

# run [env pairs...]: stdout of the launcher; a non-zero exit is a failure.
run() {
  local out rc=0
  out="$(printf '%s' '{"hook_event_name":"SessionStart","source":"startup"}' | env HOME="$FHOME" AGENT_GRAPH_ROOT="$CHECKOUT" AGENT_GRAPH_BIN="$BIN" XDG_CACHE_HOME="$CACHE_HOME" "$@" bash "$SCRIPT" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 0 ] || bad "the launcher always exits 0" "got $rc"
  printf '%s' "$out"
}

wait_for() {
  local n=0
  while [ "$n" -lt 60 ]; do
    "$@" && return 0
    sleep 0.1
    n=$((n + 1))
  done
  return 1
}
cache_written() { [ -f "$CACHE" ]; }
lock_gone() { [ ! -e "$LOCK" ]; }
bin_called_once() { [ -f "$BIN_CALLS" ] && [ "$(wc -l < "$BIN_CALLS" | tr -d ' ')" = "1" ]; }

reset_state() { rm -rf "$CACHE_HOME" "$BIN_CALLS" "$STDIN_SEEN"; }

# --- the wiring runs and its output is passed through ---------------------------
reset_state
OUT="$(run)"
[ "$OUT" = "$CONTEXT" ] && ok || bad "the wiring output is printed" "got: $OUT"
grep -q "SessionStart" "$STDIN_SEEN" 2>/dev/null && ok || bad "the payload reaches the wiring script on stdin"

# --- the indexed project list ---------------------------------------------------
wait_for cache_written && ok || bad "a missing list is built in the background"
wait_for lock_gone && ok || bad "the refresh lock is released"
EXPECTED="$(printf '0-example\nalpha\nzeta\n')"
[ "$(cat "$CACHE" 2>/dev/null)" = "$EXPECTED" ] && ok || bad "the list holds one sorted slug per line" "got: $(cat "$CACHE" 2>/dev/null)"
bin_called_once && ok || bad "one refresh calls the CLI once" "calls: $(cat "$BIN_CALLS" 2>/dev/null)"
grep -q "list-nodes --kind Project --limit 10000" "$BIN_CALLS" 2>/dev/null && ok || bad "the refresh asks for every Project node" "calls: $(cat "$BIN_CALLS" 2>/dev/null)"
[ ! -e "$CACHE.tmp" ] && ok || bad "no temporary list is left behind"

# A list younger than an hour is not rebuilt.
rm -f "$BIN_CALLS"
OUT="$(run)"
sleep 0.4
[ ! -f "$BIN_CALLS" ] && ok || bad "a fresh list starts no refresh" "calls: $(cat "$BIN_CALLS")"

# An old list is rebuilt.
touch -t 202001010000 "$CACHE"
OUT="$(run)"
wait_for bin_called_once && ok || bad "an old list starts a refresh"
wait_for lock_gone && ok || bad "the second refresh releases its lock"

# --- the refresh never breaks the hook -----------------------------------------
reset_state
set_bin "printf 'not json'"
OUT="$(run)"
[ "$OUT" = "$CONTEXT" ] && ok || bad "a CLI that prints garbage still lets the wiring print" "got: $OUT"
wait_for lock_gone && ok || bad "a failed refresh releases its lock"
[ ! -f "$CACHE" ] && ok || bad "a failed refresh writes no list"
[ ! -e "$CACHE.tmp" ] && ok || bad "a failed refresh leaves no temporary list"

reset_state
set_bin "exit 1"
OUT="$(run)"
[ "$OUT" = "$CONTEXT" ] && ok || bad "a failing CLI still lets the wiring print" "got: $OUT"
wait_for lock_gone && ok || bad "a CLI failure releases the lock"
[ ! -f "$CACHE" ] && ok || bad "a CLI failure writes no list"

reset_state
set_bin "printf '%s' '{\"empty\": true, \"rows\": []}'"
OUT="$(run)"
wait_for lock_gone && ok || bad "an empty answer releases the lock"
[ ! -f "$CACHE" ] && ok || bad "an empty answer does not replace the list with nothing"
set_bin "printf '%s' '$LIST'"

# A list that already exists survives a failed refresh.
reset_state
mkdir -p "$CACHE_HOME/agent-graph"
printf 'kept\n' > "$CACHE"
touch -t 202001010000 "$CACHE"
set_bin "exit 1"
OUT="$(run)"
wait_for lock_gone && ok || bad "the lock is released after a failed refresh of an old list"
[ "$(cat "$CACHE")" = "kept" ] && ok || bad "a failed refresh keeps the existing list"
set_bin "printf '%s' '$LIST'"

# --- the refresh lock -----------------------------------------------------------
reset_state
mkdir -p "$LOCK"
OUT="$(run)"
sleep 0.4
[ ! -f "$BIN_CALLS" ] && ok || bad "a lock held within five minutes blocks a second refresh"
[ -d "$LOCK" ] && ok || bad "a held lock is left for its owner"

touch -t 202001010000 "$LOCK"
OUT="$(run)"
wait_for cache_written && ok || bad "a lock older than five minutes is replaced and the refresh runs"
wait_for lock_gone && ok || bad "the replaced lock is released"

# --- missing pieces are silent ---------------------------------------------------
reset_state
OUT="$(run AGENT_GRAPH_ROOT="$WORK/missing-checkout")"
[ -z "$OUT" ] && ok || bad "a missing checkout prints nothing" "got: $OUT"
sleep 0.3
[ ! -f "$BIN_CALLS" ] && ok || bad "a missing checkout starts no refresh"

rm -f "$CHECKOUT/wiring/hooks/session-start.sh"
OUT="$(run)"
[ -z "$OUT" ] && ok || bad "a checkout without the wiring script prints nothing" "got: $OUT"
printf '#!/bin/bash\ncat >/dev/null\nprintf '"'"'%%s\\n'"'"' '"'%s'"'\n' "$CONTEXT" > "$CHECKOUT/wiring/hooks/session-start.sh"

reset_state
OUT="$(run AGENT_GRAPH_BIN="$WORK/bin/missing")"
[ "$OUT" = "$CONTEXT" ] && ok || bad "a missing binary still runs the wiring script" "got: $OUT"
sleep 0.3
[ ! -f "$CACHE" ] && ok || bad "a missing binary builds no list"

OUT="$(printf '%s' '{}' | env HOME="$WORK/empty-home" AGENT_GRAPH_ROOT="$CHECKOUT" XDG_CACHE_HOME="$CACHE_HOME" bash "$SCRIPT" 2>/dev/null)" && ok || bad "no binary anywhere still exits 0"
[ "$OUT" = "$CONTEXT" ] && ok || bad "no binary anywhere still runs the wiring script" "got: $OUT"

OUT="$(run RUNTIME_HOOKS_DISABLE=1)"
[ -z "$OUT" ] && ok || bad "RUNTIME_HOOKS_DISABLE silences the launcher" "got: $OUT"

printf 'agent-graph-session-start.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
