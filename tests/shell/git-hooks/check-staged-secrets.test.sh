#!/usr/bin/env bash
# Behavioral tests for the staged secret scanner.
set -euo pipefail

CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/git-hooks" && pwd)/check-staged-secrets.sh"
PASS=0
FAIL=0

# run_case <label> <allow|block> <content> [secret]: stages content and runs the
# gate. The gate's whole output (stdout and stderr) is kept and judged too:
# the no-secret-exposure rule forbids the matched value reaching any log, so
# when a secret is named it must not appear in that output, and a block must
# name the file and the detector label. Review cluster F2: the output used to
# be discarded, so a gate that printed the secret it caught still passed.
run_case() {
  local label="$1" expect="$2" content="$3" secret="${4:-}"
  local repo status got output problem=""
  repo="$(mktemp -d "${TMPDIR:-/tmp}/check-staged-secrets-XXXXXX")"
  git -C "$repo" init -q
  printf '%s\n' "$content" > "$repo/example.txt"
  git -C "$repo" add example.txt
  set +e
  output="$(cd "$repo" && bash "$CHECK" 2>&1)"
  status=$?
  set -e
  rm -rf "$repo"
  if [ "$status" -eq 0 ]; then got=allow; else got=block; fi
  if [ "$got" != "$expect" ]; then
    problem="expected=$expect got=$got"
  elif [ -n "$secret" ] && printf '%s' "$output" | grep -qF -- "$secret"; then
    problem="the matched value appears in the gate's output"
  elif [ "$expect" = block ] && ! printf '%s' "$output" | grep -qF 'example.txt: token-shaped value detected ('; then
    problem="block output does not name the file and label: $output"
  fi
  if [ -z "$problem" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf 'check-staged-secrets.test: %s %s\n' "$label" "$problem" >&2
  fi
}

run_case "clean staged content" allow "ordinary configuration text"
generic_value="$(printf '%s%s' 'abcdefghijklmnop' 'qrstuvwx')"
provider_value="gh$(printf '%s%s' 'p_abcdefghijklmnop' 'qrstuvwx')"
run_case "token-shaped staged content" block "API_TOKEN=${generic_value}" "$generic_value"
scheme="secret"
reference="${scheme}://github/default"
run_case "logical reference" allow "${reference}"
run_case "reference plus actual credential" block "${reference} API_TOKEN=${generic_value}" "$generic_value"
run_case "reference plus provider credential" block "${reference} ${provider_value}" "$provider_value"

# F-22 (docs/ideas/secret-scan-generic-assignment-code-false-positive.md): the
# use-railway commit's two flagged lines, reproduced from fragments so this
# file carries no complete literal for the live scan to trip on.
run_case "dotted attribute reference" allow \
  "API_TOKEN: $(printf '%s%s%s' 'api' '.env.' 'INTERNAL_TOKEN'),"
run_case "function call result assignment" allow \
  "bigkeys_result = $(printf '%s%s' 'future_bigkeys' '.result')()"

printf 'check-staged-secrets.test: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
