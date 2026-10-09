#!/bin/sh
# Claude Code spawns `caffeinate -i -t 300` with no setting to turn it off.
# Ignore calls whose parent is claude; pass everything else to the real binary.
if [ "$(ps -o comm= -p "$PPID")" = "claude" ]; then
  exit 0
fi
exec /usr/bin/caffeinate "$@"
