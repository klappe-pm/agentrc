#!/usr/bin/env bash
# Behavioral tests for no-attribution-guard.sh
#
# PreToolUse hook: hard-denies a write, an authoring shell command, or a GitHub
# MCP call whose text credits a coding agent. Human co-authors, ordinary prose,
# and non-authoring shell commands pass. Agent names are assembled from
# fragments so this file carries no literal attribution line for the live guard
# to block on.

set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)/no-attribution-guard.sh"

AGENT="Cla""ude"
AGENT_LOWER="cla""ude"
VENDOR="anthro""pic.com"
REPO_ORG="anthro""pics"

PASS=0
FAIL=0

# WI-16/F-21: isolate every deny firing from the operator's live telemetry.
# Every run gets GUARD_LOG_DIR and a fixture HOME, so neither the explicit log
# directory nor the $HOME fallback can reach the real one. Review cluster F2:
# this test used to count lines in the live ~/.agent-hooks telemetry file
# before and after, so any other session writing a guard event there during
# the run failed it; the proof of isolation is now the fixture HOME staying
# empty while GLDIR receives the deny records.
GLDIR="$(mktemp -d "${TMPDIR:-/tmp}/no-attribution-guard-log-XXXXXX")"
FIXTURE_HOME="$(mktemp -d "${TMPDIR:-/tmp}/no-attribution-guard-home-XXXXXX")"
trap 'rm -rf "$GLDIR" "$FIXTURE_HOME"' EXIT

# assert_guard: feed a hook payload and expect "deny" or "allow"
assert_guard() {
  local label="$1" json="$2" expect="$3"
  local out got
  out=$(printf '%s' "$json" | HOME="$FIXTURE_HOME" RUNTIME_HOOKS_DISABLE=0 ALLOW_AGENT_ATTRIBUTION=0 GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
  case "$out" in *'"permissionDecision":"deny"'*) got=deny ;; *) got=allow ;; esac
  if [ "$got" = "$expect" ]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  no-attribution-guard.sh FAIL  %-44s expected=%-5s got=%s\n' "$label" "$expect" "$got" >&2
  fi
}

assert_guard "generated-with footer in Write" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"Generated with ${AGENT} Code\"}}" deny
assert_guard "session permalink in Write" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"see https://cla""ude.ai/code/session_01Ab\"}}" deny
assert_guard "co-author trailer in Edit" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"new_string\":\"Co-Authored-By: ${AGENT} <noreply@${VENDOR}>\"}}" deny
assert_guard "robot byline in Notebook cell" \
  "{\"tool_name\":\"NotebookEdit\",\"tool_input\":{\"new_source\":\"$(printf '\xf0\x9f\xa4\x96') done\"}}" deny
assert_guard "trailer in git commit command" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m 'fix: x' -m 'Co-Authored-By: ${AGENT} <a@b>'\"}}" deny
assert_guard "footer in pull request body" \
  "{\"tool_name\":\"mcp__github__create_pull_request\",\"tool_input\":{\"body\":\"Adds a guard.\n\nGenerated with ${AGENT} Code\"}}" deny
assert_guard "human co-author allowed" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"new_string\":\"Co-Authored-By: Dana Lee <d@example.com>\"}}" allow
assert_guard "clean commit allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m 'fix: correct the staging path'\"}}" allow
assert_guard "grep for the string allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"grep -rn 'Generated with ${AGENT} Code' .\"}}" allow
assert_guard "clean prose allowed" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"The hook denies attribution footers.\"}}" allow
assert_guard "session-link docs frontmatter allowed" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/repo/docs/ideas/note.md\",\"content\":\"---\ntags: []\nsession-link: https://cla""ude.ai/code/session_01Ab\n---\n# note\"}}" allow
# Review cluster F2: the provenance exception is the frontmatter of a Markdown
# file under docs/ and nothing else.
assert_guard "session-link frontmatter outside docs denied" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/repo/notes/note.md\",\"content\":\"---\ntags: []\nsession-link: https://cla""ude.ai/code/session_01Ab\n---\n# note\"}}" deny
assert_guard "session-link with no file_path denied" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"---\ntags: []\nsession-link: https://cla""ude.ai/code/session_01Ab\n---\n# note\"}}" deny
assert_guard "session-link in a commit message denied" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m 'fix: x' -m '\nsession-link: https://cla""ude.ai/code/session_01Ab'\"}}" deny
assert_guard "session-link in a pull request body denied" \
  "{\"tool_name\":\"mcp__github__create_pull_request\",\"tool_input\":{\"body\":\"Adds a guard.\n\nsession-link: https://cla""ude.ai/code/session_01Ab\"}}" deny
assert_guard "session link in body still denied" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"new_string\":\"session-link: \\\"\\\"\n\nSource: https://cla""ude.ai/code/session_01Ab\"}}" deny
assert_guard "empty tool_input allowed" "{\"tool_name\":\"Write\",\"tool_input\":{}}" allow

# F-22: the
# caffeinate ADR's citation, written as a Markdown link, must pass through
# the real Write path.
assert_guard "repo issue citation through a real Write" \
  "{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"see [${AGENT} Code issue 21432](https://github.com/${REPO_ORG}/${AGENT_LOWER}-code/issues/21432) for details\"}}" \
  allow

# 2026-10-08: a `## sources` entry citing a third party's
# page about a product must pass through a real Edit; a vendor host must not.
assert_guard "third-party sources entry through a real Edit" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/repo/leads/company/profile.md\",\"old_string\":\"x\",\"new_string\":\"## sources\n\n- [GitLab deepens integration with Anthropic's ${AGENT} models](https://www.nasdaq.com/press-release/gitlab-deepens-integration-${REPO_ORG}-${AGENT_LOWER}-models-accelerate-secure-software), Nasdaq, 2026-10-08, partnership scope.\"}}" \
  allow
assert_guard "vendor host sources entry through a real Edit" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/repo/leads/company/profile.md\",\"old_string\":\"x\",\"new_string\":\"## sources\n\n- [${AGENT} Code](https://${AGENT_LOWER}.ai/code), vendor, 2026-10-08, product page.\"}}" \
  deny

# WI-55 (A-03): a body named by file is published text. The fixture is written
# at run time with the agent name built from fragments.
BODY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/no-attribution-guard-body-XXXXXX")"
printf 'Adds a guard.\n\nGenerated with %s Code\n' "$AGENT" > "$BODY_DIR/body.md"
printf 'Adds a guard.\n' > "$BODY_DIR/clean.md"
assert_guard "footer in a gh pr create --body-file" \
  "{\"tool_name\":\"Bash\",\"cwd\":\"$BODY_DIR\",\"tool_input\":{\"command\":\"gh pr create --title t --body-file body.md\"}}" deny
assert_guard "clean gh pr create --body-file allowed" \
  "{\"tool_name\":\"Bash\",\"cwd\":\"$BODY_DIR\",\"tool_input\":{\"command\":\"gh pr create --title t --body-file clean.md\"}}" allow
# PR 294 adversarial review: a publishing command whose body cannot be read
# in full is denied, and the reason names the safe form instead of claiming
# attribution was found.
out=$(printf '%s' "{\"tool_name\":\"Bash\",\"cwd\":\"$BODY_DIR\",\"tool_input\":{\"command\":\"cat clean.md | gh pr create --title t --body-file -\"}}" | HOME="$FIXTURE_HOME" RUNTIME_HOOKS_DISABLE=0 ALLOW_AGENT_ATTRIBUTION=0 GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
case "$out" in
  *'"permissionDecision":"deny"'*'--body-file'*) PASS=$((PASS+1)) ;;
  *) FAIL=$((FAIL+1)); printf '  no-attribution-guard.sh FAIL  unverifiable body must deny with the safe form, got: %s\n' "$out" >&2 ;;
esac
assert_guard "technical mention in a commit allowed" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m 'feat: adapter bu""ilt with Open""AI SDK'\"}}" allow
# A-03: any MCP server's GitHub writer is scanned, not only mcp__github__.
assert_guard "footer in another server's create_pr" \
  "{\"tool_name\":\"mcp__claude_ai_Mermaid_Chart__create_pr\",\"tool_input\":{\"body\":\"Generated with ${AGENT} Code\"}}" deny
assert_guard "non-message tool allowed" \
  "{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"$BODY_DIR/body.md\"}}" allow
rm -rf "$BODY_DIR"

# A-03: the guard is registered once, on every tool, so a GitHub writer from any
# MCP server reaches it and no tool runs it twice.
HOOKS_JSON="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)/hooks.json"
registration="$(python3 -c '
import json, sys
groups = json.load(open(sys.argv[1]))["PreToolUse"]
hits = [g.get("matcher", "") for g in groups for h in g.get("hooks", []) if h.get("command", "").endswith("/no-attribution-guard.sh")]
print("ok" if hits in (["" ], ["*"]) else "bad " + repr(hits))
' "$HOOKS_JSON")"
if [ "$registration" = "ok" ]; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  printf '  no-attribution-guard.sh FAIL  registration on every tool: %s\n' "$registration" >&2
fi

# ALLOW_AGENT_ATTRIBUTION and RUNTIME_HOOKS_DISABLE bypass the deny.
dirty="{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"Generated with ${AGENT} Code\"}}"
for var in ALLOW_AGENT_ATTRIBUTION RUNTIME_HOOKS_DISABLE; do
  out=$(printf '%s' "$dirty" | env "$var=1" HOME="$FIXTURE_HOME" GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
  if [ -z "$out" ]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  no-attribution-guard.sh FAIL  %s bypass\n' "$var" >&2
  fi
done

# review job 115: a detector that cannot judge the payload is a failed gate.
# A copy of the guard runs against a broken and then a missing detector, and
# a clean payload must be denied with the failure named, not passed.
BROKEN="$(mktemp -d "${TMPDIR:-/tmp}/no-attribution-guard-broken-XXXXXX")"
mkdir -p "$BROKEN/lib"
cp "$SCRIPT" "$BROKEN/no-attribution-guard.sh"
cp "$(dirname "$SCRIPT")/lib/log.sh" "$(dirname "$SCRIPT")/lib/guard-utils.sh" "$BROKEN/lib/"
printf 'import sys\nsys.stderr.write("boom\\n")\nsys.exit(3)\n' > "$BROKEN/lib/attribution-detect.py"
clean_payload="{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"The hook denies attribution footers.\"}}"
for case in crashing missing; do
  [ "$case" = missing ] && rm -f "$BROKEN/lib/attribution-detect.py"
  out=$(printf '%s' "$clean_payload" | HOME="$FIXTURE_HOME" RUNTIME_HOOKS_DISABLE=0 ALLOW_AGENT_ATTRIBUTION=0 GUARD_LOG_DIR="$GLDIR" bash "$BROKEN/no-attribution-guard.sh" 2>/dev/null || true)
  case "$out" in
    *'"permissionDecision":"deny"'*'could not run'*) PASS=$((PASS+1)) ;;
    *) FAIL=$((FAIL+1)); printf '  no-attribution-guard.sh FAIL  %s detector must deny, got: %s\n' "$case" "$out" >&2 ;;
  esac
done
rm -rf "$BROKEN"

# A temp directory that cannot be written must not make the guard fail open.
dirty_payload="{\"tool_name\":\"Write\",\"tool_input\":{\"content\":\"Generated with ${AGENT} Code\"}}"
out=$(printf '%s' "$dirty_payload" | TMPDIR=/nonexistent-no-attribution-guard HOME="$FIXTURE_HOME" RUNTIME_HOOKS_DISABLE=0 ALLOW_AGENT_ATTRIBUTION=0 GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
case "$out" in
  *'"permissionDecision":"deny"'*) PASS=$((PASS+1)) ;;
  *) FAIL=$((FAIL+1)); printf '  no-attribution-guard.sh FAIL  unwritable TMPDIR must still deny, got: %s\n' "$out" >&2 ;;
esac

# Every deny is one line of valid JSON the runtime can read, even when the
# interpreter that encodes the reason fails: an unreadable deny is no deny.
# The shim fails only the reason encoder, so the detector still runs.
REAL_PYTHON="$(command -v python3)"
SHIM="$(mktemp -d "${TMPDIR:-/tmp}/no-attribution-guard-shim-XXXXXX")"
trap 'rm -rf "$GLDIR" "$FIXTURE_HOME" "$SHIM"' EXIT
printf '#!/bin/sh\nif [ "$1" = "-c" ]; then case "$2" in *json.dumps*) exit 1 ;; esac; fi\nexec "%s" "$@"\n' "$REAL_PYTHON" > "$SHIM/python3"
chmod +x "$SHIM/python3"
for case in normal encoder-fails; do
  path="$PATH"
  [ "$case" = encoder-fails ] && path="$SHIM:$PATH"
  out=$(printf '%s' "$dirty_payload" | PATH="$path" HOME="$FIXTURE_HOME" RUNTIME_HOOKS_DISABLE=0 ALLOW_AGENT_ATTRIBUTION=0 GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
  verdict=$(printf '%s' "$out" | "$REAL_PYTHON" -c '
import json, sys
text = sys.stdin.read()
try:
    decision = json.loads(text)["hookSpecificOutput"]["permissionDecision"]
except Exception:
    decision = "unreadable"
print(decision if "\n" not in text.strip("\n") else "not one line")
')
  if [ "$verdict" = "deny" ]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    printf '  no-attribution-guard.sh FAIL  %s deny must be one JSON line, got %s: %s\n' "$case" "$verdict" "$out" >&2
  fi
done
rm -rf "$SHIM"

# A log write that fails (stderr closed) must not stop the deny from printing.
out=$(printf '%s' "$dirty_payload" | HOME="$FIXTURE_HOME" RUNTIME_HOOKS_DISABLE=0 ALLOW_AGENT_ATTRIBUTION=0 GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>&- || true)
case "$out" in
  *'"permissionDecision":"deny"'*) PASS=$((PASS+1)) ;;
  *) FAIL=$((FAIL+1)); printf '  no-attribution-guard.sh FAIL  closed stderr must still deny, got: %s\n' "$out" >&2 ;;
esac

# The deny records went to GLDIR, and nothing reached the $HOME fallback: the
# fixture HOME holds no file at all after the whole run.
home_files="$(find "$FIXTURE_HOME" -type f | wc -l | tr -d ' ')"
log_records="$(cat "$GLDIR"/* 2>/dev/null | grep -c '' || true)"
if [ "$home_files" = "0" ] && [ "${log_records:-0}" -gt 0 ]; then
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+1))
  printf '  no-attribution-guard.sh FAIL  telemetry isolation (fixture HOME files=%s, GUARD_LOG_DIR records=%s)\n' "$home_files" "${log_records:-0}" >&2
fi

printf 'no-attribution-guard.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
