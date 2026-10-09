#!/usr/bin/env bash
# Remove only clean worktrees created by worktree-create.sh.
set -euo pipefail

[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

for required in git python3; do
  command -v "$required" >/dev/null 2>&1 || { echo "worktree-remove: missing required tool: $required" >&2; exit 0; }
done

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/worktree.sh
source "$HOOK_DIR/lib/worktree.sh"

payload="$(cat)"
WORKTREE_PATH="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("worktree_path", ""))' <<< "$payload" 2>/dev/null || true)"
[ -n "$WORKTREE_PATH" ] || exit 0
WORKTREE_ROOT="$(worktree_root)"
DESTINATION="$(worktree_absolute_path "$WORKTREE_PATH")"

case "$DESTINATION" in
  "$WORKTREE_ROOT"/*/*) ;;
  *) echo "worktree-remove: refusing destination outside managed root: $DESTINATION" >&2; exit 0 ;;
esac

REPOSITORY_NAME="$(basename "$(dirname "$DESTINATION")")"
WORKTREE_NAME="$(basename "$DESTINATION")"
METADATA="$(worktree_metadata_path "$WORKTREE_ROOT" "$REPOSITORY_NAME" "$WORKTREE_NAME")"
[ -f "$METADATA" ] || { echo "worktree-remove: refusing unmanaged destination: $DESTINATION" >&2; exit 0; }
REPOSITORY="$(worktree_metadata_repo "$METADATA")"
[ -n "$REPOSITORY" ] && [ -d "$DESTINATION" ] || { echo "worktree-remove: retained unavailable worktree: $DESTINATION" >&2; exit 0; }

worktree_registered "$REPOSITORY" "$DESTINATION" || { echo "worktree-remove: retained unregistered worktree: $DESTINATION" >&2; exit 0; }

if [ -n "$(git -C "$DESTINATION" status --porcelain)" ]; then
  echo "worktree-remove: retained dirty worktree: $DESTINATION" >&2
  exit 0
fi

if git -C "$REPOSITORY" worktree remove "$DESTINATION"; then
  rm -f "$METADATA"
else
  echo "worktree-remove: retained worktree after git refused removal: $DESTINATION" >&2
fi
exit 0
