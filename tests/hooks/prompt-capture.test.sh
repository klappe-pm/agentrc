#!/usr/bin/env bash
# Behavioral tests for prompt-capture.sh
#
# UserPromptSubmit hook: stores each prompt as a Markdown note plus derived
# SQLite and JSON indexes under <project>/.docs/prompts/<category>/. The hook
# must stay silent on stdout, exit 0 on every input, dedupe repeats, redact
# secret-shaped values, honor both disable switches, and capture nothing from
# a public target.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../stratarc/data/hooks" && pwd)"
SCRIPT="$HOOK_DIR/prompt-capture.sh"
# shellcheck source=lib/hook-test.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hook-test.sh"

PASS=0
FAIL=0

TMP="$(mktemp -d "${TMPDIR:-/tmp}/prompt-capture-test-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
PROJECT="$TMP/proj"
mkdir -p "$PROJECT"
git -C "$PROJECT" init -q
export PROMPT_CAPTURE_TELEMETRY_DIR="$TMP/telemetry"
export RUNTIME_HOOKS_DISABLE=0
export PROMPT_CAPTURE_DISABLE=0
# Scrub root overrides from the ambient environment so this suite's no-git and
# vault cases are not silently rerouted by whatever the operator's real
# session happens to have set (F-31/WI-21: PROMPT_CAPTURE_FALLBACK_ROOT is
# now deployed into ~/.claude/settings.json's env block).
unset PROMPT_CAPTURE_ROOT PROMPT_CAPTURE_FALLBACK_ROOT
# The public-target list is read from the source root; the scenario that needs
# one points LLM_ROOT at a fixture, the rest must not inherit a session's.
unset LLM_ROOT

ok() { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf '  prompt-capture.sh FAIL  %s\n' "$1" >&2; }

payload() {
  python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1], "cwd": sys.argv[2], "session_id": "sess-test"}))' "$1" "$2"
}

# The hook may exit before reading stdin (disable switches), which would give
# the writer a SIGPIPE; build the payload first and tolerate the pipe status.
run_hook() {
  local body
  body="$(payload "$1" "$PROJECT")"
  printf '%s' "$body" | bash "$SCRIPT" || true
}

count_notes() { find "$PROJECT/.docs/prompts" -mindepth 2 -name '*.md' 2>/dev/null | wc -l | tr -d ' '; }

# 1. disabled guard is silent and exits 0
hook_test_disable_guard "$SCRIPT" && ok || bad "disable guard"

# 2. PROMPT_CAPTURE_DISABLE=1 is also silent and writes nothing
body="$(payload "capture nothing at all please, this is disabled" "$PROJECT")"
out="$(printf '%s' "$body" | PROMPT_CAPTURE_DISABLE=1 bash "$SCRIPT" || true)"
if [ -z "$out" ] && [ "$(count_notes)" = "0" ]; then ok; else bad "PROMPT_CAPTURE_DISABLE=1 should be a no-op"; fi

# 3. a real prompt produces one note, one sqlite row, one json entry, and no stdout
out="$(run_hook "implement a hook that stores every prompt as markdown under docs/prompts and links related prompts")"
[ -z "$out" ] && ok || bad "hook must be silent on stdout (got: $out)"
[ "$(count_notes)" = "1" ] && ok || bad "expected exactly one note, got $(count_notes)"
# The default store is the project's .docs/prompts, never docs/prompts.
[ -d "$PROJECT/.docs/prompts" ] && ok || bad "default store should be <toplevel>/.docs/prompts"
[ ! -e "$PROJECT/docs" ] && ok || bad "nothing may be written under <toplevel>/docs"
rows="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from prompts").fetchone()[0])' "$PROJECT/.docs/prompts/prompts.sqlite")"
[ "$rows" = "1" ] && ok || bad "expected one sqlite row, got $rows"
jcount="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["count"])' "$PROJECT/.docs/prompts/prompts.json")"
[ "$jcount" = "1" ] && ok || bad "expected json count 1, got $jcount"
note="$(find "$PROJECT/.docs/prompts" -mindepth 2 -name '*.md')"
grep -q '^  - prompt$' "$note" && grep -q '^topics: \[\]$' "$note" && ok || bad "note frontmatter lacks types: prompt and topics: []"
grep -Eq '^`{4,}markdown$' "$note" && ok || bad "note lacks a markdown-labeled prompt fence"
fences="$(grep -Ec '^`{4,}markdown$' "$note")"
[ "$fences" = "2" ] && ok || bad "expected two markdown fences (refactored and original), got $fences"
grep -q '^## refactored-prompt$' "$note" && grep -q '^## original-prompt$' "$note" && ok || bad "note lacks both prompt sections"
# The body carries the two prompts and nothing else.
if grep -Eq '^## (summary|related)$' "$note"; then bad "note body must carry no section but the two prompts"; else ok; fi
[ "$(grep -c '^## ' "$note")" = "2" ] && ok || bad "note body should have exactly two sections"

# 4. the same prompt again is deduped
run_hook "implement a hook that stores every prompt as markdown under docs/prompts and links related prompts" >/dev/null
[ "$(count_notes)" = "1" ] && ok || bad "duplicate prompt should not add a note"

# 4b. a harness envelope is not a prompt the user typed
run_hook "$(printf '%s\n' '<task-notification>' '<summary>1 unread notification about a subscribed pull request</summary>' '</task-notification>')" >/dev/null
[ "$(count_notes)" = "1" ] && ok || bad "harness envelope should not become a note"
if grep -rq 'unread notification' "$PROJECT/.docs/prompts"; then bad "envelope text leaked into the store"; else ok; fi

# 5. a bare slash command and a too-short prompt are skipped
run_hook "/pass" >/dev/null
run_hook "hi there" >/dev/null
[ "$(count_notes)" = "1" ] && ok || bad "slash command or short prompt should be skipped"

# 6. secret-shaped values are redacted before the note is written
# The fixture is assembled at runtime so no token-shaped literal sits in this
# file, which the staged-secrets commit gate would reject.
secret_value="$(printf 'abcdefghijklmnopqrstuvwxyz%s' 123456)"
run_hook "store this prompt but never persist my token=${secret_value} anywhere" >/dev/null
if grep -rq "$secret_value" "$PROJECT/.docs/prompts"; then
  bad "token-shaped value leaked into the store"
else
  ok
fi
grep -rq 'REDACTED:generic_assignment' "$PROJECT/.docs/prompts" --include='*.md' && ok || bad "redaction marker with kind missing"

# 7. a similar prompt links to the earlier one
run_hook "add related prompt links to every stored markdown prompt under docs/prompts so the graph connects them" >/dev/null
links="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from prompt_links").fetchone()[0])' "$PROJECT/.docs/prompts/prompts.sqlite")"
[ "$links" -ge 1 ] && ok || bad "expected at least one related link, got $links"

# 8. telemetry carries no prompt text
if grep -rq 'links related prompts' "$PROMPT_CAPTURE_TELEMETRY_DIR"; then
  bad "telemetry must not contain prompt text"
else
  ok
fi

# 9. malformed stdin exits 0 silently and is never captured as a prompt
status=0
out="$(printf 'not json but long enough to clear the minimum prompt length easily' | bash "$SCRIPT")" || status=$?
[ "$status" = "0" ] && [ -z "$out" ] && ok || bad "malformed input should be a silent no-op"
if grep -rq 'not json but long enough' "$PROJECT/.docs/prompts"; then bad "malformed payload must not become a note"; else ok; fi

# 10. a prompt from a directory with no git repository is skipped, not routed elsewhere
NOGIT="$TMP/nogit"; mkdir -p "$NOGIT"
body="$(payload "implement something from a directory that is not a git repository at all" "$NOGIT")"
printf '%s' "$body" | HOME="$TMP/fakehome" bash "$SCRIPT" || true
if [ -e "$NOGIT/.docs" ] || [ -e "$TMP/fakehome" ]; then bad "no-git prompt must not be written anywhere"; else ok; fi
grep -q '"no-git-repo"' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "no-git skip should be recorded in telemetry"

# 10b. every telemetry record carries the session id at the top level.
# regression: without it a captured prompt cannot be joined to the transcript
# that recorded what happened to it, so the prompt store sits beside the
# transcripts with no way to line the two up. A skip record needs it as much as
# a capture does, which is why it is resolved before the first early return.
missing="$(python3 - "$PROMPT_CAPTURE_TELEMETRY_DIR" <<'PY'
import json, pathlib, sys
bad = 0
for path in pathlib.Path(sys.argv[1]).glob("prompt-capture-*.jsonl"):
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if "session_id" not in record or not record["session_id"]:
            bad += 1
print(bad)
PY
)"
[ "$missing" = "0" ] && ok || bad "$missing telemetry records carry no session_id"
grep -q '"session_id": "sess-test"' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "telemetry should carry the real session id"

# 11. a URL with embedded credentials is redacted
run_hook "connect with postgres://admin:hunter2secret@db.internal:5432/app and never print it" >/dev/null
if grep -rq 'hunter2secret' "$PROJECT/.docs/prompts"; then bad "url credentials leaked into the store"; else ok; fi

# 12. a prompt that opens with a code fence cannot truncate the note
fenced_prompt="$(printf '%s\n' '```python' 'print("hello from a fenced prompt that must survive storage intact")' '```' 'please review this snippet for correctness and style')"
run_hook "$fenced_prompt" >/dev/null
fenced="$(grep -l 'fenced prompt that must survive' "$PROJECT"/.docs/prompts/*/*.md | head -1)"
if python3 -c '
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^## original-prompt\n\n(`{3,})markdown\n(.*?)\n\1$", text, re.S | re.M)
sys.exit(0 if m and m.group(2).startswith("```python") and "please review" in m.group(2) else 1)
' "$fenced"; then ok; else bad "fenced prompt did not round-trip intact"; fi

# 12b. telemetry's capability field reads the prompt's own leading slash
# command, when it has one, and stays empty otherwise (WI-16/F-21).
run_hook "/commit-and-pr push the branch and open the pull request for review please" >/dev/null
grep -q '"capability": "commit-and-pr"' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "capability should read the leading slash command"
grep -q '"capability": ""' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "a plain prompt's telemetry should carry an empty capability, not a missing one"

# 12c. telemetry carries the payload's message_id and prompt_id at the top
# level, so a captured prompt can be joined to its own transcript record rather
# than only to its session (WI-34, ledger decision 5). A payload without them
# records empty strings, never a missing key.
body="$(python3 -c 'import json,sys; print(json.dumps({"prompt": "record the message id of this prompt in the telemetry for the ledger join", "cwd": sys.argv[1], "session_id": "sess-test", "message_id": "msg-uuid-test", "prompt_id": "prompt-id-test"}))' "$PROJECT")"
printf '%s' "$body" | bash "$SCRIPT" || true
grep -q '"message_id": "msg-uuid-test"' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "telemetry should carry the payload message_id"
grep -q '"prompt_id": "prompt-id-test"' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "telemetry should carry the payload prompt_id"
grep -q '"message_id": ""' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "a payload without message_id should record an empty one, not a missing key"

# 13. the runtime label is inferred from the deploying adapter's directory
CODEX_HOME="$TMP/codexhome"; mkdir -p "$CODEX_HOME/.codex/hooks/lib"
cp "$SCRIPT" "$CODEX_HOME/.codex/hooks/prompt-capture.sh"
cp "$HOOK_DIR/lib/prompt-capture.py" "$HOOK_DIR/lib/prompt-capture.json" "$HOOK_DIR/lib/public-targets.py" "$CODEX_HOME/.codex/hooks/lib/"
body="$(payload "implement the same capture from a codex session so the runtime label reads codex" "$PROJECT")"
printf '%s' "$body" | bash "$CODEX_HOME/.codex/hooks/prompt-capture.sh" || true
grep -rq '^sub-category: codex$' "$PROJECT/.docs/prompts" --include='*.md' && ok || bad "runtime label should be inferred as codex"

# 14. the OpenCode adapter's deploy path (~/.config/opencode/hooks) infers the
# opencode runtime label rather than silently falling back to claude (F-31/WI-21)
OPENCODE_HOME="$TMP/opencodehome"; mkdir -p "$OPENCODE_HOME/.config/opencode/hooks/lib"
cp "$SCRIPT" "$OPENCODE_HOME/.config/opencode/hooks/prompt-capture.sh"
cp "$HOOK_DIR/lib/prompt-capture.py" "$HOOK_DIR/lib/prompt-capture.json" "$HOOK_DIR/lib/public-targets.py" "$OPENCODE_HOME/.config/opencode/hooks/lib/"
body="$(payload "implement the same capture from an opencode session so the runtime label reads opencode" "$PROJECT")"
printf '%s' "$body" | bash "$OPENCODE_HOME/.config/opencode/hooks/prompt-capture.sh" || true
grep -rq '^sub-category: opencode$' "$PROJECT/.docs/prompts" --include='*.md' && ok || bad "runtime label should be inferred as opencode"

# 15. a project named in the source root's projects-root/public-targets.json is
# a public repository and receives no capture at all: no store, no fallback,
# no override, only a telemetry skip that carries no prompt text.
SOURCE_ROOT="$TMP/source-root"; mkdir -p "$SOURCE_ROOT/projects-root"
printf '%s\n' '{"$comment": "fixture", "targets": ["pubproj"]}' >"$SOURCE_ROOT/projects-root/public-targets.json"
PUBLIC="$TMP/pubproj"; mkdir -p "$PUBLIC"
git -C "$PUBLIC" init -q
body="$(payload "implement a feature in the public repository that must never keep this prompt" "$PUBLIC")"
printf '%s' "$body" | LLM_ROOT="$SOURCE_ROOT" PROMPT_CAPTURE_ROOT="$TMP/public-override" PROMPT_CAPTURE_FALLBACK_ROOT="$TMP/public-fallback" bash "$SCRIPT" || true
if [ -e "$PUBLIC/.docs" ] || [ -e "$TMP/public-override" ] || [ -e "$TMP/public-fallback" ]; then bad "public target prompt must not be written anywhere"; else ok; fi
grep -q '"public-target"' "$PROMPT_CAPTURE_TELEMETRY_DIR"/prompt-capture-*.jsonl && ok || bad "public-target skip should be recorded in telemetry"
if grep -rq 'must never keep this prompt' "$PROMPT_CAPTURE_TELEMETRY_DIR"; then bad "telemetry must not contain the public prompt text"; else ok; fi
# A project the list does not name still captures under the same source root.
body="$(payload "implement a feature in the private repository beside the public one" "$PROJECT")"
printf '%s' "$body" | LLM_ROOT="$SOURCE_ROOT" bash "$SCRIPT" || true
grep -rq 'private repository beside the public one' "$PROJECT/.docs/prompts" --include='*.md' && ok || bad "an unlisted project should still capture"

if [ "$FAIL" -gt 0 ]; then
  printf 'prompt-capture.test: FAIL (%d passed, %d failed)\n' "$PASS" "$FAIL" >&2
  exit 1
fi
printf 'prompt-capture.test: passed (%d cases)\n' "$PASS"
