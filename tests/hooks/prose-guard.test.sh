#!/usr/bin/env bash
# Behavioral tests for prose-guard.sh
#
# PreToolUse(Write|Edit|NotebookEdit) hook: hard-denies file writes whose
# authored content carries an em-dash (U+2014), horizontal bar (U+2015), or a
# connector en-dash, and denies writes to *.md files whose newly authored
# content hard-wraps a paragraph or list item. The forbidden glyphs are built
# from bytes so this test file carries no literal em-dash. Every case from the
# two guards this one replaced is kept, plus cases proving the two checks and
# their escape hatches are independent.

set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)/prose-guard.sh"

EMDASH=$'\xe2\x80\x94'   # U+2014
ENDASH=$'\xe2\x80\x93'   # U+2013
NL='\n'

PASS=0
FAIL=0

# WI-16/F-21: every deny case below fires the guard's real deny path, which
# calls guard_log_event. Without GUARD_LOG_DIR here, each firing appends to
# the operator's live ~/.agent-hooks/telemetry, carrying this test's synthetic
# tool_input-only payload (no top-level tool_name, session_id, or cwd) --
# which is exactly the shape the 2026-09-21 telemetry audit found polluting
# the live file (120 of 195 records that day, all from this file, all with
# empty tool_name, cwd, and session_id together). GLDIR isolates every call.
GLDIR="$(mktemp -d "${TMPDIR:-/tmp}/prose-guard-log-XXXXXX")"
REAL_HOME="${HOME:-}"
LIVE_FILE="$REAL_HOME/.agent-hooks/telemetry/guard-events-$(date -u '+%Y-%m-%d').jsonl"
LIVE_BEFORE=0
[ -n "$REAL_HOME" ] && [ -f "$LIVE_FILE" ] && LIVE_BEFORE="$(grep -c '' "$LIVE_FILE" 2>/dev/null || printf '0')"

# assert: feed a tool_input JSON and expect "deny" or "allow"; extra env pairs may follow
assert_guard() {
  local label="$1" json="$2" expect="$3"
  shift 3
  local out got
  out=$(printf '%s' "$json" | env RUNTIME_HOOKS_DISABLE=0 ALLOW_EMDASH=0 ALLOW_HARDWRAP=0 ALLOW_REDUNDANT_LABELS=0 GUARD_LOG_DIR="$GLDIR" "$@" bash "$SCRIPT" 2>/dev/null || true)
  case "$out" in *'"permissionDecision":"deny"'*) got=deny ;; *) got=allow ;; esac
  if [ "$got" = "$expect" ]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  prose-guard.sh FAIL  %-48s expected=%-5s got=%s\n' "$label" "$expect" "$got" >&2
  fi
}

# dash cases
assert_guard "em-dash in Write content"       "{\"tool_input\":{\"content\":\"slop ${EMDASH} here\"}}"  deny
assert_guard "em-dash in Edit new_string"      "{\"tool_input\":{\"new_string\":\"a ${EMDASH} b\"}}"      deny
assert_guard "em-dash in Notebook new_source"  "{\"tool_input\":{\"new_source\":\"x ${EMDASH} y\"}}"      deny
assert_guard "connector en-dash denied"        "{\"tool_input\":{\"content\":\"foo ${ENDASH} bar\"}}"     deny
assert_guard "numeric-range en-dash allowed"   "{\"tool_input\":{\"content\":\"pages 2${ENDASH}3\"}}"     allow
assert_guard "clean colon allowed"             "{\"tool_input\":{\"content\":\"clean: really\"}}"         allow
assert_guard "empty tool_input allowed"        "{\"tool_input\":{}}"                                      allow
assert_guard "em-dash in a non-md file denied" "{\"tool_input\":{\"file_path\":\"/tmp/foo.py\",\"content\":\"x ${EMDASH} y\"}}" deny

# hard-wrap cases
assert_guard "non-md file is out of scope for wrap" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.py\",\"content\":\"line one${NL}line two\"}}" \
  allow
assert_guard "clean single-line paragraph" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"This is one clean paragraph on one line.\"}}" \
  allow
assert_guard "hard-wrapped paragraph (Write content)" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"para line one${NL}para line two\"}}" \
  deny
assert_guard "hard-wrapped paragraph (Edit new_string)" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"new_string\":\"para line one${NL}para line two\"}}" \
  deny
assert_guard "separate list items are not a violation" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"- item one${NL}- item two\"}}" \
  allow
assert_guard "numbered list items are not a violation" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"1. Do X.${NL}2. Do Y.\"}}" \
  allow
assert_guard "hard-wrapped list item is a violation" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"- item one${NL}  continues here\"}}" \
  deny
assert_guard "table rows are not a violation" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"| A | B |${NL}|---|---|${NL}| 1 | 2 |\"}}" \
  allow
assert_guard "consecutive blockquote lines are not a violation" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"> line one${NL}> line two\"}}" \
  allow
assert_guard "heading followed by paragraph is not a violation" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"## Title${NL}Body text here.\"}}" \
  allow
assert_guard "content inside a fenced block is never scanned" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"\`\`\`${NL}line one of code${NL}line two of code${NL}\`\`\`\"}}" \
  allow
assert_guard "blank line separates two single-line paragraphs" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"paragraph one.${NL}${NL}paragraph two.\"}}" \
  allow

# An Edit whose new_string starts inside a fenced block: the opener lies in
# the file before old_string, so the guard must read the file for context.
CTX_DIR="$(mktemp -d "${TMPDIR:-/tmp}/prose-guard-XXXXXX")"
trap 'rm -rf "$CTX_DIR"' EXIT
printf '# doc\n\n```bash\ncmd one\nMARKER\n```\n' > "$CTX_DIR/open-fence.md"
printf '# doc\n\nMARKER\n' > "$CTX_DIR/no-fence.md"
assert_guard "edit inside an open fence is not prose" \
  "{\"tool_input\":{\"file_path\":\"$CTX_DIR/open-fence.md\",\"old_string\":\"MARKER\",\"new_string\":\"cmd two${NL}cmd three\"}}" \
  allow
assert_guard "same edit outside any fence is a violation" \
  "{\"tool_input\":{\"file_path\":\"$CTX_DIR/no-fence.md\",\"old_string\":\"MARKER\",\"new_string\":\"cmd two${NL}cmd three\"}}" \
  deny

# both violations in one write still deny
assert_guard "em-dash and hard wrap together" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"a ${EMDASH} b${NL}c\"}}" \
  deny

# escape hatches are independent of each other
assert_guard "ALLOW_EMDASH bypasses the dash check" \
  "{\"tool_input\":{\"content\":\"q ${EMDASH} r\"}}" \
  allow ALLOW_EMDASH=1
assert_guard "ALLOW_HARDWRAP bypasses the wrap check" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"a${NL}b\"}}" \
  allow ALLOW_HARDWRAP=1
assert_guard "ALLOW_EMDASH leaves the wrap check in force" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"a${NL}b\"}}" \
  deny ALLOW_EMDASH=1
assert_guard "ALLOW_HARDWRAP leaves the dash check in force" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"q ${EMDASH} r\"}}" \
  deny ALLOW_HARDWRAP=1

# redundant-label cases (the no-redundant-labels rule), *.md only
assert_guard "cell label repeating its header denied" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"| gap |${NL}|---|${NL}| Gap: thin record |\"}}" \
  deny
assert_guard "item opening with its heading noun denied" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"## gaps${NL}${NL}- Gap: thin record\"}}" \
  deny
assert_guard "plain cell and item allowed" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"## gaps${NL}${NL}- thin record${NL}${NL}| gap |${NL}|---|${NL}| thin record |\"}}" \
  allow
assert_guard "non-md file is out of scope for labels" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.py\",\"content\":\"## gaps${NL}${NL}- Gap: thin record\"}}" \
  allow
assert_guard "ALLOW_REDUNDANT_LABELS bypasses the label check" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"## gaps${NL}${NL}- Gap: thin record\"}}" \
  allow ALLOW_REDUNDANT_LABELS=1
assert_guard "ALLOW_REDUNDANT_LABELS leaves the wrap check in force" \
  "{\"tool_input\":{\"file_path\":\"/tmp/foo.md\",\"content\":\"a${NL}b\"}}" \
  deny ALLOW_REDUNDANT_LABELS=1

# the global disable switch skips everything
out=$(printf '%s' "{\"tool_input\":{\"content\":\"q ${EMDASH} r\"}}" | RUNTIME_HOOKS_DISABLE=1 GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
if [ -z "$out" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); printf '  prose-guard.sh FAIL  RUNTIME_HOOKS_DISABLE\n' >&2; fi

# The live telemetry directory gained no record across this whole run: every
# deny case above ran under GUARD_LOG_DIR, never the operator's real default.
LIVE_AFTER=0
[ -n "$REAL_HOME" ] && [ -f "$LIVE_FILE" ] && LIVE_AFTER="$(grep -c '' "$LIVE_FILE" 2>/dev/null || printf '0')"
if [ "$LIVE_AFTER" = "$LIVE_BEFORE" ]; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  printf '  prose-guard.sh FAIL  live telemetry directory gained a record (before=%s after=%s)\n' "$LIVE_BEFORE" "$LIVE_AFTER" >&2
fi
rm -rf "$GLDIR"

printf 'prose-guard.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
