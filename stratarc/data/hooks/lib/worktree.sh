#!/usr/bin/env bash
# Shared helpers for the worktree lifecycle hooks (worktree-create.sh,
# worktree-remove.sh, worktree-validate.sh). Each hook once carried its own
# copy of the realpath, registry path and registration checks; this file holds
# the single implementation. Source it after computing HOOK_DIR.

# Canonical absolute path with symlinks resolved and ~ expanded.
worktree_absolute_path() {
  python3 -c 'import os,sys; print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))' "$1"
}

# The managed worktree root, honoring LLM_WORKTREE_ROOT.
worktree_root() {
  worktree_absolute_path "${LLM_WORKTREE_ROOT:-$HOME/projects/_worktrees}"
}

# Registry entry path for one worktree: <root> <repository-name> <worktree-name>.
worktree_metadata_path() {
  printf '%s/.llm-worktree-registry/%s/%s.json\n' "$1" "$2" "$3"
}

# The repository a registry entry names, or nothing when the entry is unreadable.
worktree_metadata_repo() {
  python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); print(data.get("repo", ""))' "$1" 2>/dev/null || true
}

# Whether git lists <destination> as a worktree of <repository>.
worktree_registered() {
  local repo="$1"
  local destination="$2"
  local entry
  while IFS= read -r entry; do
    [ "$entry" = "worktree $destination" ] && return 0
  done <<EOF
$(git -C "$repo" worktree list --porcelain 2>/dev/null)
EOF
  return 1
}
