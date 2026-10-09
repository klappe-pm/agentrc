#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)"
HOOK="${REQUIRE_PR_HOOK:-$DIR/require-pr-on-stop.sh}"
# The scratch path is resolved physically so HOME matches what git rev-parse --show-toplevel
# reports; otherwise /tmp versus /private/tmp would keep the worktree fallback from ever matching.
scratch="$(mktemp -d "${TMPDIR:-/tmp}/require-pr-hook-XXXXXX")"
scratch="$(cd "$scratch" && pwd -P)"
trap 'rm -rf "$scratch"' EXIT
repo="$scratch/repo"
fallback_repo="$scratch/fallback-repo"
remote="$scratch/remote.git"
fallback_remote="$scratch/fallback-remote.git"
bin="$scratch/bin"
test_home="$scratch/home"
worktree_repo="$test_home/projects/_worktrees/example"
worktree_remote="$scratch/worktree-remote.git"
old_date='2001-09-09T01:46:40Z'
mkdir -p "$repo" "$bin" "$test_home/.claude/sessions"
git -C "$repo" init -q -b main
git -C "$repo" config user.name Test
git -C "$repo" config user.email test@example.invalid
printf 'example\n' > "$repo/example.txt"
git -C "$repo" add example.txt
git -C "$repo" commit -q -m initial
git init -q --bare "$remote"
git -C "$repo" remote add origin "$remote"
git -C "$repo" push -q -u origin main
git -C "$repo" remote set-head origin main

# The initial push belongs to an earlier session. Sleeping across the reflog timestamp boundary
# makes the current session start unambiguous without manufacturing reflog records.
sleep 1
session_started_ms="$(python3 -c 'import time; print(time.time_ns() // 1_000_000)')"
printf '{"sessionId":"test-session","startedAt":%s}\n' "$session_started_ms" > "$test_home/.claude/sessions/session.json"
payload="$(printf '{"cwd":%s,"session_id":"test-session"}' "$(printf '%s' "$repo" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))')")"

# The stub answers pull request and commit comment queries with the JSON array of handoff comments
# (body and creation time) the hook asks for. Both default to one clean handoff posted this session
# so each case can select only the condition it needs to exercise. Like the real CLI, `pr view` on a
# branch answers with the latest pull request in any state, while `pr list` answers with open ones
# only. The agent name is built from fragments so this file carries no attribution.
AGENT="Cla""ude"
now_iso='2099-01-01T00:00:00Z'
export GH_DEFAULT_HANDOFFS="[{\"body\":\"## session handoff\\n\\n**Delivered:** x\",\"createdAt\":\"$now_iso\"}]"
dirty_handoff="{\"body\":\"## session handoff\\n\\n**Delivered:** x\\n\\nGenerated with ${AGENT} Code\",\"createdAt\":\"$now_iso\"}"
clean_handoff="{\"body\":\"## session handoff\\n\\n**Delivered:** corrected\",\"createdAt\":\"$now_iso\"}"
stale_handoff="{\"body\":\"## session handoff\\n\\n**Delivered:** earlier session\",\"createdAt\":\"$old_date\"}"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'for arg in "$@"; do' \
  '  case "$arg" in' \
  '    */commits/*/comments)' \
  '      if [ -n "${GH_COMMIT_PAGE_2:-}" ]; then' \
  '        printf "%s\n" "$GH_COMMIT_HANDOFFS"' \
  '        case " $* " in *" --paginate "*) printf "%s\n" "$GH_COMMIT_PAGE_2" ;; esac' \
  '        exit 0' \
  '      fi' \
  '      printf "%s\n" "${GH_COMMIT_HANDOFFS:-$GH_DEFAULT_HANDOFFS}"' \
  '      exit 0' \
  '      ;;' \
  '  esac' \
  'done' \
  'if [ "${1:-}" = "pr" ] && [ "${2:-}" = "view" ] && [ "${4:-}" = "--json" ] && [ "${5:-}" = "mergeCommit" ]; then' \
  '  [ -n "${GH_MERGE_COMMIT:-}" ] || exit 1' \
  '  printf "%s\n" "$GH_MERGE_COMMIT"' \
  '  exit 0' \
  'fi' \
  'if [ "${1:-}" = "pr" ] && [ "${2:-}" = "list" ] && [ "${5:-}" = "--state" ] && [ "${6:-}" = "merged" ]; then' \
  '  [ "${GH_MERGED_LIST:-}" != "unavailable" ] || exit 1' \
  '  if [ "${GH_HAS_PR:-0}" = "1" ] && [ "${GH_PR_STATE:-OPEN}" = "MERGED" ]; then printf "[{\"url\":\"https://example.invalid/pr/1\",\"headRefOid\":\"%s\"}]\n" "${GH_MERGED_HEAD:-}"; else printf "[]\n"; fi' \
  '  exit 0' \
  'fi' \
  '[ "${GH_HAS_PR:-0}" = "1" ] || exit 1' \
  'for arg in "$@"; do' \
  '  if [ "$arg" = "comments" ]; then' \
  '    if [ -n "${GH_HANDOFFS_FILE:-}" ]; then cat "$GH_HANDOFFS_FILE"; exit 0; fi' \
  '    printf "%s\n" "${GH_HANDOFFS:-$GH_DEFAULT_HANDOFFS}"' \
  '    exit 0' \
  '  fi' \
  'done' \
  'if [ "${1:-}" = "pr" ] && [ "${2:-}" = "list" ] && [ "${GH_PR_STATE:-OPEN}" != "OPEN" ]; then exit 0; fi' \
  'printf "%s\n" "https://example.invalid/pr/1"' > "$bin/gh"
chmod +x "$bin/gh"

pass=0
fail=0

check_empty() {
  label="$1"
  output="$2"
  if [ -z "$output" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'require-pr-on-stop.test: %s\n' "$label" >&2
  fi
}

check_contains() {
  label="$1"
  output="$2"
  expected="$3"
  if printf '%s' "$output" | grep -q "$expected"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'require-pr-on-stop.test: %s\n' "$label" >&2
  fi
}

check_absent() {
  label="$1"
  output="$2"
  unexpected="$3"
  if printf '%s' "$output" | grep -qi "$unexpected"; then
    fail=$((fail + 1))
    printf 'require-pr-on-stop.test: %s\n' "$label" >&2
  else
    pass=$((pass + 1))
  fi
}

# Re-entry is decided before repository and scope checks.
output="$(printf '{"cwd":%s,"session_id":"test-session","stop_hook_active":true}' "$(printf '%s' "$repo" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))')" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_empty 're-entry was not ignored' "$output"

# An upstream that was established before this session is not evidence of a push this session.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_empty 'a tracked branch with no push this session was checked' "$output"

# A push is dated by its reflog entry, not by the pushed commit. A commit whose committer date
# predates the session, pushed during the session, is still a push this session and is checked.
sleep 1
printf 'old commit\n' >> "$repo/example.txt"
git -C "$repo" add example.txt
GIT_AUTHOR_DATE="$old_date" GIT_COMMITTER_DATE="$old_date" git -C "$repo" commit -q -m old-dated
git -C "$repo" push -q origin main

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_contains 'a push this session of a commit dated before the session was not checked' "$output" 'No session handoff comment'

# A genuine push after the session start puts the default branch in scope.
printf 'main push\n' >> "$repo/example.txt"
git -C "$repo" add example.txt
git -C "$repo" commit -q -m main-push
git -C "$repo" push -q origin main

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_contains 'default branch push without a commit comment was not blocked' "$output" 'No session handoff comment'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_empty 'default branch push with a commit comment was blocked' "$output"

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS=unavailable bash "$HOOK")"
check_contains 'unreadable commit comments were not blocked' "$output" 'could not be read'

# A handoff an earlier session left on the same commit is history, not this session's handoff.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS="[$stale_handoff]" bash "$HOOK")"
check_contains 'a commit handoff from an earlier session satisfied this session' "$output" 'since this session started'

# WI-55 (A-03): the live handoff is scanned with the attribution detector and a hit blocks the
# stop, naming the label and asking for a corrected handoff as a new comment.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS="[$dirty_handoff]" bash "$HOOK")"
check_contains 'a commit handoff carrying attribution was not blocked' "$output" 'generated-with'
check_contains 'the commit handoff block did not ask for a new comment' "$output" 'corrected handoff as a new comment'

# Fourth Codex review of PR 43: commit comments are paginated, one JSON array per page, and the
# live handoff is the last one across every page. A correction on page two clears page one's hit.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS="[$dirty_handoff]" GH_COMMIT_PAGE_2="[$clean_handoff]" bash "$HOOK")"
check_empty 'a corrected handoff on a later comment page was still blocked' "$output"

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS="[$clean_handoff]" GH_COMMIT_PAGE_2='[]' bash "$HOOK")"
check_empty 'an empty later comment page hid an earlier handoff' "$output"

printf 'dirty\n' >> "$repo/example.txt"
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'dirty checkout was not blocked' "$output" 'not committed'
git -C "$repo" checkout -q -- example.txt

# A genuine feature branch push is in scope and needs an open pull request.
git -C "$repo" switch -q -c feature main
printf 'feature\n' >> "$repo/example.txt"
git -C "$repo" add example.txt
git -C "$repo" commit -q -m feature
git -C "$repo" push -q -u origin feature

# A local commit after this session's push leaves HEAD on a commit no upstream reflog entry holds.
# The session still pushed, so the unpushed commit blocks, and the block does not ask for approval
# the handoff rule already gives.
printf 'unpushed\n' >> "$repo/example.txt"
git -C "$repo" add example.txt
git -C "$repo" commit -q -m unpushed
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 bash "$HOOK")"
check_contains 'a local commit after a push this session was not blocked' "$output" 'not on its upstream'
check_contains 'the unpushed commit block did not name the pre-authorization' "$output" 'pre-authorizes'
check_absent 'the unpushed commit block asked for approval' "$output" 'approval'
git -C "$repo" push -q origin feature

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'a branch pushed this session without a pull request was not blocked' "$output" 'No pull request is open'
check_contains 'the missing pull request block did not name the pre-authorization' "$output" 'pre-authorizes'
check_absent 'the missing pull request block asked for approval' "$output" 'approval'

# A pull request merged at the branch head carries the session's work to its end (issue 250): with
# this session's handoff on it, the session may stop. It is judged like an open one, so a missing,
# earlier-session or attributed handoff still blocks.
feature_head="$(git -C "$repo" rev-parse HEAD)"
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_PR_STATE=MERGED GH_MERGED_HEAD="$feature_head" bash "$HOOK")"
check_empty 'a pull request merged at the branch head with this session handoff was blocked' "$output"

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_PR_STATE=MERGED GH_MERGED_HEAD="$feature_head" GH_HANDOFFS='[]' bash "$HOOK")"
check_contains 'a pull request merged at the branch head without a handoff was not blocked' "$output" 'No session handoff comment'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_PR_STATE=MERGED GH_MERGED_HEAD="$feature_head" GH_HANDOFFS="[$stale_handoff]" bash "$HOOK")"
check_contains 'a merged pull request handoff from an earlier session satisfied this session' "$output" 'since this session started'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_PR_STATE=MERGED GH_MERGED_HEAD="$feature_head" GH_HANDOFFS="[$dirty_handoff]" bash "$HOOK")"
check_contains 'a merged pull request handoff carrying attribution was not blocked' "$output" 'generated-with'

# A merged pull request whose head is not the branch head is history: commits followed it and need
# a pull request of their own. A closed, unmerged one never counts.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_PR_STATE=MERGED GH_MERGED_HEAD=0000000000000000000000000000000000000000 bash "$HOOK")"
check_contains 'a pull request merged before later commits satisfied the open pull request check' "$output" 'No pull request is open'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_PR_STATE=CLOSED GH_MERGED_HEAD="$feature_head" bash "$HOOK")"
check_contains 'a closed pull request satisfied the open pull request check' "$output" 'No pull request is open'

# An unreadable merged pull request answer is not a confirmed absence.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_PR_STATE=MERGED GH_MERGED_HEAD="$feature_head" GH_MERGED_LIST=unavailable bash "$HOOK")"
check_contains 'an unreadable merged pull request answer was not blocked' "$output" 'could not be read'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS='[]' bash "$HOOK")"
check_contains 'a pushed branch without a pull request handoff was not blocked' "$output" 'No session handoff comment'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 bash "$HOOK")"
check_empty 'a pushed branch with a pull request handoff was blocked' "$output"

# A handoff an earlier session posted on the pull request does not stand in for this session's.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS="[$stale_handoff]" bash "$HOOK")"
check_contains 'a pull request handoff from an earlier session satisfied this session' "$output" 'since this session started'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS="[$stale_handoff, $clean_handoff]" bash "$HOOK")"
check_empty 'a handoff posted this session after an earlier one was blocked' "$output"

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS=unavailable bash "$HOOK")"
check_contains 'unreadable pull request comments were not blocked' "$output" 'could not be read'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS='{"not":"a list"}' bash "$HOOK")"
check_contains 'a malformed comment answer was not blocked as unreadable' "$output" 'could not be read'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS='["## session handoff"]' bash "$HOOK")"
check_contains 'a handoff without a creation time was not blocked as unreadable' "$output" 'could not be read'

# WI-55 (A-03): only the live (last) handoff decides. A handoff carrying attribution blocks with
# the detector's label; a corrected handoff posted after it as a new comment clears the block.
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS="[$dirty_handoff]" bash "$HOOK")"
check_contains 'a pull request handoff carrying attribution was not blocked' "$output" 'generated-with'
check_contains 'the pull request handoff block did not ask for a new comment' "$output" 'corrected handoff as a new comment'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS="[$clean_handoff, $dirty_handoff]" bash "$HOOK")"
check_contains 'a later handoff carrying attribution was not blocked' "$output" 'generated-with'

output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS="[$dirty_handoff, $clean_handoff]" bash "$HOOK")"
check_empty 'a corrected handoff posted as a new comment was still blocked' "$output"

# Third Codex review of PR 43: a long handoff history (twenty 60 KB handoffs, past the per-process
# argument and environment limit) is still read, not reported as unreadable.
python3 -c '
import json, sys
body = "## session handoff\n\n**Delivered:** " + "x" * 60000
json.dump([{"body": body, "createdAt": sys.argv[2]}] * 20, open(sys.argv[1], "w"))
' "$scratch/long-handoffs.json" "$now_iso"
output="$(printf '%s' "$payload" | HOME="$test_home" PATH="$bin:$PATH" GH_HAS_PR=1 GH_HANDOFFS_FILE="$scratch/long-handoffs.json" bash "$HOOK")"
check_empty 'a long clean handoff history was blocked' "$output"

# With no session file and no recent HEAD reflog entry, scope falls back to worktrees only. The
# old committer date also dates the HEAD reflog entry, so no session start is found. This ordinary
# checkout remains out of scope even though it has an upstream.
mkdir -p "$fallback_repo"
git -C "$fallback_repo" init -q -b main
git -C "$fallback_repo" config user.name Test
git -C "$fallback_repo" config user.email test@example.invalid
printf 'fallback\n' > "$fallback_repo/example.txt"
git -C "$fallback_repo" add example.txt
GIT_AUTHOR_DATE="$old_date" GIT_COMMITTER_DATE="$old_date" git -C "$fallback_repo" commit -q -m initial
git init -q --bare "$fallback_remote"
git -C "$fallback_repo" remote add origin "$fallback_remote"
git -C "$fallback_repo" push -q -u origin main
fallback_payload="$(printf '{"cwd":%s}' "$(printf '%s' "$fallback_repo" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))')")"
output="$(printf '%s' "$fallback_payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_empty 'an ordinary checkout without a session start signal did not use worktree-only scope' "$output"

# The HEAD reflog fallback is dated by the reflog too: switching to a new branch now is a recent
# entry even though the commit it lands on is old, so the push that follows is this session's.
sleep 1
git -C "$fallback_repo" switch -q -c topic
git -C "$fallback_repo" push -q -u origin topic
output="$(printf '%s' "$fallback_payload" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'a recent HEAD reflog entry on an old commit did not start the session' "$output" 'No pull request is open'

# The same missing signal inside a worktree is in scope: the fallback checks it rather than exiting.
mkdir -p "$worktree_repo"
git -C "$worktree_repo" init -q -b main
git -C "$worktree_repo" config user.name Test
git -C "$worktree_repo" config user.email test@example.invalid
printf 'worktree\n' > "$worktree_repo/example.txt"
git -C "$worktree_repo" add example.txt
GIT_AUTHOR_DATE="$old_date" GIT_COMMITTER_DATE="$old_date" git -C "$worktree_repo" commit -q -m initial
git init -q --bare "$worktree_remote"
git -C "$worktree_repo" remote add origin "$worktree_remote"
GIT_COMMITTER_DATE="$old_date" git -C "$worktree_repo" push -q -u origin main
worktree_payload="$(printf '{"cwd":%s}' "$(printf '%s' "$worktree_repo" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))')")"
output="$(printf '%s' "$worktree_payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_contains 'a worktree without a session start signal was not checked' "$output" 'No session handoff comment'

output="$(printf '%s' "$worktree_payload" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS="[$stale_handoff]" bash "$HOOK")"
check_empty 'a worktree with no session start rejected an existing handoff' "$output"

# Shared checkout. Several sessions run in one canonical checkout on the default branch, and any of
# them fetches and fast-forwards it after merging a pull request. Neither that fetch nor the files
# other sessions or the user leave uncommitted there are this session's work. The setup push belongs
# to an earlier session; the shared session starts after it.
shared_remote="$scratch/shared-remote.git"
shared_repo="$scratch/shared-repo"
other_repo="$scratch/other-repo"
linked_tree="$scratch/shared-linked"
# The remote and the clone name main explicitly: a git whose default branch is master would leave
# the clone on an unborn master branch.
git init -q --bare -b main "$shared_remote"
mkdir -p "$shared_repo"
git -C "$shared_repo" init -q -b main
git -C "$shared_repo" config user.name Test
git -C "$shared_repo" config user.email test@example.invalid
printf 'shared\n' > "$shared_repo/example.txt"
git -C "$shared_repo" add example.txt
git -C "$shared_repo" commit -q -m initial
git -C "$shared_repo" remote add origin "$shared_remote"
git -C "$shared_repo" push -q -u origin main
git -C "$shared_repo" remote set-head origin main
git clone -q -b main "$shared_remote" "$other_repo"
git -C "$other_repo" config user.name Other
git -C "$other_repo" config user.email other@example.invalid
sleep 1
shared_started_ms="$(python3 -c 'import time; print(time.time_ns() // 1_000_000)')"
printf '{"sessionId":"shared-session","startedAt":%s}\n' "$shared_started_ms" > "$test_home/.claude/sessions/shared.json"

# transcript_with <file> <tool_use JSON>...: writes a session transcript holding one assistant
# message per tool_use, in the shape Claude Code records them.
transcript_with() {
  target="$1"
  shift
  python3 -c '
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    for raw in sys.argv[2:]:
        handle.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [dict(json.loads(raw), type="tool_use")]}}) + "\n")
' "$target" "$@"
}
json_string() {
  printf '%s' "$1" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))'
}
shared_payload() {
  if [ -n "${1:-}" ]; then
    printf '{"cwd":%s,"session_id":"shared-session","transcript_path":%s}' "$(json_string "$shared_repo")" "$(json_string "$1")"
  else
    printf '{"cwd":%s,"session_id":"shared-session"}' "$(json_string "$shared_repo")"
  fi
}
empty_transcript="$scratch/empty.jsonl"
: > "$empty_transcript"

# Another session merges and pushes main, then fast-forwards the shared checkout. The fetch lands
# origin/main on HEAD, which used to read as this session's push and demand a handoff on a merge
# commit this session never produced.
printf 'other merge\n' >> "$other_repo/example.txt"
git -C "$other_repo" commit -q -am other-merge
git -C "$other_repo" push -q origin main
git -C "$shared_repo" fetch -q origin
git -C "$shared_repo" merge -q --ff-only origin/main
output="$(shared_payload | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_empty 'a fetch of another session merge put the shared checkout in scope' "$output"
output="$(shared_payload "$empty_transcript" | HOME="$test_home" PATH="$bin:$PATH" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_empty 'a fetch of another session merge was checked for a session with a transcript' "$output"

# A merge this session ran is its own work: the handoff belongs on that pull request's merge commit,
# resolved from the pull request rather than read from whatever HEAD the checkout now holds.
merge_sha="$(git -C "$shared_repo" rev-parse HEAD)"
merge_transcript="$scratch/merge.jsonl"
transcript_with "$merge_transcript" '{"name":"Bash","input":{"command":"gh pr merge 7 --merge --delete-branch"}}'
output="$(shared_payload "$merge_transcript" | HOME="$test_home" PATH="$bin:$PATH" GH_MERGE_COMMIT="$merge_sha" GH_COMMIT_HANDOFFS='[]' bash "$HOOK")"
check_contains 'a merge this session ran without a merge commit handoff was not blocked' "$output" "No session handoff comment on commit $merge_sha"
output="$(shared_payload "$merge_transcript" | HOME="$test_home" PATH="$bin:$PATH" GH_MERGE_COMMIT="$merge_sha" bash "$HOOK")"
check_empty 'a merge this session ran with a merge commit handoff was blocked' "$output"
output="$(shared_payload "$merge_transcript" | HOME="$test_home" PATH="$bin:$PATH" GH_MERGE_COMMIT="$merge_sha" GH_COMMIT_HANDOFFS="[$stale_handoff]" bash "$HOOK")"
check_contains 'an earlier session handoff on the merge commit satisfied this session' "$output" 'since this session started'
output="$(shared_payload "$merge_transcript" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'an unresolvable merge this session ran was not blocked' "$output" 'could not be read'

# A session started in the shared checkout that pushes a branch from a linked worktree is judged on
# that worktree, which its transcript names, even though its cwd never moved.
git -C "$shared_repo" worktree add -q -b topic "$linked_tree"
printf 'topic\n' >> "$linked_tree/example.txt"
git -C "$linked_tree" commit -q -am topic
git -C "$linked_tree" push -q -u origin topic
output="$(shared_payload "$empty_transcript" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_empty 'a worktree the transcript never names was checked' "$output"
linked_transcript="$scratch/linked.jsonl"
transcript_with "$linked_transcript" "{\"name\":\"Bash\",\"input\":{\"command\":$(json_string "git -C $linked_tree push -u origin topic")}}"
output="$(shared_payload "$linked_transcript" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'a worktree the session pushed from was not checked' "$output" 'No pull request is open'
edited_transcript="$scratch/edited.jsonl"
transcript_with "$edited_transcript" "{\"name\":\"Edit\",\"input\":{\"file_path\":$(json_string "$linked_tree/example.txt")}}"
output="$(shared_payload "$edited_transcript" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'a worktree the session edited in was not checked' "$output" 'No pull request is open'
git -C "$shared_repo" worktree remove --force "$linked_tree"

# This session pushes main from the shared checkout, so the checkout is in scope. Uncommitted files
# there that other sessions or the user left are not this session's, and do not block it.
printf 'own push\n' >> "$shared_repo/example.txt"
git -C "$shared_repo" commit -q -am own-push
git -C "$shared_repo" push -q origin main
printf 'hand edit\n' >> "$shared_repo/example.txt"
printf 'other session\n' > "$shared_repo/other-session.txt"
output="$(shared_payload "$empty_transcript" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_empty 'uncommitted files another session or the user left blocked this session' "$output"
mine_transcript="$scratch/mine.jsonl"
printf 'mine\n' > "$shared_repo/mine.txt"
transcript_with "$mine_transcript" "{\"name\":\"Write\",\"input\":{\"file_path\":$(json_string "$shared_repo/mine.txt"),\"content\":\"mine\"}}"
output="$(shared_payload "$mine_transcript" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'a file this session wrote and left uncommitted was not blocked' "$output" 'not committed'
check_contains 'the uncommitted block did not name the file' "$output" 'mine.txt'
check_absent 'the uncommitted block named a file that is not this session' "$output" 'other-session.txt'
shell_transcript="$scratch/shell.jsonl"
transcript_with "$shell_transcript" "{\"name\":\"Bash\",\"input\":{\"command\":$(json_string "printf mine > $shared_repo/mine.txt")}}"
output="$(shared_payload "$shell_transcript" | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'a file this session wrote through the shell and left uncommitted was not blocked' "$output" 'not committed'
# Without a transcript nothing tells this session's files apart, so every uncommitted file counts.
output="$(shared_payload | HOME="$test_home" PATH="$bin:$PATH" bash "$HOOK")"
check_contains 'uncommitted files without a transcript were not blocked' "$output" 'not committed'

printf 'require-pr-on-stop.test: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
