#!/usr/bin/env bash
# SessionStart hook: orient a session before its first tool call.
#
# Two behaviours, keyed on the cwd the runtime reports for this session:
#   home directory      print the project roster from session-resume.py, the
#                        open work items that have a loadout with their launch
#                        commands (session-launch.py --list-open), and ask for
#                        a project before running any tool; a launched session
#                        (LLM_ROOT_WORK_ITEM set) is not asked (S-13).
#   a project directory print the newest interrupted session card for that
#                        project (session-resume.py --newest-interrupted),
#                        with its resume command, if one exists; otherwise
#                        stay silent.
#
# Before either, an interactive Claude Code session on a fresh start gets the
# one-line program summary from lib/program-summary.py when anything is in
# flight, awaiting review or waiting on a decision (O-06).
#
# Plain stdout on a SessionStart hook is added as developer context for the
# model (confirmed against the published hook reference), so what this
# prints is read, not decorative.
#
# Fail-open: a missing llm-root checkout, a missing python3, an unreadable
# payload, or any picker failure prints nothing and exits 0. This hook never
# blocks a session. session-resume.py's own scan is bounded by its default
# --since window and --limit, and the program summary by its own timeout
# (PROGRAM_SUMMARY_TIMEOUT, default 3 seconds; about 0.3 seconds measured
# against a two day event log).
#
# root points at the source checkout that carries scripts/session-resume.py
# and hooks/lib/session-card.py; neither is deployed into a runtime's own
# directory (only *.sh hooks are), so this runs the source tree's own copy.
# AGENTRC_SOURCE names it outright, else LLM_ROOT. With neither, the hook
# stays silent.
#
# Scope, from the code review of WI-20:
#   - Claude Code only. The picker reads ~/.claude/projects and prints
#     claude --resume, so a Codex or Gemini session (recognized by where the
#     hook is installed, as hooks/prompt-capture.sh does) stays silent. The
#     work-item branch below is the one exception: it acts on every runtime.
#   - Only a fresh start. SessionStart also fires on resume, clear and
#     compact; orienting again then would re-inject the prompt mid-session.
#     A payload with no source is treated as a start.
#   - The session being started is never offered to itself: its id is passed
#     as --exclude-session, which also lets an interruption from minutes ago
#     be offered.
#   - The card is matched on the exact directory (--cwd), never on a name
#     that another project's name happens to contain.

set -euo pipefail
[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"

# Work-item sessions (WI-30, the design record,
# section tying-the-session-back). scripts/session-launch.py sets
# LLM_ROOT_WORK_ITEM; lib/tool-budget.py then appends the session's join line
# to work-item-sessions.jsonl and prints the work item, its goals and its
# budget limits. This runs before the runtime check below, so a Codex or
# Gemini session is joined too, under the runtime its install path names.
# The engine acts on a fresh start and on a resume, which continues the same
# launch, and
# skips clear and compact. Without a work item nothing
# here runs and the payload is read further down exactly as before.
payload_read=0
if [ -n "${LLM_ROOT_WORK_ITEM:-}" ]; then
  payload="$(cat 2>/dev/null || true)"
  payload_read=1
  case "$hook_dir" in
    */.codex/*) wi_runtime=codex ;;
    */.gemini/*) wi_runtime=gemini ;;
    */.cursor/*) wi_runtime=cursor ;;
    */.config/opencode/*) wi_runtime=opencode ;;
    *) wi_runtime=claude ;;
  esac
  if command -v python3 >/dev/null 2>&1 && [ -f "$hook_dir/lib/tool-budget.py" ]; then
    printf '%s' "$payload" | TOOL_BUDGET_RUNTIME="$wi_runtime" python3 "$hook_dir/lib/tool-budget.py" session-start 2>/dev/null || true
  fi
fi

case "$hook_dir" in
  */.codex/* | */.gemini/* | */.cursor/* | */.config/opencode/*) exit 0 ;;
esac

command -v python3 >/dev/null 2>&1 || exit 0

root="${AGENTRC_SOURCE:-${LLM_ROOT:-}}"
[ -n "$root" ] || exit 0
picker="$root/scripts/session-resume.py"
[ -f "$picker" ] || exit 0

[ "$payload_read" = 1 ] || payload="$(cat 2>/dev/null || true)"
fields="$(printf '%s' "$payload" | python3 -c '
import json, sys

try:
    data = json.loads(sys.stdin.read() or "{}")
except ValueError:
    data = {}
if not isinstance(data, dict):
    data = {}

def field(name):
    value = data.get(name)
    return value if isinstance(value, str) and "\n" not in value else ""

print(field("cwd"))
print(field("source"))
print(field("session_id"))
' 2>/dev/null || true)"
cwd="$(printf '%s\n' "$fields" | sed -n 1p)"
source_kind="$(printf '%s\n' "$fields" | sed -n 2p)"
session_id="$(printf '%s\n' "$fields" | sed -n 3p)"
[ -n "$cwd" ] || exit 0
case "$source_kind" in
  "" | startup) ;;
  *) exit 0 ;;
esac

home="${HOME%/}"
cwd="${cwd%/}"

# The program summary:
# one line, in any directory, with the runs in flight, the pull requests
# awaiting review and the items waiting on a decision, counted by
# lib/program-summary.py from program-status.py snapshot --json. It is for a
# person, so a headless launch (LLM_ROOT_HEADLESS, set by session-launch.py)
# prints nothing. The helper bounds the snapshot with its own timeout and
# prints nothing on any failure or when every count is zero.
summary_helper="$hook_dir/lib/program-summary.py"
if [ "${LLM_ROOT_HEADLESS:-0}" != "1" ] && [ -f "$summary_helper" ]; then
  summary="$(AGENTRC_SOURCE="$root" python3 "$summary_helper" line 2>/dev/null || true)"
  if [ -n "$summary" ]; then
    printf '%s\n' "$summary"
  fi
fi

if [ "$cwd" = "$home" ]; then
  # A launched session already has its project: the work item names it and
  # the block above printed its goals, so asking for a project contradicts
  # it (S-13). The launcher starts in the checkout, so this is the fallback.
  [ -z "${LLM_ROOT_WORK_ITEM:-}" ] || exit 0
  roster="$(python3 "$picker" --compact 2>/dev/null || true)"
  if [ -n "$roster" ]; then
    printf '%s\n\n' "$roster"
  fi
  # The open work items with a loadout, each with its launch command.
  launcher="$root/scripts/session-launch.py"
  if [ -f "$launcher" ]; then
    open_items="$(python3 "$launcher" --list-open --root "$root" 2>/dev/null || true)"
    if [ -n "$open_items" ]; then
      printf '%s\n\n' "$open_items"
    fi
  fi
  printf 'You are starting a session in the home directory. Pick a project (or ask which one) before running any tool.\n'
  exit 0
fi

# Without a session id the picker keeps its live-session filter, so a
# session still open elsewhere is not offered. Two calls rather than an
# optional-argument array: an empty array trips set -u on bash 3.2.
if [ -n "$session_id" ]; then
  card="$(python3 "$picker" --cwd "$cwd" --exclude-session "$session_id" --newest-interrupted 2>/dev/null || true)"
else
  card="$(python3 "$picker" --cwd "$cwd" --newest-interrupted 2>/dev/null || true)"
fi
case "$card" in
  "" | "No sessions found.") ;;
  *) printf '%s\n' "$card" ;;
esac
exit 0
