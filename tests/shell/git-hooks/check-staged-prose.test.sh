#!/usr/bin/env bash
# Behavioral tests for agentrc/data/git-hooks/check-staged-prose.sh
#
# The pre-commit backstop must catch a staged em-dash and a staged hard-wrap
# in Markdown, pass clean Markdown, ignore non-Markdown, and honor the
# ALLOW_EMDASH / ALLOW_HARDWRAP escape hatches. Each case runs in a throwaway
# git repo, staging a file (no commit needed: the check reads the index blob).
# The forbidden glyphs are built from bytes so this test file carries no
# literal em-dash for the live guard to block on write.

set -euo pipefail

CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../agentrc/data/git-hooks" && pwd)/check-staged-prose.sh"

EMDASH=$'\xe2\x80\x94'   # U+2014
ENDASH=$'\xe2\x80\x93'   # U+2013

PASS=0
FAIL=0

# run_case <label> <expect: block|allow> <env-prefix> -- writes named files via
# the remaining args as "path=content" pairs, stages them, runs the check.
# stdin note: file content may not contain '=' before the first '=' delimiter.
run_case() {
  local label="$1" expect="$2" env="$3"
  shift 3
  local repo status got
  repo="$(mktemp -d "${TMPDIR:-/tmp}/check-staged-prose-XXXXXX")"
  (
    cd "$repo"
    git init -q
    git config user.email t@t.test
    git config user.name test
    local pair path content
    for pair in "$@"; do
      path="${pair%%=*}"
      content="${pair#*=}"
      printf '%s\n' "$content" > "$path"
      git add "$path"
    done
  )
  set +e
  ( cd "$repo" && env $env bash "$CHECK" >/dev/null 2>&1 )
  status=$?
  set -e
  rm -rf "$repo"
  if [ "$status" -eq 0 ]; then got=allow; else got=block; fi
  if [ "$got" = "$expect" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf '  check-staged-prose.sh FAIL  %-45s expected=%-5s got=%s\n' "$label" "$expect" "$got" >&2
  fi
}

run_case "staged em-dash is blocked"        block "" "bad.md=a ${EMDASH} b"
run_case "staged connector en-dash blocked" block "" "bad.md=foo ${ENDASH} bar"
run_case "numeric-range en-dash allowed"    allow "" "ok.md=pages 2${ENDASH}3"
run_case "staged hard-wrap is blocked"      block "" $'wrap.md=para line one\npara line two'
run_case "clean single-line paragraph"      allow "" "ok.md=One clean paragraph on one line."
run_case "separate list items are clean"    allow "" $'ok.md=- item one\n- item two'
run_case "non-markdown file is ignored"     allow "" "code.py=a ${EMDASH} b"
run_case "clean commit passes"              allow "" "ok.md=Nothing wrong here."

# Escape hatches bypass their respective check.
run_case "ALLOW_EMDASH bypasses em-dash"    allow "ALLOW_EMDASH=1" "bad.md=a ${EMDASH} b"
run_case "ALLOW_HARDWRAP bypasses hardwrap" allow "ALLOW_HARDWRAP=1" $'wrap.md=para one\npara two'
# A skipped check must not mask the other rule.
run_case "ALLOW_HARDWRAP still blocks em-dash" block "ALLOW_HARDWRAP=1" "bad.md=a ${EMDASH} b"

# Review cluster F2: a non-UTF-8 Markdown file is judged, not skipped. The
# detector crashed on it and the gate read the crash as clean.
LATIN=$'caf\xe9'
run_case "non-UTF-8 hard-wrap is blocked"   block "" "latin.md=${LATIN} line one"$'\n'"line two"
run_case "non-UTF-8 em-dash is blocked"     block "" "latin.md=${LATIN} a ${EMDASH} b"
run_case "non-UTF-8 clean file passes"      allow "" "latin.md=${LATIN} on one clean line."

# A detector that crashes has not judged the file, so the commit fails closed.
# A stub python3 on PATH exits 1 for every call; `command -v python3` still
# finds it, so only the exit handling is exercised.
STUB_BIN="$(mktemp -d "${TMPDIR:-/tmp}/check-staged-prose-stub-XXXXXX")"
printf '#!/bin/sh\necho "Traceback: simulated detector crash" >&2\nexit 1\n' > "$STUB_BIN/python3"
chmod +x "$STUB_BIN/python3"
run_case "detector crash blocks the commit" block "PATH=$STUB_BIN:$PATH" "ok.md=Nothing wrong here."
rm -rf "$STUB_BIN"

printf 'check-staged-prose.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
