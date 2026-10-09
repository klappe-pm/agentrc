#!/usr/bin/env bash
# Unit tests for lib/secret-scan.sh: ss_contains_secret / ss_redact against
# fixture strings covering each recognized pattern class, plus true
# negatives that must never be flagged.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../agentrc/data/hooks/lib" && pwd)"
# shellcheck source=secret-scan.sh
source "$DIR/secret-scan.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); }
bad() { FAIL=$((FAIL + 1)); printf '  secret-scan.test FAIL  %s\n' "$1" >&2; }

# assert_secret <label> <text>: must be flagged.
assert_secret() {
  local label="$1" text="$2"
  if ss_contains_secret "$text"; then ok; else bad "$label: expected secret to be detected"; fi
}

# assert_clean <label> <text>: must NOT be flagged.
assert_clean() {
  local label="$1" text="$2"
  if ss_contains_secret "$text"; then bad "$label: expected no secret, but one was flagged"; else ok; fi
}

# --- true positives: one per pattern class ----------------------------------
#
# Every fixture is ASSEMBLED AT RUNTIME from the prefix variables below, and no
# line in this file may carry a complete token-shaped value.
#
# The reason is circularity: scripts/git-hooks/check-staged-secrets.sh runs this
# same detector over staged content, so a literal fixture makes the detector's
# own test suite unstageable and blocks every commit that touches this file.
# Interpolating a variable breaks the literal run at the `$`, which is outside
# every pattern's character class, while the assembled string still exercises
# the pattern exactly.
#
# Same technique as prose-detect.py, which builds forbidden dashes with chr()
# so its source carries no literal for its own guard to trip on.
#
# When you add a fixture, assemble it the same way, then confirm the file is
# still clean:
#   bash hooks/lib/guard-utils.sh check "$(cat hooks/lib/secret-scan.test.sh)"

_AKIA="AKIA"
_GH="gh"
_GHPAT="github_pat"
_SK="sk"
_XOX="xox"
_AIZA="AIza"
_EYJ="eyJ"
_BEARER="Bearer"
_PEM_OPEN="-----BEGIN"
_PEM_TAIL="PRIVATE KEY-----"
_V20="abcdefghij1234567890"
_V24="abcdefghijklmnop12345678"
_VPW="superSecretValue123456"

assert_secret "aws_access_key" "export AWS_ACCESS_KEY_ID=${_AKIA}ABCDEFGHIJKLMNOP"
assert_secret "github_token_ghp" "auth: ${_GH}p_abcdefghijklmnopqrstuv"
assert_secret "github_token_ghs" "GITHUB_TOKEN=${_GH}s_ABCDEFGHIJKLMNOPQRST1234"
assert_secret "sk_style_key_anthropic" "ANTHROPIC_API_KEY=${_SK}-ant-api03-abcdefghijklmnopqrstuvwx"
assert_secret "sk_style_key_openai" "OPENAI_API_KEY=${_SK}-abcdefghijklmnopqrstuvwxyz123456"
assert_secret "generic_KEY_assign" "API_KEY=${_V20}"
assert_secret "generic_TOKEN_assign" "SLACK_TOKEN: ${_V24}"
assert_secret "generic_SECRET_assign" "CLIENT_SECRET=${_V24}"
assert_secret "generic_PASSWORD_assign" "DB_PASSWORD: ${_VPW}"
assert_secret "generic_api_key_lowercase" "api_key=${_V20}"

# --- superset shapes folded in from the two secret-guard hooks --------------

assert_secret "private_key_block" "${_PEM_OPEN} RSA ${_PEM_TAIL}"$'\nMIIEpAIBAAKCAQEA'
assert_secret "private_key_block_openssh" "${_PEM_OPEN} OPENSSH ${_PEM_TAIL}"
assert_secret "github_fine_grained_pat" "${_GHPAT}_11ABCDEFG0123456789012345678901234567890123456789012345"
assert_secret "slack_token_xoxb" "SLACK_TOKEN=${_XOX}b-1234567890-abcdefghijklmnop"
assert_secret "google_api_key" "key: ${_AIZA}SyD-abcdefghijklmnopqrstuvwxyz0123"
assert_secret "jwt" "auth: ${_EYJ}hbGciOiJIUzI1NiJ9.${_EYJ}zdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ_abcdefg"
assert_secret "bearer_token" "Authorization: ${_BEARER} abcdefghijklmnopqrstuvwxyz0123456789"

# --- true negatives: substrings and short/incomplete shapes must not match --

assert_clean "keyboard_word" "I bought a new keyboard for my desk at home"
assert_clean "tokenizer_word" "the tokenizer splits text into tokens for the model"
assert_clean "ordinary_log_line" "worktree guard allowed the checkout onto main"
assert_clean "monkeypatch_word" "this test relies on monkeypatching the clock"
assert_clean "short_generic_value" "KEY=short1"
assert_clean "aws_like_but_short" "AKIASHORT"
assert_clean "gh_prefix_too_short" "ghp_short"
assert_clean "sk_prefix_too_short" "sk-short"
# The sk- pattern needs a leading word boundary. Without it any ordinary word
# ending in "sk" followed by a long hyphenated phrase matches. A scan of one
# 496 file corpus produced 33 such matches and zero real keys.
assert_clean "sk_inside_risk_word" "risk-and-cost-of-wrong-decision"
assert_clean "sk_inside_autodesk_word" "autodesk-management-console-access"
assert_clean "sk_inside_flask_word" "flask-websocket-server-configuration"
assert_clean "sk_inside_word_in_path" "docs/ds-risk-and-cost-of-wrong-decision.md"
assert_clean "var_reference_not_literal" 'TOKEN=${GH_TOKEN}'
assert_clean "op_reference_not_literal" "TOKEN=op://vault/item/field"
_URI_SCHEME="secret"
_URI_REF="${_URI_SCHEME}://github/default"
assert_clean "logical_reference_not_literal" "${_URI_REF}"
assert_clean "assigned_logical_reference" "TOKEN=${_URI_REF}"
assert_clean "quoted_logical_reference" "\"GH_TOKEN\": \"${_URI_REF}\""
assert_clean "logical_reference_sentence" "Use ${_URI_REF}."
assert_secret "generic_slash_prefixed_value" "API_KEY=/${_V20}"
assert_secret "reference_with_real_assignment" "${_URI_REF} API_KEY=${_V20}"
assert_secret "reference_with_query_assignment" "${_URI_REF}?token=${_V20}"
assert_secret "reference_with_embedded_token" "${_URI_SCHEME}://github/${_GH}p_abcdefghijklmnopqrstuv"
if ss_match_label "${_URI_REF}" >/dev/null; then
  bad "logical reference: label must be empty"
else ok; fi
reference_redacted="$(ss_redact "${_URI_REF} API_KEY=${_V20}")"
case "$reference_redacted" in
  *"${_URI_REF}"*"[REDACTED]"*) ok ;;
  *) bad "logical reference: preserve reference and redact actual assignment" ;;
esac
assert_clean "bearer_word_no_token" "the bearer of this note may enter"
assert_clean "jwt_prefix_too_short" "eyJ.a.b"
assert_clean "slack_prefix_too_short" "xoxb-short"

# --- F-22: generic_assignment narrowing --------------------------------------
# the design record. Fragments
# below reproduce the shapes found in skills/use-railway without quoting a
# complete literal that would itself trip this same detector at commit time.
_DOT1="api""."
_DOT2="env""."
_TOK="INTERNAL""_TOKEN"
_FN1="future_bigkeys""."
_FN2="result"
_FN3="extract_keyspace"

# A dotted attribute reference: references/iac.md line 108,
#   API_TOKEN: api.env.INTERNAL_TOKEN,
assert_clean "dotted_attribute_reference" "API_TOKEN: ${_DOT1}${_DOT2}${_TOK},"

# A secret:// URI is a reference, never a literal: scripts/credentials.py
# line 58 names the syntax in an error message, and the pattern reads
# "secret:" as SECRET plus a colon separator with //namespace/key as the value.
assert_clean "secret_uri_reference" "raise ConfigurationError(\"Invalid secret URI; use secret://namespace/key.\")"
assert_clean "secret_uri_bare" "secret://github/api-automation"
# A function call result assignment: scripts/analyze-redis.py line 903,
#   bigkeys_result = future_bigkeys.result()
assert_clean "function_call_result_assignment" "bigkeys_result = ${_FN1}${_FN2}()"
# A bare function call assignment: scripts/analyze-redis.py line 921,
#   result.total_keys = extract_keyspace(info)
assert_clean "bare_function_call_assignment" "result.total_keys = ${_FN3}(info)"
# The same value, quoted, is still a literal and still matches.
assert_secret "quoted_dotted_looking_value" "API_TOKEN=\"${_DOT1}${_DOT2}${_TOK}\""
# A secret literal passed as a call keyword argument still matches: the
# boundary that rejects a call is the open paren right after the value, not
# a closing one.
assert_secret "secret_as_call_kwarg" "connect(TOKEN=${_V20})"

# --- ss_match_label: reports which shape tripped, empty + rc1 when clean -----

lbl="$(ss_match_label "export AWS_ACCESS_KEY_ID=${_AKIA}ABCDEFGHIJKLMNOP")"
[ "$lbl" = "aws_access_key" ] && ok || bad "ss_match_label: expected aws_access_key, got '$lbl'"
lbl="$(ss_match_label "auth: ${_EYJ}hbGciOiJIUzI1NiJ9.${_EYJ}zdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ_abcdefg")"
[ "$lbl" = "jwt" ] && ok || bad "ss_match_label: expected jwt, got '$lbl'"
if ss_match_label "just some ordinary prose about keyboards" >/dev/null; then
  bad "ss_match_label: clean text should return nonzero"
else ok; fi

# --- redaction: [REDACTED] replaces the matched span, leaves the rest intact -

out="$(ss_redact "export API_KEY=${_V20} then continue")"
case "$out" in
  *"[REDACTED]"*"then continue") ok ;;
  *) bad "ss_redact: expected [REDACTED] followed by trailing text, got: $out" ;;
esac
case "$out" in
  *"${_V20}"*) bad "ss_redact: raw secret value leaked into output: $out" ;;
  *) ok ;;
esac

out_space="$(ss_redact "TOKEN=${_V20} next")"
[ "$out_space" = "[REDACTED] next" ] && ok || bad "ss_redact: expected trailing space to remain, got: $out_space"
out_comma="$(ss_redact "TOKEN=${_V20},next")"
[ "$out_comma" = "[REDACTED],next" ] && ok || bad "ss_redact: expected trailing comma to remain, got: $out_comma"
out_bracket="$(ss_redact "[TOKEN=${_V20}]")"
[ "$out_bracket" = "[[REDACTED]]" ] && ok || bad "ss_redact: expected closing bracket to remain, got: $out_bracket"

out2="$(ss_redact "I like my keyboard and the tokenizer works fine")"
[ "$out2" = "I like my keyboard and the tokenizer works fine" ] && ok || bad "ss_redact: must not touch clean text, got: $out2"

# Issue #312: ss_redact must remove the whole PEM block, not just the BEGIN
# header. Built from pieces (real key material is never used in a test
# fixture): a synthetic body line and the matching END footer, assembled at
# runtime the same way the header pieces above are.
_PEM_BODY="MIIEpAIBAAKCAQEAdummybase64bodylinefortest1234567890"
_PEM_CLOSE="-----END"

pem_multiline="${_PEM_OPEN} RSA ${_PEM_TAIL}"$'\n'"${_PEM_BODY}"$'\n'"${_PEM_CLOSE} RSA ${_PEM_TAIL}"
pem_redacted="$(ss_redact "$pem_multiline")"
case "$pem_redacted" in
  *"$_PEM_BODY"*) bad "private_key_block redact: key body leaked (real newline): $pem_redacted" ;;
  *) ok ;;
esac
case "$pem_redacted" in
  *"${_PEM_CLOSE} RSA ${_PEM_TAIL}"*) bad "private_key_block redact: END footer leaked (real newline): $pem_redacted" ;;
  *) ok ;;
esac
[ "$pem_redacted" = "[REDACTED]" ] && ok || bad "private_key_block redact: expected the whole block replaced, got: $pem_redacted"

# The same block flattened onto one line with a JSON-style literal \n
# escape (a captured value inside a JSON string), rather than a real
# newline character.
pem_escaped="${_PEM_OPEN} ${_PEM_TAIL}\\n${_PEM_BODY}\\n${_PEM_CLOSE} ${_PEM_TAIL}"
pem_escaped_redacted="$(ss_redact "$pem_escaped")"
case "$pem_escaped_redacted" in
  *"$_PEM_BODY"*) bad "private_key_block redact: key body leaked (escaped newline): $pem_escaped_redacted" ;;
  *) ok ;;
esac
case "$pem_escaped_redacted" in
  *"${_PEM_CLOSE} ${_PEM_TAIL}"*) bad "private_key_block redact: END footer leaked (escaped newline): $pem_escaped_redacted" ;;
  *) ok ;;
esac
case "$pem_escaped_redacted" in
  "[REDACTED]"*) ok ;;
  *) bad "private_key_block redact: expected marker at the start, got: $pem_escaped_redacted" ;;
esac

pem_labeled="$(ss_redact_labeled "$pem_multiline")"
[ "$pem_labeled" = "[REDACTED:private_key_block]" ] && ok || bad "private_key_block redact_labeled: expected the whole block replaced with a labeled marker, got: $pem_labeled"

out3="$(ss_redact "${_AKIA}ABCDEFGHIJKLMNOP and ${_GH}p_abcdefghijklmnopqrstuv both leaked")"
case "$out3" in
  *"${_AKIA}ABCDEFGHIJKLMNOP"*) bad "ss_redact: aws key leaked: $out3" ;;
  *) ok ;;
esac
case "$out3" in
  *"${_GH}p_abcdefghijklmnopqrstuv"*) bad "ss_redact: github token leaked: $out3" ;;
  *) ok ;;
esac
case "$out3" in
  *"[REDACTED]"*"[REDACTED]"*) ok ;;
  *) bad "ss_redact: expected two distinct redactions, got: $out3" ;;
esac

# --- ss_redact_labeled: [REDACTED:<kind>] replaces the matched span ---------
# the captured-content-redaction rule requires the label on the marker so a
# reviewer of the archive knows what shape was found without seeing it.

labeled_out="$(ss_redact_labeled "export API_KEY=${_V20} then continue")"
case "$labeled_out" in
  *"[REDACTED:generic_assignment]"*"then continue") ok ;;
  *) bad "ss_redact_labeled: expected labeled marker followed by trailing text, got: $labeled_out" ;;
esac
case "$labeled_out" in
  *"${_V20}"*) bad "ss_redact_labeled: raw secret value leaked into output: $labeled_out" ;;
  *) ok ;;
esac

labeled_aws="$(ss_redact_labeled "${_AKIA}ABCDEFGHIJKLMNOP and ${_GH}p_abcdefghijklmnopqrstuv both leaked")"
case "$labeled_aws" in
  *"[REDACTED:aws_access_key]"*"[REDACTED:github_token]"*) ok ;;
  *) bad "ss_redact_labeled: expected two distinct labeled redactions, got: $labeled_aws" ;;
esac

labeled_clean="$(ss_redact_labeled "I like my keyboard and the tokenizer works fine")"
[ "$labeled_clean" = "I like my keyboard and the tokenizer works fine" ] && ok || bad "ss_redact_labeled: must not touch clean text, got: $labeled_clean"

labeled_ref="$(ss_redact_labeled "${_URI_REF} API_KEY=${_V20}")"
case "$labeled_ref" in
  *"${_URI_REF}"*"[REDACTED:generic_assignment]"*) ok ;;
  *) bad "ss_redact_labeled: preserve reference and label the actual assignment, got: $labeled_ref" ;;
esac

# ss_redact itself is unchanged by the labeled variant existing.
unchanged="$(ss_redact "export API_KEY=${_V20} then continue")"
case "$unchanged" in
  *"[REDACTED]"*"then continue") ok ;;
  *) bad "ss_redact: unlabeled output must be unchanged by ss_redact_labeled existing, got: $unchanged" ;;
esac

# --- CLI: redact-labeled-stdin -----------------------------------------------

cli_labeled="$(printf 'TOKEN=%s next' "${_V20}" | bash "$DIR/secret-scan.sh" redact-labeled-stdin)"
[ "$cli_labeled" = "[REDACTED:generic_assignment] next" ] && ok || bad "redact-labeled-stdin: expected labeled marker, got: $cli_labeled"

if [ "$FAIL" -gt 0 ]; then
  printf 'secret-scan.test: FAIL (%d passed, %d failed)\n' "$PASS" "$FAIL" >&2
  exit 1
fi
printf 'secret-scan.test: passed (%d cases)\n' "$PASS"
