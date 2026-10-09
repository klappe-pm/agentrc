#!/usr/bin/env bash
# Regression tests for managed worktree lifecycle hooks.
set -euo pipefail

for required in git mktemp python3; do
  command -v "$required" >/dev/null 2>&1 || { echo "missing required tool: $required" >&2; exit 1; }
done

ROOT="$(cd "$(dirname "$0")/../../stratarc/data/hooks" && pwd)"
CREATE="$ROOT/worktree-create.sh"
REMOVE="$ROOT/worktree-remove.sh"
VALIDATE="$ROOT/worktree-validate.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/llm-worktree-test-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
FAIL=0

fail() {
  echo "FAIL: $1" >&2
  FAIL=1
}

create_payload() {
  python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1], "cwd": sys.argv[2]}))' "$1" "$2"
}

path_payload() {
  python3 -c 'import json,sys; print(json.dumps({"worktree_path": sys.argv[1], "cwd": sys.argv[2]}))' "$1" "$2"
}

REPO="$TMP/source"
HOME_DIR="$TMP/home"
WORKTREE_ROOT="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$HOME_DIR/projects/_worktrees")"
mkdir -p "$REPO" "$HOME_DIR"
git -C "$REPO" init -q -b main
git -C "$REPO" config user.email test@example.invalid
git -C "$REPO" config user.name test
printf 'base\n' > "$REPO/file"
git -C "$REPO" add file
git -C "$REPO" commit -qm initial

# A requested path outside the approved root is normalized under the canonical root.
REQUESTED="claude-session"
CLAUDE_DEST="$WORKTREE_ROOT/source/claude-session"
OUT="$(create_payload "$REQUESTED" "$REPO" | HOME="$HOME_DIR" LLM_WORKTREE_RUNTIME=claude bash "$CREATE")" || fail "create canonical Claude worktree"
[ "$OUT" = "$CLAUDE_DEST" ] || fail "create returns the canonical Claude destination"
[ -d "$CLAUDE_DEST" ] || fail "Claude worktree exists"

# Claude and Codex can create independent worktrees for the same source repository.
CODEX_REQUESTED="codex-session"
CODEX_DEST="$WORKTREE_ROOT/source/codex-session"
create_payload "$CODEX_REQUESTED" "$REPO" | HOME="$HOME_DIR" LLM_WORKTREE_RUNTIME=codex bash "$CREATE" > "$TMP/codex.out" || fail "create canonical Codex worktree"
[ "$(cat "$TMP/codex.out")" = "$CODEX_DEST" ] || fail "create returns the canonical Codex destination"
[ -d "$CODEX_DEST" ] || fail "Codex worktree exists"
[ "$CLAUDE_DEST" != "$CODEX_DEST" ] || fail "runtime sessions receive independent destinations"

# An ordinary pre-existing directory is never deleted or claimed.
UNOWNED="$WORKTREE_ROOT/source/unowned"
mkdir -p "$UNOWNED"
printf 'preserve\n' > "$UNOWNED/marker"
if create_payload "unowned" "$REPO" | HOME="$HOME_DIR" bash "$CREATE" >/dev/null 2>&1; then
  fail "refuse an unowned destination"
fi
[ -f "$UNOWNED/marker" ] || fail "unowned destination remains untouched"

# Reusing a dirty managed destination is refused and removal leaves it intact.
printf 'dirty\n' >> "$CLAUDE_DEST/file"
if create_payload "$REQUESTED" "$REPO" | HOME="$HOME_DIR" bash "$CREATE" >/dev/null 2>&1; then
  fail "refuse a dirty managed destination"
fi
path_payload "$CLAUDE_DEST" "$REPO" | HOME="$HOME_DIR" bash "$REMOVE" >/dev/null || fail "remove returns without deleting a dirty worktree"
[ -d "$CLAUDE_DEST" ] || fail "dirty managed worktree remains intact"

# A valid managed worktree passes session validation, while a stale cwd is rejected.
path_payload "$CLAUDE_DEST" "$CLAUDE_DEST" | HOME="$HOME_DIR" bash "$VALIDATE" || fail "validate a registered managed worktree"
STALE="$TMP/missing-session"
if path_payload "$STALE" "$STALE" | HOME="$HOME_DIR" bash "$VALIDATE" >/dev/null 2>&1; then
  fail "reject a stale session cwd"
fi

# Clean worktrees are removed without force and the independent Codex worktree remains.
git -C "$CLAUDE_DEST" checkout -- file
path_payload "$CLAUDE_DEST" "$REPO" | HOME="$HOME_DIR" bash "$REMOVE" || fail "remove a clean managed worktree"
[ ! -e "$CLAUDE_DEST" ] || fail "clean managed worktree removed"
[ -d "$CODEX_DEST" ] || fail "Codex worktree remains after Claude cleanup"

# Disable mode bypasses cleanup and validation, while creation remains active because Claude requires a returned path.
DISABLED_REMOVE_PAYLOAD="$(path_payload "$CODEX_DEST" "$REPO")"
HOME="$HOME_DIR" RUNTIME_HOOKS_DISABLE=1 bash "$REMOVE" <<< "$DISABLED_REMOVE_PAYLOAD" || fail "disabled removal returns successfully"
[ -d "$CODEX_DEST" ] || fail "disabled removal retains the worktree"
DISABLED_VALIDATE_PAYLOAD="$(path_payload "$STALE" "$STALE")"
if ! HOME="$HOME_DIR" RUNTIME_HOOKS_DISABLE=1 bash "$VALIDATE" <<< "$DISABLED_VALIDATE_PAYLOAD"; then
  fail "disabled validation returns successfully"
fi

[ "$FAIL" -eq 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit "$FAIL"
