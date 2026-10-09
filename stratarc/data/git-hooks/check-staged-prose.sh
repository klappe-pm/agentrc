#!/usr/bin/env bash
# Git pre-commit backstop for the em-dash and hard-wrap prose rules.
#
# The PreToolUse guard hooks/prose-guard.sh only fires when a write goes
# through the agent runtime. A violation staged directly
# with plain git (or by another tool) bypasses them. This script closes that
# gap: it scans the STAGED content of every Markdown file in the index for the
# same two violations, using the same detector (hooks/lib/prose-detect.py), and
# aborts the commit if any file trips it.
#
# Wired in via pre-commit in this directory and the repository core.hooksPath.
#
# Escape hatches match the guards' env vars so a bypass works the same way:
#   ALLOW_EMDASH=1    skip the em-dash / connector-en-dash check
#   ALLOW_HARDWRAP=1  skip the hard-wrap check
# Both set means the check is a no-op. The detector is resolved relative to
# this script so it works from any repo the hook is installed into.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT="$SCRIPT_DIR/../hooks/lib/prose-detect.py"

# A missing required detector is a broken policy gate, so fail closed.
if [ ! -f "$DETECT" ] || ! command -v python3 >/dev/null 2>&1; then
  printf 'check-staged-prose: required detector or python3 is unavailable.\n' >&2
  exit 1
fi

check_emdash=1
check_hardwrap=1
[ "${ALLOW_EMDASH:-0}" = "1" ] && check_emdash=0
[ "${ALLOW_HARDWRAP:-0}" = "1" ] && check_hardwrap=0
if [ "$check_emdash" -eq 0 ] && [ "$check_hardwrap" -eq 0 ]; then
  exit 0
fi

violations=0

# run_detect <mode> <file> <content>: prints the detector's reason. A detector
# that exits nonzero has not judged the file, so that is reported and counted
# as a violation: the gate fails closed. Review cluster F2: each call used to
# end in `2>/dev/null || true`, so a crash (a non-UTF-8 Markdown file raised
# UnicodeDecodeError) read as a clean file and the commit went through.
run_detect() {
  local mode="$1" file="$2" content="$3" out err
  err="$(mktemp "${TMPDIR:-/tmp}/check-staged-prose-err-XXXXXX")"
  if ! out="$(printf '%s' "$content" | python3 "$DETECT" "$mode" 2>"$err")"; then
    printf 'check-staged-prose: %s: the %s detector failed, so the file was not judged\n' "$file" "$mode" >&2
    sed 's/^/  /' "$err" >&2 || true
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  printf '%s' "$out"
}

# Staged Markdown paths only (added/copied/modified), NUL-delimited so paths
# with spaces survive intact.
while IFS= read -r -d '' file; do
  # Read the staged blob (`:path`), not the working tree, so a partially
  # staged file is judged by exactly what would be committed.
  content="$(git show ":$file" 2>/dev/null || true)"
  [ -n "$content" ] || continue

  if [ "$check_emdash" -eq 1 ]; then
    if ! reason="$(run_detect emdash "$file" "$content")"; then
      violations=$((violations + 1))
    elif [ -n "$reason" ]; then
      printf 'check-staged-prose: %s: forbidden dash (%s)\n' "$file" "$reason" >&2
      violations=$((violations + 1))
    fi
  fi

  if [ "$check_hardwrap" -eq 1 ]; then
    if ! reason="$(run_detect hardwrap "$file" "$content")"; then
      violations=$((violations + 1))
    elif [ -n "$reason" ]; then
      printf 'check-staged-prose: %s: hard-wrapped prose (line starting "%s")\n' "$file" "$reason" >&2
      violations=$((violations + 1))
    fi
  fi
done < <(git diff --cached --name-only --diff-filter=ACM -z -- '*.md')

if [ "$violations" -gt 0 ]; then
  printf 'check-staged-prose: %d violation(s); commit blocked (the no-em-dash and no-hardwrapped-writing rules).\n' "$violations" >&2
  printf 'Rewrite the flagged Markdown, or set ALLOW_EMDASH=1 / ALLOW_HARDWRAP=1 only to commit verbatim source text.\n' >&2
  exit 1
fi

exit 0
