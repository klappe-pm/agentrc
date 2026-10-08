#!/usr/bin/env bash
# Shared runtime utilities for hook logging and secret scanning.
#
# Contains:
# - secret-scan helpers (ss_contains_secret / ss_match_label / ss_redact)
# - lightweight structured logger helpers (log_info / log_warn / log_error / log_debug)
#
# Hooks source this file to share one implementation for both logging and
# secret-detection behavior.

set -u

# --- shared logger --------------------------------------------------------------

_LOG_UTILS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_log_emit() {
  local level="$1"
  shift
  local ts msg
  ts="$(date +%Y-%m-%dT%H:%M:%S%z)"
  msg="$*"
  if command -v ss_redact >/dev/null 2>&1; then
    msg="$(ss_redact "$msg")"
  fi
  printf '[%s] %s %s\n' "$ts" "$level" "$msg" >&2
}

log_info() { _log_emit INFO "$*"; }
log_warn() { _log_emit WARN "$*"; }
log_error() { _log_emit ERROR "$*"; }
log_debug() { [ "${RUNTIME_DEBUG:-0}" = "1" ] && _log_emit DEBUG "$*" || true; }

# --- secret scan ---------------------------------------------------------------

# Secret / token-shaped value detection and redaction.
#
# Backs the token-shaped-values and no-secret-exposure rules ("never log or print token-shaped
# values" / "drop or redact captured content that carries a token-shaped
# value"). Any hook or lib that writes text to a log or generated file
# should route that text through ss_redact first.
#
# Public contract (when sourced):
#   ss_contains_secret <text>   # returns 0 if text holds a token-shaped
#                               # value, 1 otherwise
#   ss_match_label <text>       # returns 0 and prints matched label or 1
#   ss_redact <text>            # echoes text with each token-shaped
#                               # substring replaced by [REDACTED]

# --- pattern definitions -------------------------------------------------------
# One "label|extended-regex" entry per recognized secret shape. Add a new
# shape here as its own entry; do not fold shapes into a shared monolith
# regex. Matching is always case-insensitive (see _ss_grep_i below) so
# casing variants are still caught.

_SS_PATTERNS=(
  'private_key_block|-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'aws_access_key|AKIA[0-9A-Z]{16}'
  'github_fine_grained_pat|github_pat_[A-Za-z0-9_]{20,}'
  'github_token|gh[pousr]_[A-Za-z0-9]{20,}'
  'slack_token|xox[baprs]-[A-Za-z0-9-]{10,}'
  # The leading \b is load-bearing. Without it this fires on any ordinary word
  # ending in "sk" followed by a long hyphenated phrase: "risk-and-cost-of-
  # wrong-decision", "autodesk-management-console", "flask-websocket-server".
  # A scan of one 496 file corpus produced 33 matches, every one a false
  # positive of that shape. \b is zero width, so ss_redact still extracts only
  # the token and not the character before it.
  'sk_style_key|\bsk-[A-Za-z0-9_-]{20,}'
  'google_api_key|AIza[0-9A-Za-z_-]{20,}'
  'jwt|eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}'
  'bearer_token|Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{20,}'
  # A quoted value is a literal in any shape. An unquoted value must end at a
  # real boundary (space, quote, comma, semicolon, or a closing bracket) and
  # not run straight into an opening paren, which is what a function call
  # looks like (extract_keyspace(info), future_bigkeys.result()). That alone
  # is not enough: a dotted attribute reference (api.env.INTERNAL_TOKEN) ends
  # at a real boundary too, so _ss_generic_assignment_is_excluded rejects an
  # unquoted value that is nothing but a dotted chain of identifiers.
  'generic_assignment|[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*[[:space:]]*[=:][[:space:]]*(['\''"][A-Za-z0-9_./+=-]{16,}['\''"]|[A-Za-z0-9_./+=-]{16,}([]});,'\''"[:space:]]|$))'
)

# _ss_generic_assignment_is_excluded <matched-span>: true when a
# generic_assignment candidate's value is an unquoted, purely dot-separated
# chain of identifiers (api.env.INTERNAL_TOKEN, future_bigkeys.result) rather
# than a secret literal. A quoted value is never excluded here, whatever its
# shape. This is the second half of the narrowing the loose pattern
# above cannot express on its own (ERE has no negative lookahead), so it runs
# as a plain string check on the value the pattern already isolated.
_ss_generic_assignment_is_excluded() {
  local match="$1" value last
  value="${match#*[=:]}"
  value="$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//')"
  case "$value" in
    \'*|\"*) return 1 ;;
  esac
  # A value opening with // is the authority part of a URI whose scheme the
  # pattern just consumed as its keyword: secret://namespace/key is the
  # secret reference syntax of the secret-resolution rule, and a
  # reference is the opposite of a literal. The pattern reads "secret:" as
  # SECRET followed by a colon separator, so the URI shape lands here.
  case "$value" in
    //*) return 0 ;;
  esac
  # Drop the single trailing boundary character the pattern above consumed
  # (a space, quote, comma, semicolon, or closing bracket), so the shape
  # check below sees only the value itself.
  last="${value: -1}"
  case "$last" in
    ' '|$'\t'|','|';'|')'|'}'|']'|"'"|'"') value="${value%?}" ;;
  esac
  local dotted_re='^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$'
  [[ "$value" =~ $dotted_re ]]
}

# _ss_first_valid_match <text> <pattern> <label>: prints the first accepted
# match of pattern in text, honoring the generic_assignment exclusion above
# for that one label. Silent and returns nonzero when nothing is accepted.
_ss_first_valid_match() {
  local text="$1" pattern="$2" label="$3" match
  while IFS= read -r match; do
    [ -n "$match" ] || continue
    if [ "$label" = "generic_assignment" ] && _ss_generic_assignment_is_excluded "$match"; then
      continue
    fi
    printf '%s\n' "$match"
    return 0
  done < <(printf '%s' "$text" | grep -Eoi -- "$pattern")
  return 1
}

ss_contains_secret() {
  local text="${1-}"
  local entry pattern label
  for entry in "${_SS_PATTERNS[@]}"; do
    label="${entry%%|*}"
    pattern="${entry#*|}"
    if _ss_first_valid_match "$text" "$pattern" "$label" >/dev/null; then
      return 0
    fi
  done
  return 1
}

ss_match_label() {
  local text="${1-}"
  local entry pattern label
  for entry in "${_SS_PATTERNS[@]}"; do
    label="${entry%%|*}"
    pattern="${entry#*|}"
    if _ss_first_valid_match "$text" "$pattern" "$label" >/dev/null; then
      printf '%s\n' "$label"
      return 0
    fi
  done
  return 1
}

# _ss_redact_pem_blocks <text> <marker>: echoes text with every PRIVATE KEY
# PEM block (its BEGIN header through the matching END footer, or through
# the end of the text when no END footer follows) replaced by <marker>,
# and returns 0. The private_key_block entry in _SS_PATTERNS only matches
# the BEGIN header on its own; the per-pattern loop in _ss_redact_core
# below replaces just that matched span, leaving the base64 key body and
# the END footer untouched. Called only once _ss_redact_core's own case
# match has confirmed text carries a BEGIN...PRIVATE KEY----- span, so
# text with none of that shape never pays for this function's subshell.
# `-0777` slurps the input as one string and the `s` flag makes `.` match
# a real embedded newline (a file's own line breaks), while a JSON-escaped
# block flattened onto one line with a literal two-character `\n` escape
# matches the same `.*?` with no flag needed, since that shape was never a
# real newline to begin with.
#
# Without perl on PATH, or if it fails, this echoes text unchanged and
# returns 1: the caller then leaves private_key_block in the per-pattern
# loop below, so the BEGIN header alone still gets redacted exactly as it
# did before this whole-block redaction existed, rather than the run
# silently redacting nothing at all.
_ss_redact_pem_blocks() {
  local text="$1" marker="$2"
  command -v perl >/dev/null 2>&1 || { printf '%s' "$text"; return 1; }
  printf '%s' "$text" | PEM_MARKER="$marker" perl -0777 -pe \
    's/-----BEGIN[ A-Za-z]*PRIVATE\ KEY-----.*?(?:-----END[ A-Za-z]*PRIVATE\ KEY-----|\z)/$ENV{PEM_MARKER}/gis;'
}

# _ss_redact_core <text> <marker-mode>: shared implementation for ss_redact
# and ss_redact_labeled. marker-mode "plain" replaces each match with
# [REDACTED], the behavior ss_redact has always had; marker-mode "labeled"
# replaces each match with [REDACTED:<label>] instead, the shape
# the captured-content-redaction rule requires so a reviewer of a generated
# artifact knows what was found without ever seeing the value itself.
_ss_redact_core() {
  local text="${1-}" marker_mode="${2-plain}"
  local entry pattern label match replacement_match value last marker
  local pem_marker="[REDACTED]" pem_handled=1 new_text
  [ "$marker_mode" = "labeled" ] && pem_marker="[REDACTED:private_key_block]"
  case "$text" in
    *"-----BEGIN"*"PRIVATE"*"KEY-----"*)
      if new_text="$(_ss_redact_pem_blocks "$text" "$pem_marker")"; then
        text="$new_text"
        pem_handled=0
      fi
      ;;
  esac
  for entry in "${_SS_PATTERNS[@]}"; do
    label="${entry%%|*}"
    if [ "$label" = "private_key_block" ] && [ "$pem_handled" -eq 0 ]; then
      continue
    fi
    pattern="${entry#*|}"
    marker="[REDACTED]"
    [ "$marker_mode" = "labeled" ] && marker="[REDACTED:$label]"
    while IFS= read -r match; do
      [ -n "$match" ] || continue
      if [ "$label" = "generic_assignment" ] && _ss_generic_assignment_is_excluded "$match"; then
        continue
      fi
      replacement_match="$match"
      if [ "$label" = "generic_assignment" ]; then
        value="${match#*[=:]}"
        value="$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//')"
        case "$value" in
          \'*|\"*) ;;
          *)
            last="${match: -1}"
            case "$last" in
              ' '|$'\t'|','|';'|')'|'}'|']'|"'"|'"') replacement_match="${match%?}" ;;
            esac
            ;;
        esac
      fi
      text="${text//"$replacement_match"/$marker}"
    done < <(printf '%s' "$text" | grep -Eoi -- "$pattern")
  done
  printf '%s\n' "$text"
}

ss_redact() {
  _ss_redact_core "${1-}" plain
}

# ss_redact_labeled <text>: like ss_redact, but each match becomes
# [REDACTED:<kind>] rather than the unlabeled [REDACTED]. Used wherever a
# redacted copy is meant to be read later and the kind of value that was
# removed matters (the captured-content-redaction rule).
ss_redact_labeled() {
  _ss_redact_core "${1-}" labeled
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  mode="${1:-}"
  shift || true
  case "$mode" in
    redact)
      ss_redact "$*"
      ;;
    check)
      if ss_contains_secret "$*"; then
        echo "secret-shaped value detected"
        exit 0
      else
        echo "no secret-shaped value detected"
        exit 1
      fi
      ;;
    label)
      if ss_match_label "$*"; then
        exit 0
      else
        exit 1
      fi
      ;;
    label-stdin)
      input="$(cat)"
      if ss_match_label "$input"; then
        exit 0
      else
        exit 1
      fi
      ;;
    redact-labeled-stdin)
      input="$(cat)"
      ss_redact_labeled "$input"
      ;;
    *)
      echo "usage: secret-scan.sh redact|check|label <text> | label-stdin | redact-labeled-stdin" >&2
      exit 64
      ;;
  esac
fi
