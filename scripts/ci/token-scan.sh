#!/usr/bin/env bash
# Scan every tracked and untracked, non-ignored file for token-shaped values
# with the shared detector in agentrc/data/hooks/lib/guard-utils.sh. Prints
# the path and the kind of each match, never the value. Exit 0 clean, 1 on
# any match, 2 when the detector is unavailable.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
UTILS="$ROOT/agentrc/data/hooks/lib/guard-utils.sh"
if [ ! -f "$UTILS" ]; then
  printf 'token-scan: required detector is unavailable: %s\n' "$UTILS" >&2
  exit 2
fi
# shellcheck source=../../agentrc/data/hooks/lib/guard-utils.sh
. "$UTILS"

violations=0
while IFS= read -r -d '' file; do
  path="$ROOT/$file"
  [ -f "$path" ] || continue
  # Binary files carry no authored text to judge.
  if ! grep -Iq . "$path" 2>/dev/null; then
    continue
  fi
  label="$(ss_match_label "$(cat "$path")" || true)"
  if [ -n "$label" ]; then
    printf 'token-scan: %s: token-shaped value detected (%s)\n' "$file" "$label" >&2
    violations=$((violations + 1))
  fi
done < <(git -C "$ROOT" ls-files -z --cached --others --exclude-standard)

if [ "$violations" -gt 0 ]; then
  printf 'token-scan: %d file(s) with token-shaped values.\n' "$violations" >&2
  exit 1
fi
printf 'token-scan: clean.\n'
