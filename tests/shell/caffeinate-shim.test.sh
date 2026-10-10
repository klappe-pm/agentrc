#!/usr/bin/env bash
# Verify caffeinate-shim.sh: passthrough to the real binary when the caller
# is not claude, and the presence of the claude-skip branch.
#
# The claude-skip branch itself (`ps -o comm= -p "$PPID"` equals "claude")
# is not exercised here: faking a parent process name requires either a
# real process actually named claude or stubbing `ps`, and an earlier
# manual check of this shim verified that branch empirically against
# a live Claude Code session (pgrep and pmset assertion checks), which is
# the reliable evidence for an OS-level process check like this one. This
# test covers what a unit test can cover honestly: the passthrough path,
# and that the skip condition still names the process this shim exists for.
set -euo pipefail

SHIM="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../stratarc/data/bin" && pwd)/caffeinate-shim.sh"

fail() {
  printf '%s\n' "caffeinate-shim.test: $1" >&2
  exit 1
}

# The shim wraps a macOS tool; on any other machine there is nothing to compare against, so the test is skipped (status 77), not failed.
if ! command -v /usr/bin/caffeinate >/dev/null 2>&1; then
  printf '%s\n' "caffeinate-shim.test: no /usr/bin/caffeinate on this machine to compare against" >&2
  exit 77
fi

# The test runner's own process is the parent here, not claude, so the shim
# must pass every argument straight through to the real binary. A long-lived
# timed run is a `caffeinate` invocation with an observable side effect: the
# process exists in the process table until it is killed or its `-t` runs
# out. `-t 10` and a five-second poll bound give the real binary plenty of
# room to appear even when the machine is busy forking and execing many
# other processes at once (a full parallel test run); the process is
# killed as soon as it is seen so a normal run still finishes in well under
# a second. The pid is matched as a whole line (`grep -qx`), not a
# substring, so a pid that happens to be a substring of another process's
# line cannot produce a false match.
"$SHIM" -t 10 &
shim_pid=$!
seen=0
for _ in $(seq 1 50); do
  if pgrep -f "caffeinate -t 10" | grep -qx "$shim_pid"; then
    seen=1
    break
  fi
  sleep 0.1
done
kill "$shim_pid" 2>/dev/null || true
wait "$shim_pid" 2>/dev/null || true
[ "$seen" -eq 1 ] || fail "passthrough did not start the real caffeinate binary"
if pgrep -f "caffeinate -t 10" | grep -qx "$shim_pid" 2>/dev/null; then
  fail "passthrough did not exit after being signalled"
fi

grep -q 'comm= -p "\$PPID"' "$SHIM" || fail "the parent-process check is missing from the shim source"
grep -q '= "claude" \]' "$SHIM" || fail "the shim no longer names claude as the process it skips for"
grep -q 'exec /usr/bin/caffeinate' "$SHIM" || fail "the shim no longer execs the real binary for every other caller"

printf '%s\n' "caffeinate-shim.test: passed"
