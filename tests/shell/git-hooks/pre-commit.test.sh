#!/usr/bin/env bash
# The dispatcher must both pass clean staged content and actually deny staged
# content the sub-checks it calls (check-staged-prose.sh, check-staged-secrets.sh)
# would deny on their own. Each sub-check has its own behavioral test proving
# it denies in isolation; nothing proved the dispatcher forwards that denial
# instead of, say, being reduced to a bare `exit 0` that never calls either
# one. The forbidden glyph is built from a byte sequence so this file carries
# no literal em dash for the live write guard to trip on.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/git-hooks" && pwd)"
HOOK="$SCRIPT_DIR/pre-commit"

PASS=0
FAIL=0

# new_repo: creates a throwaway git repository under its own mktemp root and
# prints the repository path. Shared by both cases below so the init/config
# bootstrap is written once.
new_repo() {
  local temp
  temp="$(mktemp -d -t stratarc-pre-commit-test.XXXXXXXXXX)"
  git -C "$temp" init -q managed-project
  git -C "$temp/managed-project" config user.email t@t.test
  git -C "$temp/managed-project" config user.name test
  printf '%s\n' "$temp/managed-project"
}

# A clean staged file passes through the dispatcher.
clean_repo="$(new_repo)"
printf '# clean\n\nClean staged prose.\n' > "$clean_repo/clean.md"
git -C "$clean_repo" add clean.md
if (cd "$clean_repo" && "$HOOK"); then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  printf '  pre-commit.test FAIL  dispatcher rejected clean staged prose\n' >&2
fi
rm -rf "$(dirname "$clean_repo")"

# A staged forbidden em dash must be denied by the dispatcher itself, proving
# it actually calls check-staged-prose.sh rather than short-circuiting to a
# pass.
EMDASH=$'\xe2\x80\x94'
dirty_repo="$(new_repo)"
printf '# dirty\n\nA staged sentence with a forbidden dash%s here.\n' "$EMDASH" > "$dirty_repo/dirty.md"
git -C "$dirty_repo" add dirty.md
if (cd "$dirty_repo" && "$HOOK") 2>/dev/null; then
  FAIL=$((FAIL+1))
  printf '  pre-commit.test FAIL  dispatcher passed a staged em dash\n' >&2
else
  PASS=$((PASS+1))
fi
rm -rf "$(dirname "$dirty_repo")"

printf 'pre-commit.test: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
