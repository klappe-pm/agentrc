#!/usr/bin/env bash
# Behavioral tests for stratarc/data/git-hooks/commit-msg.
#
# strip-commit-attribution.test.sh exercises strip-commit-attribution.sh
# directly and never runs the commit-msg wrapper that forwards $1 to it. Two
# things were therefore unverified: that the wrapper actually forwards the
# message file path, and that it fails closed with its documented message
# when strip-commit-attribution.sh is missing or not executable. The agent
# name is assembled from fragments so this file carries no literal
# attribution line for the live guard to trip on.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/git-hooks" && pwd)"
HOOK="$SCRIPT_DIR/commit-msg"

AGENT="Cla""ude"
VENDOR="anthro""pic.com"

PASS=0
FAIL=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  commit-msg.test FAIL  %s\n    expected: %q\n    actual:   %q\n' \
      "$label" "$expected" "$actual" >&2
  fi
}

# The wrapper forwards $1 to strip-commit-attribution.sh, which strips the
# trailer in place. Run from inside this checkout so REPO_ROOT resolves to a
# tree where the sibling script actually exists.
msg="$(mktemp -t commit-msg-test.XXXXXXXXXX)"
trap 'rm -f "$msg"' EXIT
printf 'fix: forward the message path\n\nCo-Authored-By: %s <a@%s>\n' "$AGENT" "$VENDOR" > "$msg"
if "$HOOK" "$msg"; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  printf '  commit-msg.test FAIL  wrapper exited nonzero on a forwardable message\n' >&2
fi
check "strips the attribution trailer it forwarded" \
  "$(printf 'fix: forward the message path')" \
  "$(cat "$msg")"

# A clean message passes through untouched.
clean="$(mktemp -t commit-msg-test.XXXXXXXXXX)"
printf 'fix: a clean message\n\nNothing to strip here.\n' > "$clean"
before="$(cat "$clean")"
if ! "$HOOK" "$clean"; then
  FAIL=$((FAIL+1))
  printf '  commit-msg.test FAIL  wrapper rejected a clean message\n' >&2
else
  check "leaves a clean message untouched" "$before" "$(cat "$clean")"
fi
rm -f "$clean"

# When strip-commit-attribution.sh is not present beside the wrapper's own
# repository root, the wrapper fails closed with its documented message
# rather than silently letting an unjudged commit through. A throwaway git
# repository with no stratarc/data/git-hooks/ tree stands in for the missing
# sibling, isolated from this checkout's real script.
TEMP_ROOT="$(mktemp -d -t commit-msg-test-repo.XXXXXXXXXX)"
git init -q "$TEMP_ROOT"
missing_msg="$TEMP_ROOT/COMMIT_EDITMSG"
printf 'fix: x\n' > "$missing_msg"
if (cd "$TEMP_ROOT" && "$HOOK" "$missing_msg") 2>"$TEMP_ROOT/stderr"; then
  FAIL=$((FAIL+1))
  printf '  commit-msg.test FAIL  wrapper passed with no strip-commit-attribution.sh sibling\n' >&2
else
  PASS=$((PASS+1))
fi
if grep -q "commit-msg: required check is unavailable:" "$TEMP_ROOT/stderr"; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  printf '  commit-msg.test FAIL  missing-sibling stderr: %s\n' "$(cat "$TEMP_ROOT/stderr")" >&2
fi
rm -rf "$TEMP_ROOT"

printf 'commit-msg.test: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
