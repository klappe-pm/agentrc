#!/usr/bin/env bash
# Create or reuse a clean, hook-managed worktree below the shared root.
set -euo pipefail

for required in git mkdir python3; do
  command -v "$required" >/dev/null 2>&1 || { echo "worktree-create: missing required tool: $required" >&2; exit 1; }
done

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/worktree.sh
source "$HOOK_DIR/lib/worktree.sh"

payload="$(cat)"

field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1" <<< "$payload" 2>/dev/null || true
}

repository_path() {
  local candidate
  for candidate in "${LLM_WORKTREE_REPO:-}" "${CLAUDE_WORKTREE_REPO:-}" "${CODEX_WORKTREE_REPO:-}" "$1" "$2"; do
    if [ -n "$candidate" ] && git -C "$candidate" rev-parse --show-toplevel >/dev/null 2>&1; then
      git -C "$candidate" rev-parse --show-toplevel
      return 0
    fi
  done
  return 1
}

metadata_matches() {
  local metadata="$1"
  [ -f "$metadata" ] || return 1
  python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); raise SystemExit(0 if data.get("repo") == sys.argv[2] and data.get("path") == sys.argv[3] else 1)' "$metadata" "$REPOSITORY" "$DESTINATION" 2>/dev/null
}

acquire_lock() {
  local lock="$WORKTREE_ROOT/.llm-worktree-locks/$REPOSITORY_NAME.lock"
  local attempts=0
  mkdir -p "$(dirname "$lock")"
  until mkdir "$lock" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ]; then
      echo "worktree-create: timed out waiting for repository lock: $REPOSITORY" >&2
      exit 1
    fi
    sleep 1
  done
  LOCK_PATH="$lock"
  trap 'rmdir "$LOCK_PATH" 2>/dev/null || true' EXIT
}

SOURCE_REPO_PATH="$(field source_repo_path)"
CWD="$(field cwd)"
REQUESTED_PATH="$(field worktree_path)"
REQUESTED_NAME="$(field name)"
REPOSITORY="$(repository_path "$SOURCE_REPO_PATH" "$CWD" || true)"

if [ -z "$REPOSITORY" ]; then
  echo "worktree-create: cannot resolve a git repository." >&2
  exit 1
fi

WORKTREE_ROOT="$(worktree_root)"
REPOSITORY_NAME="$(basename "$REPOSITORY")"
RAW_NAME="${REQUESTED_NAME:-$(basename "${REQUESTED_PATH:-}")}"
WORKTREE_NAME="$(printf '%s' "$RAW_NAME" | tr -c 'A-Za-z0-9._-' '_')"
if [ -z "$WORKTREE_NAME" ] || [ "$WORKTREE_NAME" = "." ] || [ "$WORKTREE_NAME" = ".." ]; then
  WORKTREE_NAME="agent-${LLM_WORKTREE_RUNTIME:-session}-$$-$(date +%s)"
fi
DESTINATION="$WORKTREE_ROOT/$REPOSITORY_NAME/$WORKTREE_NAME"
METADATA="$(worktree_metadata_path "$WORKTREE_ROOT" "$REPOSITORY_NAME" "$WORKTREE_NAME")"

acquire_lock
git -C "$REPOSITORY" worktree prune >/dev/null 2>&1 || true

if [ -e "$DESTINATION" ]; then
  if ! metadata_matches "$METADATA" || ! worktree_registered "$REPOSITORY" "$DESTINATION"; then
    echo "worktree-create: refusing unowned destination: $DESTINATION" >&2
    exit 1
  fi
  if [ -n "$(git -C "$DESTINATION" status --porcelain)" ]; then
    echo "worktree-create: refusing dirty managed destination: $DESTINATION" >&2
    exit 1
  fi
  printf '%s\n' "$DESTINATION"
  exit 0
fi

mkdir -p "$(dirname "$DESTINATION")" "$(dirname "$METADATA")"
BASE="HEAD"
if git -C "$REPOSITORY" rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  BASE="origin/main"
fi
git -C "$REPOSITORY" worktree add --detach "$DESTINATION" "$BASE" >&2
python3 -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"repo": sys.argv[2], "path": sys.argv[3], "runtime": sys.argv[4]}, sort_keys=True) + "\n")' "$METADATA" "$REPOSITORY" "$DESTINATION" "${LLM_WORKTREE_RUNTIME:-unknown}"
printf '%s\n' "$DESTINATION"
