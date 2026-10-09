#!/usr/bin/env bash
# Stop hook for preserving completed session changes in a reviewed branch and requiring the
# session handoff comment that the session-handoff-on-pr rule tells the next session to read.
#
# It judges only this session's own work. Several sessions share one canonical checkout, and git
# cannot tell their work apart there: remote tracking reflogs live in the common git directory of
# every worktree, any session's fetch moves them, and `git status` lists every session's files and
# the user's hand edits alike. So the session transcript (transcript_path in the Stop payload, plus
# its subagents' transcripts) is read with hooks/lib/session-footprint.py to name the worktrees the
# session wrote in or ran git against, the pull requests it merged, and the uncommitted files it
# wrote. Without a readable transcript only the cwd's worktree is judged, and every uncommitted
# file there counts.
#
# A worktree is in scope when its current branch's remote tracking ref records a push after the
# session start, dated by the reflog entry rather than the commit. A fetch never counts, since any
# session fetches. The start comes from ~/.claude/sessions metadata or the oldest recent HEAD reflog
# entry of the cwd; if neither exists, only worktrees under ~/projects/_worktrees are in scope.
# Within scope the session's own files must be committed and pushed, and the pull request must be
# open, or merged at exactly the branch head, and carry a handoff posted since the session start;
# a push to the default branch carries it on the pushed head commit instead. A pull request the
# session merged with `gh pr merge` carries a handoff on its merge commit, whatever HEAD any
# checkout now holds. The live handoff is also scanned with hooks/lib/attribution-detect.py, and a
# hit blocks the stop with the detector's label and a request for a corrected handoff posted as a
# new comment.

set -euo pipefail
[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

for required in git python3; do
  if ! command -v "$required" >/dev/null 2>&1; then
    printf 'require-pr-on-stop: required tool unavailable: %s\n' "$required" >&2
    exit 1
  fi
done

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECTOR="$HOOK_DIR/lib/attribution-detect.py"
FOOTPRINT="$HOOK_DIR/lib/session-footprint.py"

emit_block() {
  reason="$1"
  encoded="$(REQUIRE_PR_REASON="$reason" python3 -c 'import json, os; print(json.dumps(os.environ["REQUIRE_PR_REASON"]))')"
  printf '{"decision":"block","reason":%s}\n' "$encoded"
}

# judge_handoffs <json array of handoff comments, oldest first, each {"body", "createdAt"}>
# Prints one word: "unreadable" when the answer is not a list of such comments, "missing" when it
# is empty, "stale" when every handoff predates the session start in $session_start (an earlier
# session's handoff is history, not this session's), "clean" when the live (last) handoff of this
# session carries no attribution, or the detector's label when it does (A-03 of
# the design record). With no known
# session start every handoff counts. A missing detector is reported on stderr and judged clean,
# the way the attribution guard itself fails open. The array travels on stdin, never in the
# environment or argv, because an append-only handoff history grows past the per-process limit.
judge_handoffs() {
  printf '%s' "$1" | REQUIRE_PR_DETECTOR="$DETECTOR" REQUIRE_PR_SESSION_START="$session_start" python3 -c '
import datetime
import importlib.util
import json
import os
import sys

# A paginated answer is one JSON array per page, back to back; the pages are joined in order.
text = sys.stdin.read()
decoder = json.JSONDecoder()
handoffs, position = [], 0
try:
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        page, position = decoder.raw_decode(text, position)
        if not isinstance(page, list):
            raise ValueError("page is not a list")
        handoffs.extend(page)
    if not text.strip():
        raise ValueError("empty answer")
except ValueError:
    handoffs = None


def posted_at(comment):
    """The comment creation time as epoch seconds, or None when it is not a readable comment."""
    if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
        return None
    created = comment.get("createdAt")
    if not isinstance(created, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


times = [posted_at(comment) for comment in handoffs] if isinstance(handoffs, list) else [None]
if any(moment is None for moment in times):
    print("unreadable")
    sys.exit(0)
if not handoffs:
    print("missing")
    sys.exit(0)
start = os.environ.get("REQUIRE_PR_SESSION_START", "")
current = [comment["body"] for comment, moment in zip(handoffs, times) if not start or moment >= int(start)]
if not current:
    print("stale")
    sys.exit(0)
path = os.environ["REQUIRE_PR_DETECTOR"]
if not os.path.isfile(path):
    sys.stderr.write("require-pr-on-stop: attribution detector unavailable: %s\n" % path)
    print("clean")
    sys.exit(0)
spec = importlib.util.spec_from_file_location("attribution_detect", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.detect(current[-1]) or "clean")
' || printf 'unreadable\n'
}

attribution_block() {
  where="$1"
  label="$2"
  emit_block "The live session handoff on $where carries agent attribution ($label), which the no-agent-attribution rule forbids in pull request and commit comments. A handoff is never edited (see the session-handoff-on-pr rule), so post a corrected handoff as a new comment opening with '## session handoff', without the attribution, before ending the task."
}

# check_commit_handoff <sha> <what the commit is> [owner/repo]
# Blocks (returns 1) unless the commit carries a clean handoff posted this session. Without a
# repository argument gh resolves {owner}/{repo} from the current directory.
check_commit_handoff() {
  sha="$1"
  what="$2"
  slug="${3:-}"
  [ -n "$slug" ] || slug='{owner}/{repo}'
  handoffs="$(gh api --paginate "repos/$slug/commits/$sha/comments" --jq '[.[] | select(.body | test("## session handoff")) | {body, createdAt: .created_at}]' 2>/dev/null || true)"
  verdict="$(judge_handoffs "$handoffs")"
  case "$verdict" in
    unreadable)
      emit_block "The comments on $what could not be read, so the session handoff cannot be verified. Confirm the handoff is posted on commit $sha before ending the task."
      return 1
      ;;
    missing)
      emit_block "No session handoff comment on commit $sha ($what). Post one opening with the heading '## session handoff' and carrying Delivered, Gates, Next, Blocked, Do not and Open decisions, per the session-handoff-on-pr rule, before ending the task."
      return 1
      ;;
    stale)
      emit_block "No session handoff comment on commit $sha ($what) since this session started; the ones there belong to earlier sessions. Post a new one opening with the heading '## session handoff' and carrying Delivered, Gates, Next, Blocked, Do not and Open decisions, per the session-handoff-on-pr rule, before ending the task."
      return 1
      ;;
    clean) ;;
    *)
      attribution_block "commit $sha" "$verdict"
      return 1
      ;;
  esac
  return 0
}

# check_repo <worktree top level>
# Runs inside that worktree so gh resolves its repository. Returns 1 after emitting a block, 0 when
# the worktree holds nothing of this session's that is unfinished.
check_repo() {
  repo="$1"
  cd "$repo" || return 0

  branch="$(git -C "$repo" branch --show-current 2>/dev/null || true)"
  [ -n "$branch" ] || return 0

  upstream="$(git -C "$repo" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)"
  [ -n "$upstream" ] || return 0

  if [ -n "$session_start" ]; then
    # Any push of this upstream after the session start puts the worktree in scope, whatever HEAD
    # is now, so a commit made after that push still reaches the unpushed commit check below. A
    # fetch never counts: in a shared checkout it is whichever session last synchronized.
    pushed_this_session="$(git -C "$repo" log -g --date=unix --format='%gd%x09%gs' "$upstream" 2>/dev/null | awk -F '\t' -v start="$session_start" '{ moved = $1; sub(/^.*@\{/, "", moved); sub(/\}$/, "", moved) } moved + 0 >= start + 0 && $2 ~ /update by push/ { found = 1 } END { if (found) print "1" }')"
    [ "$pushed_this_session" = "1" ] || return 0
  else
    case "$repo/" in
      "$HOME/projects/_worktrees/"*) ;;
      *) return 0 ;;
    esac
  fi

  # Only this session's uncommitted files block it. Another session's files, or the user's hand
  # edits, in a shared checkout are not its work. Without a transcript nothing tells them apart.
  # The NUL separated list is piped, never captured: bash drops NUL bytes from a substitution.
  if [ -n "$(git -C "$repo" status --porcelain 2>/dev/null || true)" ]; then
    if [ "$have_transcript" = "1" ]; then
      own="$({ git -C "$repo" status --porcelain=v1 -z --untracked-files=all 2>/dev/null || true; } | python3 "$FOOTPRINT" owned "$transcript" "$repo" "$session_start" 2>/dev/null || printf 'unavailable')"
    else
      own="unavailable"
    fi
    if [ "$own" = "unavailable" ]; then
      emit_block "Session changes are not committed. Preserve them in a local commit before ending the task."
      return 1
    fi
    if [ -n "$own" ]; then
      listed="$(printf '%s\n' "$own" | head -n 5 | paste -sd ',' - | sed 's/,/, /g')"
      emit_block "Session changes are not committed in $repo ($listed). Preserve them in a local commit before ending the task."
      return 1
    fi
  fi

  ahead="$(git -C "$repo" rev-list --count "$upstream..HEAD" 2>/dev/null || true)"
  if [ -z "$ahead" ] || [ "$ahead" -ne 0 ]; then
    emit_block "This branch has local commits that are not on its upstream. Push them before ending the task; on a repository the user owns, the session-handoff-on-pr rule pre-authorizes pushing the session's own work."
    return 1
  fi

  if ! command -v gh >/dev/null 2>&1; then
    emit_block "The GitHub CLI is unavailable, so a session handoff cannot be verified."
    return 1
  fi

  # the session-handoff-on-pr rule: a non-default branch carries its handoff on a pull request; the
  # default branch has no pull request of its own, so its handoff lives as a comment on the pushed
  # head commit instead.
  default_branch="$(git -C "$repo" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true)"
  default_branch="${default_branch#origin/}"
  if [ -z "$default_branch" ]; then
    if git -C "$repo" show-ref --verify --quiet refs/remotes/origin/main; then
      default_branch="main"
    elif git -C "$repo" show-ref --verify --quiet refs/remotes/origin/master; then
      default_branch="master"
    fi
  fi

  if [ -n "$default_branch" ] && [ "$branch" = "$default_branch" ]; then
    sha="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"
    check_commit_handoff "$sha" "the pushed commit" || return 1
    return 0
  fi

  # `gh pr view <branch>` answers with the latest pull request in any state, so a merged or closed
  # one would pass for the open pull request the rule requires. `gh pr list` answers with open ones.
  pr_url="$(gh pr list --head "$branch" --state open --json url --jq '.[0].url // empty' 2>/dev/null || true)"
  if [ -z "$pr_url" ]; then
    # A pull request merged at exactly the branch head carries this session's work to its end, since
    # someone else may merge it before the session stops (issue 250). It stands in for the open one
    # and its handoff is judged the same way below. One merged before later commits is history, and
    # a closed, unmerged one never counts. An answer that cannot be read is not a confirmed absence.
    merged="$(gh pr list --head "$branch" --state merged --json url,headRefOid 2>/dev/null || printf 'unavailable')"
    pr_url="$(printf '%s' "$merged" | REQUIRE_PR_HEAD="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)" python3 -c '
import json
import os
import sys

try:
    pulls = json.loads(sys.stdin.read())
except ValueError:
    pulls = None
if not isinstance(pulls, list) or not all(isinstance(pull, dict) for pull in pulls):
    print("unreadable")
    sys.exit(0)
head = os.environ.get("REQUIRE_PR_HEAD", "")
for pull in pulls:
    if head and pull.get("headRefOid") == head and isinstance(pull.get("url"), str) and pull["url"]:
        print(pull["url"])
        break
' 2>/dev/null || printf 'unreadable')"
    if [ "$pr_url" = "unreadable" ]; then
      emit_block "The branch's merged pull requests could not be read, so the session handoff cannot be verified. Confirm a pull request carries this session's handoff before ending the task."
      return 1
    fi
  fi
  if [ -z "$pr_url" ]; then
    emit_block "No pull request is open for this branch. Open one against its base and post the session handoff on it before ending the task; on a repository the user owns, the session-handoff-on-pr rule pre-authorizes that pull request and comment."
    return 1
  fi

  # The session-handoff-on-pr rule: the pull request exists, but a session that ends without a
  # handoff leaves its successor nothing to read. The heading is matched literally because that is
  # what the rule tells the next session to search for. The bodies come back as one JSON array, so
  # a multi-line handoff is never split or merged, and the live (last) one is scanned for
  # attribution. Each carries its creation time, so a handoff an earlier session posted does not
  # count as this session's. The pull request is read by its URL, never by branch, as above.
  handoffs="$(gh pr view "$pr_url" --json comments --jq '[.comments[] | select(.body | test("## session handoff")) | {body, createdAt}]' 2>/dev/null || true)"
  verdict="$(judge_handoffs "$handoffs")"
  case "$verdict" in
    unreadable)
      emit_block "The pull request's comments could not be read, so the session handoff cannot be verified. Confirm the handoff is posted on $pr_url before ending the task."
      return 1
      ;;
    missing)
      emit_block "No session handoff comment on $pr_url. Post one opening with the heading '## session handoff' and carrying Delivered, Gates, Next, Blocked, Do not and Open decisions, per the session-handoff-on-pr rule, before ending the task."
      return 1
      ;;
    stale)
      emit_block "No session handoff comment on $pr_url since this session started; the ones there belong to earlier sessions. Post a new one opening with the heading '## session handoff' and carrying Delivered, Gates, Next, Blocked, Do not and Open decisions, per the session-handoff-on-pr rule, before ending the task."
      return 1
      ;;
    clean) ;;
    *)
      attribution_block "$pr_url" "$verdict"
      return 1
      ;;
  esac
  return 0
}

# check_merge <selector> <owner/repo or empty> <directory or empty>
# A pull request this session merged carries the handoff on its merge commit. The commit comes from
# the pull request, never from a checkout's HEAD, which may hold another session's merge.
check_merge() {
  selector="$1"
  slug="$2"
  where="${3:-}"
  [ -n "$where" ] && [ -d "$where" ] || where="$cwd"
  cd "$where" 2>/dev/null || return 0
  if ! command -v gh >/dev/null 2>&1; then
    emit_block "The GitHub CLI is unavailable, so the handoff on the pull request this session merged cannot be verified."
    return 1
  fi
  set -- pr view
  [ -z "$selector" ] || set -- "$@" "$selector"
  set -- "$@" --json mergeCommit --jq '.mergeCommit.oid // empty'
  [ -z "$slug" ] || set -- "$@" -R "$slug"
  if ! merge_sha="$(gh "$@" 2>/dev/null)"; then
    emit_block "The pull request this session merged (${selector:-the current branch}) could not be read, so the handoff on its merge commit cannot be verified. Confirm the handoff is posted on the merge commit before ending the task."
    return 1
  fi
  # A merge that did not land (a failed or queued one) has no merge commit to carry a handoff.
  [ -n "$merge_sha" ] || return 0
  check_commit_handoff "$merge_sha" "the merge commit of ${selector:-the current branch}" "$slug" || return 1
  return 0
}

payload="$(cat 2>/dev/null || true)"
fields="$(printf '%s' "$payload" | python3 -c '
import json
import sys

try:
    data = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError:
    data = {}
cwd = data.get("cwd")
print(cwd if isinstance(cwd, str) else "")
print("1" if data.get("stop_hook_active") else "0")
transcript = data.get("transcript_path")
print(transcript if isinstance(transcript, str) else "")
session_id = data.get("session_id")
print(session_id if isinstance(session_id, str) else "__REQUIRE_PR_NO_SESSION__")
' 2>/dev/null || true)"
cwd="$(printf '%s\n' "$fields" | sed -n 1p)"
active="$(printf '%s\n' "$fields" | sed -n 2p)"
transcript="$(printf '%s\n' "$fields" | sed -n 3p)"
session_id="$(printf '%s\n' "$fields" | sed -n 4p)"
[ "$session_id" != "__REQUIRE_PR_NO_SESSION__" ] || session_id=""
[ "$active" = "1" ] && exit 0
[ -n "$cwd" ] || exit 0

cwd_repo="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true)"

session_start=""
if [ -n "$session_id" ] && [ -d "$HOME/.claude/sessions" ]; then
  session_start="$(REQUIRE_PR_SESSION_ID="$session_id" python3 -c '
import glob
import json
import os

target = os.environ["REQUIRE_PR_SESSION_ID"]
for path in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        continue
    if record.get("sessionId") != target:
        continue
    started = record.get("startedAt")
    if isinstance(started, (int, float)):
        print(int(started) // 1000)
    break
' 2>/dev/null || true)"
fi

# Reflog entries are dated by when the ref moved, which `%gd` prints under --date=unix as
# ref@{<epoch>}. `%ct` is the commit's own committer date, so pushing an older commit would read
# as a push from before the session.
if [ -z "$session_start" ] && [ -n "$cwd_repo" ]; then
  recent_cutoff="$(( $(date +%s) - 3600 ))"
  session_start="$(git -C "$cwd_repo" log -g --date=unix --format='%gd' HEAD 2>/dev/null | awk -v cutoff="$recent_cutoff" '{ sub(/^.*@\{/, ""); sub(/\}$/, "") } $1 + 0 >= cutoff + 0 && (oldest == "" || $1 + 0 < oldest + 0) { oldest = $1 + 0 } END { if (oldest != "") print oldest }')"
fi

# The worktrees to judge: the cwd's, then every one the transcript shows the session writing in or
# running git against, each once.
have_transcript="0"
dirs="$cwd"
merges=""
if [ -n "$transcript" ] && [ -f "$FOOTPRINT" ]; then
  footprint="$(python3 "$FOOTPRINT" repos "$transcript" "$cwd" 2>/dev/null || printf 'unavailable')"
  if [ "$footprint" != "unavailable" ]; then
    have_transcript="1"
    dirs="$footprint"
    merges="$(python3 "$FOOTPRINT" merges "$transcript" 2>/dev/null || true)"
  fi
fi

seen=""
while IFS= read -r dir; do
  [ -n "$dir" ] || continue
  top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$top" ] || continue
  case "$seen" in *"|$top|"*) continue ;; esac
  seen="$seen|$top|"
  ( check_repo "$top" ) || exit 0
done <<EOF
$dirs
EOF

while IFS="$(printf '\037')" read -r selector slug where; do
  [ -n "$selector$where" ] || continue
  ( check_merge "$selector" "$slug" "$where" ) || exit 0
done <<EOF
$merges
EOF
exit 0
