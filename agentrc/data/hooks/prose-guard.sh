#!/usr/bin/env bash
# PreToolUse(Write|Edit|NotebookEdit) hook. Hard deny on forbidden dashes in
# any authored content and on hard-wrapped prose in authored Markdown.
#
# One process replaces the former no-emdash-guard.sh and no-hardwrap-guard.sh,
# which parsed the same payload twice and ran the same detector twice on every
# write. The two checks keep their own rules, messages and escape hatches.
#
# Dash check (the no-em-dash rule): em-dash (U+2014) and horizontal bar
# (U+2015) are denied unconditionally; an en-dash (U+2013) is denied only as a
# connector with a space on either side, so a bare numeric range passes. Runs
# on every file type, over Write's content, Edit's new_string and
# NotebookEdit's new_source. Escape hatch: ALLOW_EMDASH=1.
#
# Hard-wrap check (the no-hardwrapped-writing rule): two adjacent non-blank
# lines that form one paragraph or one list item are a violation regardless of
# column width; headings, table rows, horizontal rules, blockquotes, separate
# list items and fenced code are never flagged. Runs only for *.md files. The
# detector's payload mode reads an Edit's new_string in the context of the
# target file, so a fragment that starts inside a code fence is not prose.
# Escape hatch: ALLOW_HARDWRAP=1.
#
# Redundant-label check (the no-redundant-labels rule): a table cell that
# opens with a label equal to or a prefix of its column header, or a list item
# that opens with its heading's noun as a label, is denied. Runs only for *.md
# files, with an Edit read in the context of the target file so the heading or
# header above the fragment is known. Escape hatch: ALLOW_REDUNDANT_LABELS=1.
#
# All checks use hooks/lib/prose-detect.py. The dash and wrap checks share it
# with the git pre-commit backstop scripts/git-hooks/check-staged-prose.sh, so
# exactly one detection implementation exists.
#
# Emits a single JSON object on stdout per Claude Code's hook protocol.

set -u
trap 'exit 0' ERR
[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$HOOK_DIR/lib/log.sh"
# shellcheck source=lib/guard-log.sh
[ -f "$HOOK_DIR/lib/guard-log.sh" ] && source "$HOOK_DIR/lib/guard-log.sh"
command -v guard_log_event >/dev/null 2>&1 || guard_log_event() { :; }

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || exit 0

# One python pass extracts the target path and every field carrying newly
# authored text, NUL-separated so real newlines inside the text survive.
fields="$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
except Exception:
    sys.exit(0)
ti = d.get("tool_input") or {}
print(ti.get("file_path") or "")
parts = []
for k in ("content", "new_string", "new_source"):
    v = ti.get(k)
    if isinstance(v, str):
        parts.append(v)
print("\x00".join(parts), end="")
' 2>/dev/null || true)"
file_path="$(printf '%s\n' "$fields" | head -n1)"
text="$(printf '%s\n' "$fields" | tail -n +2)"
text="${text//$'\x00'/$'\n'}"

[ -n "$text" ] || exit 0

deny() {
  local label="$1" msg="$2"
  local rule="no-em-dash"
  case "$label" in
    wrapped:*) rule="no-hardwrapped-writing" ;;
    labeled:*) rule="no-redundant-labels" ;;
  esac
  log_warn "prose-guard: denied write ($label)"
  guard_log_event prose-guard "$rule" deny "$label" "$payload"
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
    "$(printf '%s' "$msg" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"
  exit 0
}

if [ "${ALLOW_EMDASH:-0}" != "1" ]; then
  reason="$(printf '%s' "$text" | python3 "$HOOK_DIR/lib/prose-detect.py" emdash 2>/dev/null || true)"
  if [ -n "$reason" ]; then
    deny "$reason" "Forbidden dash in authored content ($reason). The no-em-dash rule is absolute: never use em-dashes. Rewrite with a comma, colon, period, parentheses, or a new sentence. (A bare numeric range joined by an en-dash is fine; set ALLOW_EMDASH=1 only to quote source text verbatim.)"
  fi
fi

if [ "${ALLOW_HARDWRAP:-0}" != "1" ]; then
  case "$file_path" in
    *.md)
      reason="$(printf '%s' "$payload" | python3 "$HOOK_DIR/lib/prose-detect.py" hardwrap-payload 2>/dev/null || true)"
      if [ -n "$reason" ]; then
        deny "wrapped: $reason in $file_path" "Hard-wrapped prose in $(basename "$file_path") (line starting \"$reason\"). The no-hardwrapped-writing rule is absolute: write each paragraph and list item as one line and let the editor soft-wrap. Break lines only at blank lines between blocks, headings, separate list items, table rows, code fences, or blockquotes. Set ALLOW_HARDWRAP=1 only to quote source text verbatim."
      fi
      ;;
  esac
fi

if [ "${ALLOW_REDUNDANT_LABELS:-0}" != "1" ]; then
  case "$file_path" in
    *.md)
      reason="$(printf '%s' "$payload" | python3 "$HOOK_DIR/lib/prose-detect.py" labels-payload 2>/dev/null || true)"
      if [ -n "$reason" ]; then
        deny "labeled: $reason in $file_path" "Redundant label in $(basename "$file_path") (line \"$reason\"). The no-redundant-labels rule: a table cell never opens with a label that repeats its column header, and a list item never opens with its heading's noun as a label. Delete the label and keep the content. Set ALLOW_REDUNDANT_LABELS=1 only to quote source text verbatim."
      fi
      ;;
  esac
fi

exit 0
