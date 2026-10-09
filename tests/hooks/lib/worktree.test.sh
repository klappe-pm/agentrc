#!/usr/bin/env bash
# Behavioral tests for worktree.sh, the shared helpers sourced by
# worktree-create.sh, worktree-remove.sh, and worktree-validate.sh.
#
# Nothing in the repository called these functions directly before: each
# lifecycle hook was only exercised end to end, through its own
# worktree-lifecycle.test.sh, never through a case that calls one helper and
# checks its return value in isolation.

set -euo pipefail

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/hooks/lib" && pwd)"
# shellcheck source=worktree.sh
source "$LIB/worktree.sh"

PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); }
bad() {
  FAIL=$((FAIL + 1))
  printf 'FAIL: %s\n' "$1" >&2
  printf '      %s\n' "${2:-}" >&2
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/worktree-lib-test-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
# The physical path, so a /tmp-under-a-symlink layout (macOS aliases /var to
# /private/var) matches what realpath() itself resolves to.
WORK="$(cd "$WORK" && pwd -P)"

# --- worktree_absolute_path: resolves symlinks and ~ ---------------------------
mkdir -p "$WORK/real-target"
ln -s "$WORK/real-target" "$WORK/link-to-target"
resolved="$(worktree_absolute_path "$WORK/link-to-target")"
[ "$resolved" = "$WORK/real-target" ] && ok || bad "resolves a symlink to its real path" "got $resolved, expected $WORK/real-target"

resolved="$(worktree_absolute_path "~")"
[ "$resolved" = "$(cd "$HOME" && pwd -P)" ] && ok || bad "expands ~ to \$HOME" "got $resolved"

# --- worktree_root: honors LLM_WORKTREE_ROOT, defaults otherwise ---------------
mkdir -p "$WORK/custom-root"
root="$(LLM_WORKTREE_ROOT="$WORK/custom-root" worktree_root)"
[ "$root" = "$WORK/custom-root" ] && ok || bad "LLM_WORKTREE_ROOT overrides the default root" "got $root, expected $WORK/custom-root"

root="$(env -u LLM_WORKTREE_ROOT bash -c 'source "'"$LIB"'/worktree.sh" && worktree_root')"
# Resolved through the same helper the library itself uses, not a
# hand-assembled `cd "$HOME" && pwd -P` concatenation: that only resolved
# symlinks in $HOME itself, not in a $HOME/projects that is its own symlink
# (issue #291), so a container whose ~/projects points at /data/projects
# compared an unresolved expectation against the library's fully resolved
# result and failed on a host layout difference, not a real regression.
expected_default="$(worktree_absolute_path "$HOME/projects/_worktrees")"
[ "$root" = "$expected_default" ] && ok || bad "defaults to \$HOME/projects/_worktrees" "got $root, expected $expected_default"

# --- worktree_metadata_path: pure string formatting ----------------------------
path="$(worktree_metadata_path "/root" "my-repo" "my-worktree")"
expected="/root/.llm-worktree-registry/my-repo/my-worktree.json"
[ "$path" = "$expected" ] && ok || bad "formats the registry entry path" "got $path, expected $expected"

# --- worktree_metadata_repo: reads the repo field, empty on unreadable --------
entry="$WORK/entry.json"
printf '{"repo": "/home/x/projects/active/example"}' > "$entry"
repo="$(worktree_metadata_repo "$entry")"
[ "$repo" = "/home/x/projects/active/example" ] && ok || bad "reads the repo field from a registry entry" "got $repo"

repo="$(worktree_metadata_repo "$WORK/does-not-exist.json")"
[ "$repo" = "" ] && ok || bad "a missing entry file yields an empty repo, not an error" "got '$repo'"

# --- worktree_registered: checks git worktree list --porcelain ----------------
main_repo="$WORK/main-repo"
git init -q "$main_repo"
git -C "$main_repo" config user.email t@t.test
git -C "$main_repo" config user.name test
printf 'root\n' > "$main_repo/README.md"
git -C "$main_repo" add README.md
git -C "$main_repo" commit -q -m root

added_worktree="$WORK/added-worktree"
git -C "$main_repo" worktree add -q -b wt-branch "$added_worktree" >/dev/null 2>&1

if worktree_registered "$main_repo" "$added_worktree"; then
  ok
else
  bad "recognizes a real worktree git itself lists" "worktree_registered returned false"
fi

if worktree_registered "$main_repo" "$WORK/never-added"; then
  bad "does not claim an unregistered path is a worktree" "worktree_registered returned true"
else
  ok
fi

printf 'worktree.test: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
