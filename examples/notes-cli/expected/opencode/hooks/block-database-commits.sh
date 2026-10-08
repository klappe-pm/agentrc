#!/usr/bin/env bash
# PreToolUse hook for shell commands: refuse a git add or git commit that names
# a local database file. The tool payload arrives as JSON on standard input.
# Exit 2 blocks the command and returns the message on standard error to the
# agent. Backs the never-commit-local-databases rule.

set -euo pipefail

payload="$(cat)"

if printf '%s' "$payload" | grep -Eq 'git +(add|commit)(\\.|[^"\\])*\.(db|sqlite3?)(-journal|-wal|-shm)?([^[:alnum:]_]|$)'; then
  printf 'block-database-commits: refusing to stage a local database file (rule never-commit-local-databases).\n' >&2
  exit 2
fi

exit 0
