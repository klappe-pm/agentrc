#!/usr/bin/env bash
# Behavioral tests for scripts/hook-lint.sh against fixture hooks directories.
#
# No test file exercised this linter before, even though it is one of the
# four gates in the verification contract. Each case builds a throwaway
# hooks/ directory under a temp root and runs the real hook-lint.sh with
# --hooks-dir pointed at it, so nothing here touches the repository's own
# hooks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
LINT="$SCRIPT_DIR/hook-lint.sh"

EMPTY_TESTS="$(mktemp -d "${TMPDIR:-/tmp}/hook-lint-tests-XXXXXX")"
trap 'rm -rf "$EMPTY_TESTS"' EXIT

PASS=0
FAIL=0

check_exit() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  hook-lint.test FAIL  %s: expected exit %s, got %s\n' "$label" "$expected" "$actual" >&2
  fi
}

check_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  hook-lint.test FAIL  %s: expected to find %q\n    in: %s\n' "$label" "$needle" "$haystack" >&2
  fi
}

check_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  hook-lint.test FAIL  %s: did not expect to find %q\n' "$label" "$needle" >&2
  fi
}

# run_lint ARGS... -- sets LINT_OUT and LINT_EXIT. hook-lint.sh routinely
# exits nonzero on purpose; capturing it under `set -e` needs the errexit
# suspended around the call, not a trailing `$?` that -e would never let run.
LINT_OUT=""
LINT_EXIT=0
run_lint() {
  set +e
  LINT_OUT="$(bash "$LINT" --tests-dir "${LINT_TESTS_DIR:-$EMPTY_TESTS}" "$@" 2>&1)"
  LINT_EXIT=$?
  set -e
}

# write_hook DIR NAME SHEBANG_OK SET_OK DISABLE_OK
write_hook() {
  local dir="$1" name="$2" shebang_ok="$3" set_ok="$4" disable_ok="$5"
  local shebang_line set_line disable_line
  [ "$shebang_ok" = "1" ] && shebang_line='#!/usr/bin/env bash' || shebang_line='#!/bin/bash'
  [ "$set_ok" = "1" ] && set_line='set -euo pipefail' || set_line=': no set flags here'
  [ "$disable_ok" = "1" ] && disable_line='[ "${RUNTIME_HOOKS_DISABLE:-}" = "1" ] && exit 0' || disable_line=': nothing to disable'
  printf '%s\n# fixture hook\n%s\n%s\nexit 0\n' "$shebang_line" "$set_line" "$disable_line" > "$dir/$name"
  chmod +x "$dir/$name"
}

TMP="$(mktemp -d -t hook-lint-test.XXXXXXXXXX)"
trap 'rm -rf "$TMP" "$EMPTY_TESTS"' EXIT

# --- Fixture A: a mix of one fully compliant hook, one bad shebang, one
# missing set flags, one missing RUNTIME_HOOKS_DISABLE, one missing its
# sibling test, and worktree-create.sh (exempt from the disable check, and
# covered by the shared worktree-lifecycle.test.sh sibling).
a="$TMP/fixture-a"
mkdir -p "$a"
write_hook "$a" good-hook.sh 1 1 1
printf 'stub\n' > "$a/good-hook.test.sh"
write_hook "$a" bad-shebang.sh 0 1 1
printf 'stub\n' > "$a/bad-shebang.test.sh"
write_hook "$a" no-set-flags.sh 1 0 1
printf 'stub\n' > "$a/no-set-flags.test.sh"
write_hook "$a" no-disable-var.sh 1 1 0
printf 'stub\n' > "$a/no-disable-var.test.sh"
write_hook "$a" no-sibling-test.sh 1 1 1
write_hook "$a" worktree-create.sh 1 1 0
printf 'stub\n' > "$a/worktree-lifecycle.test.sh"

run_lint --hooks-dir "$a"
check_exit "fixture A default" "1" "$LINT_EXIT"
check_contains "fixture A" "$LINT_OUT" "bad-shebang.sh: shebang must be"
check_contains "fixture A" "$LINT_OUT" "no-set-flags.sh: missing set safety flags"
check_contains "fixture A" "$LINT_OUT" "WARN  no-disable-var.sh: does not honor RUNTIME_HOOKS_DISABLE=1"
check_contains "fixture A" "$LINT_OUT" "WARN  no-sibling-test.sh: missing sibling test"
check_not_contains "fixture A: worktree-create.sh is exempt from the disable check" "$LINT_OUT" "worktree-create.sh: does not honor"
check_not_contains "fixture A: worktree-create.sh is covered by worktree-lifecycle.test.sh" "$LINT_OUT" "worktree-create.sh: missing sibling test"
check_not_contains "fixture A: the compliant hook is silent" "$LINT_OUT" "good-hook.sh:"
check_contains "fixture A summary" "$LINT_OUT" "hook-lint: 2 error(s), 2 warning(s)"

# --- Fixture B: warnings only, no hard errors, to isolate --strict's
# warning-to-error promotion from the unconditional error exit path.
b="$TMP/fixture-b"
mkdir -p "$b"
write_hook "$b" no-disable-var.sh 1 1 0
printf 'stub\n' > "$b/no-disable-var.test.sh"
write_hook "$b" no-sibling-test.sh 1 1 1

run_lint --hooks-dir "$b"
check_exit "fixture B default (warnings do not fail)" "0" "$LINT_EXIT"
check_contains "fixture B default" "$LINT_OUT" "WARN  no-disable-var.sh"
check_contains "fixture B default" "$LINT_OUT" "WARN  no-sibling-test.sh"

run_lint --hooks-dir "$b" --strict
check_exit "fixture B --strict (warnings promoted to errors)" "1" "$LINT_EXIT"
check_contains "fixture B strict" "$LINT_OUT" "ERROR no-disable-var.sh: does not honor RUNTIME_HOOKS_DISABLE=1"
check_contains "fixture B strict" "$LINT_OUT" "ERROR no-sibling-test.sh: missing sibling test"
check_not_contains "fixture B strict: no bare warnings remain" "$LINT_OUT" "WARN "

# --- Fixture C: worktree-remove.sh and worktree-validate.sh also map to the
# shared worktree-lifecycle.test.sh, and removing that shared sibling flags
# every one of the three by name, proving the mapping (not an accidental
# pass) is what silences fixture A.
c="$TMP/fixture-c"
mkdir -p "$c"
write_hook "$c" worktree-create.sh 1 1 0
write_hook "$c" worktree-remove.sh 1 1 1
write_hook "$c" worktree-validate.sh 1 1 1

run_lint --hooks-dir "$c"
check_contains "fixture C: worktree-create.sh flagged with no shared sibling" "$LINT_OUT" "worktree-create.sh: missing sibling test"
check_contains "fixture C: worktree-remove.sh flagged with no shared sibling" "$LINT_OUT" "worktree-remove.sh: missing sibling test"
check_contains "fixture C: worktree-validate.sh flagged with no shared sibling" "$LINT_OUT" "worktree-validate.sh: missing sibling test"

# --- A missing --hooks-dir is reported and treated as nothing to lint,
# exiting clean rather than failing the gate that pointed at it.
missing="$TMP/does-not-exist"
run_lint --hooks-dir "$missing"
check_exit "missing hooks dir" "0" "$LINT_EXIT"
check_contains "missing hooks dir message" "$LINT_OUT" "hook-lint: no hooks dir at $missing"

# --- A sibling test in --tests-dir silences the warning for that hook.
d="$TMP/fixture-d"
mkdir -p "$d" "$TMP/tests-d"
write_hook "$d" alpha.sh 1 1 1
printf '%s\n' '#!/usr/bin/env bash' > "$TMP/tests-d/alpha.test.sh"
LINT_TESTS_DIR="$TMP/tests-d" run_lint --hooks-dir "$d"
check_exit "tests-dir sibling" "0" "$LINT_EXIT"
check_contains "tests-dir sibling clean" "$LINT_OUT" "hook-lint: clean (0 warning(s))"

printf 'hook-lint.test: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
