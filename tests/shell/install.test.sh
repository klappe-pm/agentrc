#!/usr/bin/env bash
# Verify scripts/install.sh: it installs the package with pipx, scaffolds a
# source root with agentrc init, leaves a non-empty directory alone, installs
# the caffeinate shim on macOS only, and fails when pipx fails. pipx and
# agentrc are stubs, so nothing is installed on the machine.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/agentrc-install-XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
fakehome="$scratch/home"
stubs="$scratch/stubs"
log="$scratch/calls.log"
mkdir -p "$fakehome" "$stubs"

fail() {
  printf '%s\n' "install.test: $1" >&2
  exit 1
}

# The stub pipx answers `environment --value PIPX_BIN_DIR` with its own
# directory, where the stub agentrc lives.
cat > "$stubs/pipx" <<'STUB'
#!/usr/bin/env bash
printf 'pipx %s\n' "$*" >> "$STUB_LOG"
[ "${PIPX_FAIL:-0}" = 1 ] && exit 17
if [ "$1" = environment ]; then
  printf '%s\n' "$STUB_BIN"
fi
exit 0
STUB
cat > "$stubs/agentrc" <<'STUB'
#!/usr/bin/env bash
printf 'agentrc %s\n' "$*" >> "$STUB_LOG"
mkdir -p "$2"
printf 'scaffold\n' > "$2/agentrc.toml"
STUB
chmod 755 "$stubs/pipx" "$stubs/agentrc"

run_install() {
  HOME="$fakehome" STUB_LOG="$log" STUB_BIN="$stubs" PATH="$stubs:$PATH" bash "$REPO/scripts/install.sh" "$@"
}

# macOS: package installed from the checkout, source root scaffolded, shim installed.
: > "$log"
AGENTRC_PLATFORM=Darwin run_install --source "$scratch/source" >"$scratch/out"
grep -q "^pipx install --force $REPO\$" "$log" || fail "pipx was not asked to install the checkout"
grep -q "^agentrc init $scratch/source\$" "$log" || fail "agentrc init did not run for the source root"
[ -f "$scratch/source/agentrc.toml" ] || fail "the source root was not scaffolded"
shim="$fakehome/bin/caffeinate"
[ -f "$shim" ] || fail "the caffeinate shim was not installed"
diff -q "$REPO/agentrc/data/bin/caffeinate-shim.sh" "$shim" >/dev/null || fail "the installed shim does not match its source"
# Python rather than stat: stat -f is BSD only and stat -c is GNU only.
mode="$(python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "$shim")"
[ "$mode" = "755" ] || fail "expected shim mode 755, got $mode"
grep -q 'install: done' "$scratch/out" || fail "no completion message"

# None of the retired steps run: no credentials, roborev, sync, or clone.
if grep -Eq 'credentials|roborev|sync|clone' "$REPO/scripts/install.sh"; then
  fail "install.sh still mentions a retired step"
fi

# A non-empty source directory is left alone.
printf 'mine\n' > "$scratch/source/keep.txt"
: > "$log"
AGENTRC_PLATFORM=Darwin run_install --source "$scratch/source" >"$scratch/out"
if grep -q '^agentrc init' "$log"; then fail "agentrc init ran into a non-empty directory"; fi
[ -f "$scratch/source/keep.txt" ] || fail "an existing file was disturbed"
grep -q 'leaving it alone' "$scratch/out" || fail "the skip was not reported"

# Linux: the shim is a macOS-only wrapper and is skipped with a reason.
linuxhome="$scratch/linux-home"
mkdir -p "$linuxhome"
HOME="$linuxhome" AGENTRC_PLATFORM=Linux STUB_LOG="$log" STUB_BIN="$stubs" PATH="$stubs:$PATH" \
  bash "$REPO/scripts/install.sh" --source "$scratch/linux-source" >"$scratch/linux-out"
[ ! -e "$linuxhome/bin/caffeinate" ] || fail "Linux mode installed the macOS-only shim"
grep -q 'caffeinate shim skipped on Linux' "$scratch/linux-out" || fail "Linux mode did not report the skipped shim"

# A pipx failure fails the install and does not scaffold or print completion.
rm -rf "$scratch/failed-source"
if PIPX_FAIL=1 AGENTRC_PLATFORM=Linux HOME="$fakehome" STUB_LOG="$log" STUB_BIN="$stubs" PATH="$stubs:$PATH" \
  bash "$REPO/scripts/install.sh" --source "$scratch/failed-source" >"$scratch/fail-out" 2>&1; then
  fail "a pipx failure was reported as success"
fi
[ ! -e "$scratch/failed-source" ] || fail "a source root was scaffolded after pipx failed"
if grep -q 'install: done' "$scratch/fail-out"; then fail "completion message printed after failure"; fi

printf '%s\n' 'install.test: passed'
