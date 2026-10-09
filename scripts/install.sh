#!/usr/bin/env bash
# Install agentrc and scaffold a source root. Idempotent for the install step.
#
#   bash scripts/install.sh [--source DIR] [--package SPEC]
#
# Steps:
#   1. pipx install the agentrc package (SPEC defaults to the checkout this
#      script lives in, so a contributor installs what they are editing).
#   2. agentrc init DIR, only when DIR does not exist or is empty.
#   3. On macOS, install the caffeinate shim into ~/bin.
#
# The shim is skipped when AGENTRC_PLATFORM names a platform other than Darwin.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
package="$(cd "$here/.." && pwd -P)"
source_dir="${AGENTRC_SOURCE:-$HOME/agentrc-source}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) source_dir="$2"; shift 2 ;;
    --package) package="$2"; shift 2 ;;
    -h|--help)
      printf 'usage: install.sh [--source DIR] [--package SPEC]\n'
      exit 0
      ;;
    *)
      printf 'install: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

for dependency in pipx python3; do
  command -v "$dependency" >/dev/null 2>&1 || { printf 'install: required tool missing: %s\n' "$dependency" >&2; exit 1; }
done

# 1. the package
pipx install --force "$package"
printf 'install: agentrc installed from %s\n' "$package"

# 2. a source root, never into a directory that already holds files
if [ -d "$source_dir" ] && [ -n "$(ls -A "$source_dir")" ]; then
  printf 'install: %s is not empty; leaving it alone\n' "$source_dir"
else
  bin_dir="$(pipx environment --value PIPX_BIN_DIR)"
  "$bin_dir/agentrc" init "$source_dir"
fi

# 3. caffeinate shim: macOS only, it wraps /usr/bin/caffeinate. It is read from
# the checkout this script lives in.
platform="${AGENTRC_PLATFORM:-$(uname -s)}"
if [ "$platform" = "Darwin" ]; then
  shim="$here/../agentrc/data/bin/caffeinate-shim.sh"
  if [ -f "$shim" ]; then
    mkdir -p "$HOME/bin"
    install -m 755 "$shim" "$HOME/bin/caffeinate"
    printf 'install: caffeinate shim -> %s/bin/caffeinate\n' "$HOME"
  else
    printf 'install: caffeinate shim not found at %s; skipped\n' "$shim" >&2
  fi
else
  printf 'install: caffeinate shim skipped on %s\n' "$platform"
fi

printf 'install: done. Source root: %s\n' "$source_dir"
