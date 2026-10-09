#!/usr/bin/env bash
# Git commit-msg backstop for the no-agent-attribution rule.
#
# A PreToolUse attribution guard only fires when a commit goes through the
# runtime's Bash tool. A commit made directly with plain git
# (or by another tool, or through an editor session) bypasses it. This script
# closes that gap, and it does so by stripping rather than blocking: a commit
# message is not authored prose the user reviews line by line, so removing the
# trailer silently is the correct repair and a failed commit is not.
#
# It rewrites the message file in place, dropping every line that carries an
# agent co-author trailer, a "generated with" footer, a session permalink, a
# vendor noreply address, or a robot byline, using the same detector as the
# guard (stratarc/data/hooks/lib/attribution-detect.py). Comment lines that git
# itself adds are left untouched, since git strips them anyway.
#
# Wired in via git-hooks/commit-msg.
#
# There is no escape hatch here. ALLOW_AGENT_ATTRIBUTION=1 lets a document
# quote a third party's attribution verbatim; it never applies to a commit
# message, because no commit may reference an agent under any circumstance.
# A leftover ALLOW_AGENT_ATTRIBUTION=1 in a shell would otherwise stand this
# backstop down for every commit made from that shell.
#
# Usage: strip-commit-attribution.sh <path-to-commit-message-file>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT="$SCRIPT_DIR/../hooks/lib/attribution-detect.py"

msg_file="${1:-}"
if [ -z "$msg_file" ] || [ ! -f "$msg_file" ]; then
  printf 'strip-commit-attribution: no commit message file given.\n' >&2
  exit 1
fi

# A missing required detector is a broken policy gate, so fail closed.
if [ ! -f "$DETECT" ] || ! command -v python3 >/dev/null 2>&1; then
  printf 'strip-commit-attribution: required detector or python3 is unavailable.\n' >&2
  exit 1
fi

# A detector that exits nonzero has not judged the message, so the commit
# fails closed with its error shown. An earlier form of this call ended in
# `2>/dev/null || true`, so a crash (a non-UTF-8 message raised
# UnicodeDecodeError) read as a clean message and the commit went through
# unjudged.
err="$msg_file.attribution.err"
if ! label="$(python3 "$DETECT" detect <"$msg_file" 2>"$err")"; then
  printf 'strip-commit-attribution: the attribution detector failed, so the commit message was not judged; commit blocked.\n' >&2
  cat "$err" >&2 || true
  rm -f "$err"
  exit 1
fi
rm -f "$err"
[ -n "$label" ] || exit 0

tmp="$msg_file.attribution.tmp"
if ! python3 "$DETECT" strip <"$msg_file" >"$tmp"; then
  rm -f "$tmp"
  printf 'strip-commit-attribution: the attribution detector failed while stripping; commit blocked.\n' >&2
  exit 1
fi
mv "$tmp" "$msg_file"
printf 'strip-commit-attribution: removed agent attribution from the commit message (%s).\n' "$label" >&2
