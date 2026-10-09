#!/usr/bin/env bash
# Behavioral tests for guard-log.sh
#
# The guard event log is the one new capture in the session provenance design.
# Its contract is that it records a firing with enough identity to join back to
# a session, and that it can never change the decision of the guard that called
# it. Both halves are tested here.

set -euo pipefail

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../agentrc/data/hooks/lib" && pwd)"

PASS=0
FAIL=0

WORK="$(mktemp -d "${TMPDIR:-/tmp}/guard-log-test-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

ok() { PASS=$((PASS + 1)); }
bad() {
  FAIL=$((FAIL + 1))
  printf 'FAIL: %s\n' "$1" >&2
  printf '      %s\n' "${2:-}" >&2
}

TODAY="$(date -u '+%Y-%m-%d')"

# field <dir> <key>: read one key from the single logged record
field() {
  python3 -c "
import json, pathlib, sys
p = pathlib.Path('$1/guard-events-$TODAY.jsonl')
if not p.is_file():
    print('<no-file>')
    sys.exit(0)
lines = [l for l in p.read_text().splitlines() if l.strip()]
if not lines:
    print('<empty>')
    sys.exit(0)
print(json.loads(lines[-1]).get('$2', '<missing>'))
" 2>/dev/null || printf '<error>'
}

count_records() {
  python3 -c "
import pathlib
p = pathlib.Path('$1/guard-events-$TODAY.jsonl')
print(len([l for l in p.read_text().splitlines() if l.strip()]) if p.is_file() else 0)
" 2>/dev/null || printf '0'
}

PAYLOAD='{"session_id":"sess-abc","tool_name":"Write","cwd":"/tmp/proj","tool_input":{"file_path":"/tmp/x.md"}}'

# --- a normal firing ----------------------------------------------------------
DIR1="$WORK/one"
(
  # shellcheck source=guard-log.sh
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR1" guard_log_event demo-guard demo-rule deny "a short label" "$PAYLOAD"
)

# regression: the environment assignments were attached to the left side of the
# pipe, so python3 never received them and every field came back empty.
[ "$(field "$DIR1" guard)" = "demo-guard" ] && ok || bad "guard name is recorded" "$(field "$DIR1" guard)"
[ "$(field "$DIR1" rule)" = "demo-rule" ] && ok || bad "rule is recorded" "$(field "$DIR1" rule)"
[ "$(field "$DIR1" decision)" = "deny" ] && ok || bad "decision is recorded" "$(field "$DIR1" decision)"
[ "$(field "$DIR1" detail)" = "a short label" ] && ok || bad "detail is recorded" "$(field "$DIR1" detail)"
[ "$(field "$DIR1" event)" = "guard-fired" ] && ok || bad "event name is recorded" "$(field "$DIR1" event)"

# the join keys are the reason this log exists
[ "$(field "$DIR1" session_id)" = "sess-abc" ] && ok || bad "session id joins to a transcript" "$(field "$DIR1" session_id)"
[ "$(field "$DIR1" tool_name)" = "Write" ] && ok || bad "tool name is recorded" "$(field "$DIR1" tool_name)"
[ "$(field "$DIR1" cwd)" = "/tmp/proj" ] && ok || bad "cwd is recorded" "$(field "$DIR1" cwd)"

# G-02: a deny record names the call it denied, so a counter that reserved a
# slot for that call at PreToolUse can release it. A payload without the key
# records an empty string rather than dropping the field.
DIR1B="$WORK/tool-use-id"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR1B" guard_log_event demo-guard demo-rule deny "label" '{"session_id":"s","tool_name":"Write","tool_use_id":"toolu_G02"}'
)
[ "$(field "$DIR1B" tool_use_id)" = "toolu_G02" ] && ok || bad "tool_use_id is recorded" "$(field "$DIR1B" tool_use_id)"
[ "$(field "$DIR1" tool_use_id)" = "" ] && ok || bad "tool_use_id is empty when the payload has none" "$(field "$DIR1" tool_use_id)"

TS="$(field "$DIR1" timestamp)"
case "$TS" in
  20*T*Z) ok ;;
  *) bad "timestamp is iso 8601 utc" "got '$TS'" ;;
esac

# --- appending, not overwriting -----------------------------------------------
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR1" guard_log_event demo-guard demo-rule deny "second firing" "$PAYLOAD"
)
[ "$(count_records "$DIR1")" = "2" ] && ok || bad "records append" "got $(count_records "$DIR1")"

# --- disabled ------------------------------------------------------------------
DIR2="$WORK/disabled"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR2" GUARD_LOG_DISABLE=1 guard_log_event g r deny "x" "$PAYLOAD"
)
[ "$(count_records "$DIR2")" = "0" ] && ok || bad "GUARD_LOG_DISABLE suppresses the record" "wrote anyway"

DIR3="$WORK/hooks-off"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR3" RUNTIME_HOOKS_DISABLE=1 guard_log_event g r deny "x" "$PAYLOAD"
)
[ "$(count_records "$DIR3")" = "0" ] && ok || bad "RUNTIME_HOOKS_DISABLE suppresses the record" "wrote anyway"

# --- fail open -----------------------------------------------------------------
# A logging failure must never change a guard's decision, so the function
# returns 0 for every input, including ones that cannot be parsed.
DIR4="$WORK/malformed"
rc=0
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR4" guard_log_event g r deny "x" 'this is not json'
) || rc=$?
[ "$rc" -eq 0 ] && ok || bad "a malformed payload does not fail the caller" "exit $rc"
[ "$(field "$DIR4" session_id)" = "" ] && ok || bad "a malformed payload yields an empty session id" "$(field "$DIR4" session_id)"
[ "$(field "$DIR4" guard)" = "g" ] && ok || bad "a malformed payload still records the guard" "$(field "$DIR4" guard)"

rc=0
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$WORK/empty-payload" guard_log_event g r deny "x" ""
) || rc=$?
[ "$rc" -eq 0 ] && ok || bad "an empty payload does not fail the caller" "exit $rc"

rc=0
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR=/proc/nonexistent/nope guard_log_event g r deny "x" "$PAYLOAD"
) || rc=$?
[ "$rc" -eq 0 ] && ok || bad "an unwritable directory does not fail the caller" "exit $rc"

# --- detail is bounded ---------------------------------------------------------
DIR5="$WORK/long"
LONG="$(python3 -c "print('x' * 500)")"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR5" GUARD_LOG_DETAIL_CAP=50 guard_log_event g r deny "$LONG" "$PAYLOAD"
)
DETAIL_LEN="$(python3 -c "print(len('''$(field "$DIR5" detail)'''))")"
if [ "$DETAIL_LEN" -le 50 ]; then
  ok
else
  bad "detail is capped" "length $DETAIL_LEN exceeds the cap"
fi

# --- runtime payload shapes: tool_name, session_id, capability, runtime ------
#
# WI-16/F-21. Verified against each runtime's own hook documentation (cited in
# guard-log.sh): every deployed runtime names the tool under a top-level
# "tool_name" key, so no shape here fails on tool_name specifically. Cursor is
# the one verified divergence, and it is on the join key: its payload names
# the session "conversation_id" instead of "session_id". That is the shape
# that fails on the code before this change and passes after it.

# Cursor's own session-id key. Fails before the fix: session_id comes back
# empty because only payload["session_id"] was read.
DIR_CURSOR="$WORK/cursor-shape"
CURSOR_PAYLOAD='{"conversation_id":"conv-1","tool_name":"Shell","cwd":"/tmp/proj","tool_input":{"command":"npm install"}}'
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_CURSOR" guard_log_event demo-guard demo-rule deny "x" "$CURSOR_PAYLOAD"
)
[ "$(field "$DIR_CURSOR" session_id)" = "conv-1" ] && ok || bad "session_id falls back to conversation_id (Cursor shape)" "$(field "$DIR_CURSOR" session_id)"
[ "$(field "$DIR_CURSOR" tool_name)" = "Shell" ] && ok || bad "tool_name reads directly on the Cursor shape" "$(field "$DIR_CURSOR" tool_name)"

# A payload that carries no top-level tool_name at all (the shape every
# existing prose-guard.test.sh case sends): tool_name stays empty, but
# tool_name_reason now explains why instead of leaving the field silently
# blank. Fails before the fix: the key does not exist yet.
DIR_NOTOOL="$WORK/no-tool-name"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_NOTOOL" guard_log_event demo-guard demo-rule deny "x" '{"tool_input":{"content":"x"}}'
)
[ "$(field "$DIR_NOTOOL" tool_name)" = "" ] && ok || bad "tool_name is empty with no top-level key" "$(field "$DIR_NOTOOL" tool_name)"
[ -n "$(field "$DIR_NOTOOL" tool_name_reason)" ] && [ "$(field "$DIR_NOTOOL" tool_name_reason)" != "<missing>" ] && ok || bad "tool_name_reason explains the empty tool_name" "$(field "$DIR_NOTOOL" tool_name_reason)"

# A payload that does carry tool_name: tool_name_reason is the empty string,
# not omitted, so every record has the same schema.
DIR_HASTOOL="$WORK/has-tool-name"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_HASTOOL" guard_log_event demo-guard demo-rule deny "x" "$PAYLOAD"
)
[ "$(field "$DIR_HASTOOL" tool_name_reason)" = "" ] && ok || bad "tool_name_reason is empty when tool_name is present" "$(field "$DIR_HASTOOL" tool_name_reason)"

# capability: a Task names the subagent_type, a Skill names the skill, a
# SlashCommand names the command. Fails before the fix: no such field exists.
DIR_TASK="$WORK/capability-task"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_TASK" guard_log_event subagent-cap-guard subagent-cap deny "x" '{"tool_name":"Task","session_id":"s1","tool_input":{"subagent_type":"reviewer"}}'
)
[ "$(field "$DIR_TASK" capability)" = "reviewer" ] && ok || bad "capability reads a Task's subagent_type" "$(field "$DIR_TASK" capability)"

# Claude Code 2.1.280 names the subagent tool "Agent", not "Task"
# (docs/runtimes/evidence/claude-code-2.1.280-subagent-pretooluse.json). Fails
# before the fix: only tool_name "Task" read subagent_type, so an Agent call
# denied by the cap guard was logged with no capability.
DIR_AGENT="$WORK/capability-agent"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_AGENT" guard_log_event subagent-cap-guard subagent-cap deny "x" '{"tool_name":"Agent","session_id":"s1","tool_input":{"description":"d","prompt":"p","subagent_type":"reviewer"}}'
)
[ "$(field "$DIR_AGENT" capability)" = "reviewer" ] && ok || bad "capability reads an Agent call's subagent_type" "$(field "$DIR_AGENT" capability)"

DIR_SKILL="$WORK/capability-skill"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_SKILL" guard_log_event demo-guard demo-rule deny "x" '{"tool_name":"Skill","session_id":"s1","tool_input":{"skill":"commit-and-pr"}}'
)
[ "$(field "$DIR_SKILL" capability)" = "commit-and-pr" ] && ok || bad "capability reads a Skill's name" "$(field "$DIR_SKILL" capability)"

DIR_CMD="$WORK/capability-command"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_CMD" guard_log_event demo-guard demo-rule deny "x" '{"tool_name":"SlashCommand","session_id":"s1","tool_input":{"command":"/commit"}}'
)
[ "$(field "$DIR_CMD" capability)" = "/commit" ] && ok || bad "capability reads a SlashCommand's command" "$(field "$DIR_CMD" capability)"

DIR_AGENTTYPE="$WORK/capability-agent-type"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_AGENTTYPE" guard_log_event demo-guard demo-rule deny "x" '{"tool_name":"Bash","session_id":"s1","agent_type":"code-reviewer"}'
)
[ "$(field "$DIR_AGENTTYPE" capability)" = "code-reviewer" ] && ok || bad "capability reads a top-level agent_type" "$(field "$DIR_AGENTTYPE" capability)"

DIR_NOCAP="$WORK/capability-absent"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_NOCAP" guard_log_event demo-guard demo-rule deny "x" "$PAYLOAD"
)
[ "$(field "$DIR_NOCAP" capability)" = "" ] && ok || bad "capability is the empty string, not missing, when unavailable" "$(field "$DIR_NOCAP" capability)"

# runtime: a caller (a test, or a future adapter) can name it directly.
DIR_RUNTIME="$WORK/runtime-override"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_RUNTIME" GUARD_LOG_RUNTIME="codex" guard_log_event demo-guard demo-rule deny "x" "$PAYLOAD"
)
[ "$(field "$DIR_RUNTIME" runtime)" = "codex" ] && ok || bad "GUARD_LOG_RUNTIME overrides the inferred runtime" "$(field "$DIR_RUNTIME" runtime)"

# Without an override, the key is still present (empty from the source tree,
# where no runtime directory appears in the path).
DIR_RUNTIME_DEFAULT="$WORK/runtime-default"
(
  source "$LIB/guard-log.sh"
  GUARD_LOG_DIR="$DIR_RUNTIME_DEFAULT" guard_log_event demo-guard demo-rule deny "x" "$PAYLOAD"
)
[ "$(field "$DIR_RUNTIME_DEFAULT" runtime)" != "<missing>" ] && ok || bad "runtime key is present even when uninferrable" "$(field "$DIR_RUNTIME_DEFAULT" runtime)"

printf 'guard-log: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
