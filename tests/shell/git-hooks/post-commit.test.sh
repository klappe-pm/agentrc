#!/usr/bin/env bash
# Verify post-commit: the canonical checkout runs `stratarc sync`, a linked
# worktree never does, a failed synchronization is reported, and a missing
# stratarc command is skipped without failing the commit. The stratarc command is
# a stub that records its arguments and the source root it was given.
set -euo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/git-hooks" && pwd)/post-commit"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/post-commit-XXXXXX")"
trap 'rm -rf "$scratch"' EXIT

fail() {
  printf '%s\n' "post-commit.test: $1" >&2
  exit 1
}

stub_ok="$scratch/stratarc-ok"
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s|%s\n" "$*" "${STRATARC_SOURCE:-}" > deployed' > "$stub_ok"
stub_fail="$scratch/stratarc-fail"
printf '%s\n' '#!/usr/bin/env bash' 'exit 9' > "$stub_fail"
chmod 755 "$stub_ok" "$stub_fail"

new_repo() {
  git -C "$1" init -q -b main
  git -C "$1" config user.email test@example.com
  git -C "$1" config user.name test
  printf 'x\n' > "$1/file"
  printf 'deployed\n' > "$1/.gitignore"
  git -C "$1" add -A
  git -C "$1" -c core.hooksPath=/dev/null commit -qm fixture
}

# The canonical checkout runs `stratarc sync` against its own root.
canonical="$scratch/canonical"
mkdir -p "$canonical"
new_repo "$canonical"
(cd "$canonical" && STRATARC_BIN="$stub_ok" bash "$HOOK") >/dev/null 2>&1 || fail 'the canonical checkout failed'
[ -f "$canonical/deployed" ] || fail 'the canonical checkout did not run stratarc sync'
recorded="$(cat "$canonical/deployed")"
real_root="$(cd "$canonical" && git rev-parse --show-toplevel)"
[ "$recorded" = "sync|$real_root" ] || fail "stratarc was called as '$recorded', expected 'sync|$real_root'"
rm -f "$canonical/deployed"

# A linked worktree holds its own branch's copy of the shared config and must
# not publish it.
git -C "$canonical" worktree add -q "$scratch/linked" -b linked
output="$(cd "$scratch/linked" && STRATARC_BIN="$stub_ok" bash "$HOOK" 2>&1)" || fail 'the hook failed inside a linked worktree'
[ ! -f "$scratch/linked/deployed" ] || fail 'a linked worktree deployed the shared config'
case "$output" in *"linked worktree"*) ;; *) fail "the worktree skip was not explained: $output" ;; esac

# A synchronization failure is surfaced, never swallowed.
failing="$scratch/failing"
mkdir -p "$failing"
new_repo "$failing"
set +e
output="$(cd "$failing" && STRATARC_BIN="$stub_fail" bash "$HOOK" 2>&1)"
status=$?
set -e
[ "$status" -ne 0 ] || fail 'synchronization failure was hidden'
case "$output" in *"synchronization failed"*) ;; *) fail "the failure was not reported: $output" ;; esac

# No stratarc command on PATH: skipped with a message, the commit still succeeds.
set +e
output="$(cd "$canonical" && STRATARC_BIN="$scratch/no-such-stratarc" bash "$HOOK" 2>&1)"
status=$?
set -e
[ "$status" -eq 0 ] || fail 'a missing stratarc command failed the commit'
[ ! -f "$canonical/deployed" ] || fail 'a deploy ran without an stratarc command'
case "$output" in *"not on PATH"*) ;; *) fail "the missing command was not reported: $output" ;; esac

# Outside a git repository the hook does nothing.
nogit="$scratch/nogit"
mkdir -p "$nogit"
(cd "$nogit" && STRATARC_BIN="$stub_ok" bash "$HOOK") >/dev/null 2>&1 || fail 'the hook failed outside a repository'

# The hook carries no operator-specific deploy machinery.
if grep -Eq 'roborev|deploy_guard|scripts/sync.py|llm-root' "$HOOK"; then
  fail 'post-commit still references retired deploy machinery'
fi

printf '%s\n' 'post-commit.test: passed'
