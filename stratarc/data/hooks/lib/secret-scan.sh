#!/usr/bin/env bash
# Compatibility wrapper for secret detection helpers. The implementation now lives
# in guard-utils.sh so callers can source one canonical behavior file.

set -u

_SS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=guard-utils.sh
source "$_SS_LIB_DIR/guard-utils.sh"

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  bash "$_SS_LIB_DIR/guard-utils.sh" "$@"
fi
