#!/usr/bin/env bash
# Block token-shaped values in staged content without printing matched values.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCAN="$SCRIPT_DIR/../hooks/lib/secret-scan.sh"

if [ ! -f "$SCAN" ]; then
  printf 'check-staged-secrets: required scanner is unavailable: %s\n' "$SCAN" >&2
  exit 1
fi

violations=0
while IFS= read -r -d '' file; do
  label="$(git show ":$file" 2>/dev/null | bash "$SCAN" label-stdin || true)"
  if [ -n "$label" ]; then
    printf 'check-staged-secrets: %s: token-shaped value detected (%s)\n' "$file" "$label" >&2
    violations=$((violations + 1))
  fi
done < <(git diff --cached --name-only --diff-filter=ACM -z)

if [ "$violations" -gt 0 ]; then
  printf 'check-staged-secrets: %d violation(s); commit blocked (the secrets-out-of-git rule).\n' "$violations" >&2
  exit 1
fi
