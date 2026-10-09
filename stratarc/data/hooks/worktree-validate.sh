#!/usr/bin/env bash
# Reject stale or unregistered session worktrees before they become a session cwd.
set -euo pipefail

[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

for required in git python3; do
  command -v "$required" >/dev/null 2>&1 || { echo "worktree-validate: missing required tool: $required" >&2; exit 1; }
done

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/worktree.sh
source "$HOOK_DIR/lib/worktree.sh"

payload="$(cat)"
WORKSPACE="$(python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("cwd") or data.get("worktree_path") or "")' <<< "$payload" 2>/dev/null || true)"
[ -n "$WORKSPACE" ] || exit 0
WORKSPACE="$(worktree_absolute_path "$WORKSPACE")"
WORKTREE_ROOT="$(worktree_root)"

if [ ! -d "$WORKSPACE" ]; then
  echo "worktree-validate: stale session directory: $WORKSPACE" >&2
  exit 1
fi

case "$WORKSPACE" in
  "$WORKTREE_ROOT"/*/*) ;;
  *) exit 0 ;;
esac

REPOSITORY_NAME="$(basename "$(dirname "$WORKSPACE")")"
WORKTREE_NAME="$(basename "$WORKSPACE")"
METADATA="$(worktree_metadata_path "$WORKTREE_ROOT" "$REPOSITORY_NAME" "$WORKTREE_NAME")"
[ -f "$METADATA" ] || { echo "worktree-validate: unmanaged workspace: $WORKSPACE" >&2; exit 1; }
REPOSITORY="$(worktree_metadata_repo "$METADATA")"
[ -n "$REPOSITORY" ] || { echo "worktree-validate: invalid worktree registry: $WORKSPACE" >&2; exit 1; }

worktree_registered "$REPOSITORY" "$WORKSPACE" && exit 0

echo "worktree-validate: unregistered workspace: $WORKSPACE" >&2
exit 1
