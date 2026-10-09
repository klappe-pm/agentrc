#!/usr/bin/env bash
# Behavioral tests for stratarc/data/git-hooks/strip-commit-attribution.sh
#
# The commit-msg backstop must remove agent attribution from a message file in
# place, keep the subject and body, leave a clean message byte for byte alone,
# and strip even when ALLOW_AGENT_ATTRIBUTION=1 is set, because that switch is
# for quoting in documents and never applies to a commit message. Agent names
# are assembled from fragments so this file carries no literal attribution line.

set -euo pipefail

# A session that used the document escape hatch can leave it exported. The
# stripper must ignore it; the cases below run once with it unset and once with
# it set so both paths are proven.
unset ALLOW_AGENT_ATTRIBUTION

STRIP="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../stratarc/data/git-hooks" && pwd)/strip-commit-attribution.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

AGENT="Cla""ude"
VENDOR="anthro""pic.com"

PASS=0
FAIL=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  strip-commit-attribution.sh FAIL  %s\n    expected: %q\n    actual:   %q\n' \
      "$label" "$expected" "$actual" >&2
  fi
}

# A message carrying every attribution form keeps only the human content.
msg="$TMP/dirty"
{
  printf 'feat: add the attribution guard\n\n'
  printf 'Denies attribution before the write lands.\n\n'
  printf -- '---\n'
  printf 'Generated with %s Code\n' "$AGENT"
  printf 'Co-Authored-By: %s <noreply@%s>\n' "$AGENT" "$VENDOR"
  printf '%s-Session: https://cla' "$AGENT"; printf 'ude.ai/code/session_01Ab\n'
} > "$msg"
bash "$STRIP" "$msg"
check "strips every attribution form" \
  "$(printf 'feat: add the attribution guard\n\nDenies attribution before the write lands.')" \
  "$(cat "$msg")"

# A clean message is untouched.
clean="$TMP/clean"
printf 'fix: correct the staging path\n\nBody stays put.\n' > "$clean"
before="$(cat "$clean")"
bash "$STRIP" "$clean"
check "leaves a clean message alone" "$before" "$(cat "$clean")"

# A human co-author trailer survives.
human="$TMP/human"
printf 'fix: pair work\n\nCo-Authored-By: Dana Lee <d@example.com>\n' > "$human"
before="$(cat "$human")"
bash "$STRIP" "$human"
check "keeps a human co-author trailer" "$before" "$(cat "$human")"

# The document escape hatch has no effect on a commit message: the trailer is
# stripped even with the switch set.
hatch="$TMP/hatch"
printf 'docs: quote a trailer\n\nCo-Authored-By: %s <a@b>\n' "$AGENT" > "$hatch"
ALLOW_AGENT_ATTRIBUTION=1 bash "$STRIP" "$hatch"
check "strips even with ALLOW_AGENT_ATTRIBUTION set" \
  "$(printf 'docs: quote a trailer')" \
  "$(cat "$hatch")"

# Review cluster F2: a session permalink on a `session-link:` line is not docs
# frontmatter in a commit message, so the backstop strips it. The detect gate
# used to exempt the line, so the strip never ran.
link="$TMP/link"
printf 'fix: x\n\nBody.\n\nsession-link: https://cla' > "$link"; printf 'ude.ai/code/session_01Ab\n' >> "$link"
bash "$STRIP" "$link" 2>/dev/null || true
check "strips a session-link permalink line" "$(printf 'fix: x\n\nBody.')" "$(cat "$link")"

# Review cluster F2: a commit message in a legacy encoding (not UTF-8) is still
# judged and stripped, and every other byte is kept exactly.
latin="$TMP/latin"
printf 'fix: caf\xe9\n\nCo-Authored-By: %s <a@b>\n' "$AGENT" > "$latin"
printf 'fix: caf\xe9\n' > "$TMP/latin-want"
bash "$STRIP" "$latin" 2>/dev/null || true
if cmp -s "$latin" "$TMP/latin-want"; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  printf '  strip-commit-attribution.sh FAIL  non-UTF-8 message: got %s\n' "$(od -c "$latin" | head -n 3 | tr '\n' ' ')" >&2
fi

# A detector that crashes is a broken policy gate: the commit fails closed
# rather than passing unjudged. A stub python3 that exits 1 stands in for any
# crash; the message file is left as it was.
STUB="$TMP/stub-bin"
mkdir -p "$STUB"
printf '#!/bin/sh\necho "Traceback: simulated detector crash" >&2\nexit 1\n' > "$STUB/python3"
chmod +x "$STUB/python3"
crash="$TMP/crash"
printf 'fix: x\n\nCo-Authored-By: %s <a@b>\n' "$AGENT" > "$crash"
if PATH="$STUB:$PATH" bash "$STRIP" "$crash" >/dev/null 2>&1; then
  FAIL=$((FAIL+1))
  printf '  strip-commit-attribution.sh FAIL  detector crash should fail the commit\n' >&2
else
  PASS=$((PASS+1))
fi

# A missing message file is a usage error, not a silent pass.
if bash "$STRIP" "$TMP/nope" >/dev/null 2>&1; then
  FAIL=$((FAIL+1))
  printf '  strip-commit-attribution.sh FAIL  missing file should exit nonzero\n' >&2
else
  PASS=$((PASS+1))
fi

printf 'strip-commit-attribution.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
