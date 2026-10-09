#!/usr/bin/env bash
# UserPromptSubmit hook: capture every prompt into the project's prompt store.
#
# Hands the raw hook payload to lib/prompt-capture.py, which redacts
# token-shaped values ([REDACTED:<kind>], per rules/captured-content-redaction),
# refactors the prompt deterministically into a structured LLM prompt, and
# stores it as a Markdown note plus derived SQLite and JSON indexes under
# <project git toplevel>/.docs/prompts/<category>/. A prompt sent from a
# directory with no git repository, or from an Obsidian vault, goes to
# PROMPT_CAPTURE_FALLBACK_ROOT when it is set, else is skipped, never routed
# into a store nobody chose. A project named in the source tree's
# projects-root/public-targets.json is a public repository and receives no
# capture at all.
#
# Contract:
# - Silent on stdout. UserPromptSubmit stdout is injected into the model's
#   context on Claude Code and Codex, so this hook prints nothing.
# - Fail-open. Any failure costs one missing note, never a blocked prompt.
#   Errors are redacted through lib/log.sh and appended to
#   prompt-capture.errors.log next to the telemetry so they stay visible;
#   RUNTIME_DEBUG=1 sends them to stderr instead.
# - Deterministic. No model call runs in this path; the opt-in `refine`
#   subcommand of lib/prompt-capture.py is the only place one can.
#
# Runtime label: PROMPT_CAPTURE_RUNTIME when set, else inferred from where the
# adapter deployed this script (~/.codex/hooks -> codex, ~/.gemini/hooks ->
# gemini, ~/.cursor/hooks -> cursor, ~/.config/opencode/hooks -> opencode),
# else claude.
#
# Env: RUNTIME_HOOKS_DISABLE=1 or PROMPT_CAPTURE_DISABLE=1 turns it off.
#      PROMPT_CAPTURE_ROOT overrides the store root.
#      PROMPT_CAPTURE_FALLBACK_ROOT is consulted only when there is no git
#      repository or the repository is an Obsidian vault; Claude Code sets it
#      from runtime_settings.claude.settings in components.json.
#      PROMPT_CAPTURE_TELEMETRY_DIR overrides ~/.agent-hooks/telemetry.

set -euo pipefail
[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0
[ "${PROMPT_CAPTURE_DISABLE:-0}" = "1" ] && exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
[ -f "$HOOK_DIR/lib/log.sh" ] && source "$HOOK_DIR/lib/log.sh"
command -v log_warn >/dev/null 2>&1 || log_warn() { printf '%s\n' "$*" >&2; }
ENGINE="$HOOK_DIR/lib/prompt-capture.py"
[ -f "$ENGINE" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

runtime="${PROMPT_CAPTURE_RUNTIME:-}"
if [ -z "$runtime" ]; then
  case "$HOOK_DIR" in
    */.codex/*) runtime=codex ;;
    */.gemini/*) runtime=gemini ;;
    */.cursor/*) runtime=cursor ;;
    */.config/opencode/*) runtime=opencode ;;
    *) runtime=claude ;;
  esac
fi

TELEMETRY_DIR="${PROMPT_CAPTURE_TELEMETRY_DIR:-${HOME:-/tmp}/.agent-hooks/telemetry}"
ERR_LOG="$TELEMETRY_DIR/prompt-capture.errors.log"
mkdir -p "$TELEMETRY_DIR" 2>/dev/null || ERR_LOG=/dev/null

# Engine stderr is captured, redacted through the shared logger, and appended
# to the error log (or echoed under RUNTIME_DEBUG=1). Never blocks the prompt.
err="$(printf '%s' "$INPUT" | python3 "$ENGINE" capture --quiet --stdin-json --runtime "$runtime" 2>&1 >/dev/null || true)"
if [ -n "$err" ]; then
  if [ "${RUNTIME_DEBUG:-0}" = "1" ]; then
    log_warn "prompt-capture: $(printf '%s' "$err" | tail -n 1)"
  else
    log_warn "prompt-capture: $(printf '%s' "$err" | tail -n 1)" 2>>"$ERR_LOG"
  fi
fi
exit 0
