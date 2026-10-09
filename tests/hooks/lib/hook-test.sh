#!/usr/bin/env bash
# Shared helpers for live hook sibling smoke tests.

set -euo pipefail

hook_test_disable_guard() {
  local hook_path="$1"
  local hook_name
  local tmp_home
  local output

  hook_name="$(basename "$hook_path")"
  tmp_home="$(mktemp -d "${TMPDIR:-/tmp}/${hook_name%.sh}-home-XXXXXX")"
  output="$(
    RUNTIME_HOOKS_DISABLE=1 HOME="$tmp_home" bash "$hook_path" </dev/null 2>&1
  )" || {
    local status=$?
    rm -rf "$tmp_home"
    printf '%s: expected disabled hook to exit 0, got %s\n' "$hook_name" "$status" >&2
    printf '%s\n' "$output" >&2
    return 1
  }
  rm -rf "$tmp_home"

  if [ -n "$output" ]; then
    printf '%s: expected disabled hook to be silent\n' "$hook_name" >&2
    printf '%s\n' "$output" >&2
    return 1
  fi
}

# hook_test_assert_decision <hook_path> <label> <cmd_string> <expected>
#
# Sends a PreToolUse(Bash) payload with cmd_string to the hook and asserts
# that the hook emits the expected permissionDecision. For expected=allow,
# a silent success is the preferred pass-through form.
#
# Increments HOOK_TEST_PASS / HOOK_TEST_FAIL in the caller's scope so the
# test driver can summarize at the end. Caller must initialize both vars
# to 0 before the first call.
hook_test_assert_decision() {
  local hook_path="$1"
  local label="$2"
  local cmd="$3"
  local expected="$4"
  local hook_name
  hook_name="$(basename "$hook_path")"

  local payload
  payload=$(printf '%s' "$cmd" | python3 -c \
    'import json,sys; print(json.dumps({"tool_input":{"command":sys.stdin.read()}}))')

  local output actual
  output=$(printf '%s' "$payload" \
    | RUNTIME_HOOKS_DISABLE=0 bash "$hook_path" 2>/dev/null) || true
  if [ -z "$output" ]; then
    actual="allow"
  else
    actual=$(printf '%s' "$output" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError:
    print("<error>")
    sys.exit(0)
print((d.get("hookSpecificOutput") or {}).get("permissionDecision") or "allow")
' 2>/dev/null) || actual="<error>"
  fi

  if [ "$actual" = "$expected" ]; then
    HOOK_TEST_PASS=$((HOOK_TEST_PASS + 1))
  else
    HOOK_TEST_FAIL=$((HOOK_TEST_FAIL + 1))
    printf '  %s FAIL  %-50s expected=%-5s got=%s\n' \
      "$hook_name" "$label" "$expected" "$actual" >&2
  fi
}

# hook_test_assert_file_warning <hook_path> <label> <file_path> <expected_substring>
#
# Sends a PreToolUse(Write/Edit) payload with file_path and asserts that the
# hook's stderr contains expected_substring. Used for advisory hooks that
# warn about file content (frontmatter-validator, command-skill-agent-validator,
# etc.). The hook is expected to exit 0 regardless.
#
# If expected_substring is the literal string "<silent>", asserts stderr is
# empty (the hook should pass silently for this input).
hook_test_assert_file_warning() {
  local hook_path="$1"
  local label="$2"
  local file_path="$3"
  local expected="$4"
  local hook_name
  hook_name="$(basename "$hook_path")"

  local payload
  payload=$(printf '%s' "$file_path" | python3 -c \
    'import json,sys; print(json.dumps({"tool_input":{"file_path":sys.stdin.read()}}))')

  local stderr_out
  stderr_out=$(printf '%s' "$payload" \
    | RUNTIME_HOOKS_DISABLE=0 bash "$hook_path" 2>&1 >/dev/null) || true

  local ok=0
  if [ "$expected" = "<silent>" ]; then
    [ -z "$stderr_out" ] && ok=1
  else
    case "$stderr_out" in *"$expected"*) ok=1 ;; esac
  fi

  if [ "$ok" -eq 1 ]; then
    HOOK_TEST_PASS=$((HOOK_TEST_PASS + 1))
  else
    HOOK_TEST_FAIL=$((HOOK_TEST_FAIL + 1))
    printf '  %s FAIL  %-50s expected=%q\n' \
      "$hook_name" "$label" "$expected" >&2
    printf '    got stderr: %q\n' "$stderr_out" >&2
  fi
}

# hook_test_summary <hook_name>
#
# Prints a one-line summary based on HOOK_TEST_PASS / HOOK_TEST_FAIL.
# Returns nonzero if any tests failed.
hook_test_summary() {
  local hook_name="$1"
  if [ "${HOOK_TEST_FAIL:-0}" -gt 0 ]; then
    printf '%s: FAIL (%d passed, %d failed)\n' \
      "$hook_name" "${HOOK_TEST_PASS:-0}" "$HOOK_TEST_FAIL" >&2
    return 1
  fi
  printf '%s: passed (%d cases)\n' "$hook_name" "${HOOK_TEST_PASS:-0}"
}
