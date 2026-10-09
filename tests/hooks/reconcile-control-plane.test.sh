#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../stratarc/data/hooks" && pwd)"
HOOK="$DIR/reconcile-control-plane.sh"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/reconcile-hook-XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
root="$scratch/source-root"
marker="$scratch/ran"
mkdir -p "$root/scripts"
git -C "$root" init -q

# The stub records which checkout's reconciler ran, so a test can tell the
# live checkout's run apart from a linked worktree's.
stub='import os, pathlib; marker = pathlib.Path(os.environ["RECONCILE_MARKER"]); marker.open("a").write(str(pathlib.Path(__file__).resolve().parent.parent) + "\n")'
printf '%s\n' '#!/usr/bin/env python3' "$stub" > "$root/scripts/reconcile-control-plane.py"
git -C "$root" add scripts/reconcile-control-plane.py
git -C "$root" -c user.name=fixture -c user.email=fixture@example.invalid commit -qm fixture

other="$scratch/other-repo"
mkdir -p "$other"

unrelated="$scratch/unrelated-repo"
mkdir -p "$unrelated/scripts"
git -C "$unrelated" init -q
printf '%s\n' '# control-plane' > "$unrelated/control-plane.md"
printf '%s\n' '#!/usr/bin/env python3' "$stub" > "$unrelated/scripts/reconcile-control-plane.py"

worktree="$scratch/_worktrees/source-root/lane"
mkdir -p "$(dirname "$worktree")"
git -C "$root" worktree add -q -b fixture-lane "$worktree"
printf '%s\n' '# control-plane' > "$worktree/control-plane.md"
printf '%s\n' '#!/usr/bin/env python3' "$stub" > "$worktree/scripts/reconcile-control-plane.py"

pass=0
fail=0

run_hook() {
  rm -f "$marker"
  printf '%s' "$1" | LLM_ROOT="$root" RECONCILE_MARKER="$marker" bash "$HOOK"
}

expect_ran() {
  local label="$1" want="$2"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$(cd "$want" && pwd -P)" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'reconcile-control-plane.test: %s did not reconcile %s\n' "$label" "$want" >&2
  fi
}

expect_skipped() {
  local label="$1"
  if [ ! -e "$marker" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'reconcile-control-plane.test: %s reconciled when it should not have\n' "$label" >&2
  fi
}

expect_both_ran() {
  local label="$1" expected
  expected="$(cd "$root" && pwd -P)
$(cd "$worktree" && pwd -P)"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$expected" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'reconcile-control-plane.test: %s did not reconcile both checkouts in order\n' "$label" >&2
  fi
}

run_hook "{\"tool_input\":{\"file_path\":\"$root/rules/example.md\"}}"
expect_ran "a write into the source root" "$root"

plain_root="$scratch/plain/source-root"
mkdir -p "$plain_root/scripts"
printf '%s\n' '#!/usr/bin/env python3' "$stub" > "$plain_root/scripts/reconcile-control-plane.py"
rm -f "$marker"
printf '%s' "{\"tool_input\":{\"file_path\":\"$plain_root/rules/example.md\"}}" | LLM_ROOT="$plain_root" RECONCILE_MARKER="$marker" bash "$HOOK"
expect_ran "a write into the source root without git" "$plain_root"

rm -f "$marker"
printf '%s' "{\"tool_input\":{\"file_path\":\"$root/rules/example.md\"}}" | RUNTIME_HOOKS_DISABLE=1 LLM_ROOT="$root" RECONCILE_MARKER="$marker" bash "$HOOK"
expect_skipped "a disabled hook"

# A write into another repository whose content names the source root path is
# not a write into the source root.
run_hook "{\"tool_input\":{\"file_path\":\"$other/notes.md\",\"content\":\"see $root/rules/example.md and /source-root/AGENTS.md\"}}"
expect_skipped "a write elsewhere that mentions the source root path"

run_hook "{\"tool_input\":{\"file_path\":\"$unrelated/notes.md\",\"content\":\"see /source-root/AGENTS.md\"}}"
expect_skipped "a write into an unrelated checkout with a reconciler"

GIT_DIR="$root/.git" run_hook "{\"tool_input\":{\"file_path\":\"$unrelated/notes.md\",\"content\":\"see /source-root/AGENTS.md\"}}"
expect_skipped "an unrelated checkout despite an inherited GIT_DIR"

run_hook "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$other/notes.md\",\"content\":\"*** Update File: $root/rules/example.md\"}}"
expect_skipped "a write elsewhere whose content contains a patch header"

run_hook "{\"tool_input\":{\"file_path\":\"$other/a.md\",\"old_string\":\"x\",\"new_string\":\"$root/scripts/sync.py\"}}"
expect_skipped "an edit elsewhere that mentions the source root path"

# A relative path resolves against the payload's cwd.
run_hook "{\"cwd\":\"$root\",\"tool_input\":{\"file_path\":\"rules/example.md\"}}"
expect_ran "a relative write inside llm-root" "$root"

run_hook "{\"cwd\":\"$other\",\"tool_input\":{\"file_path\":\"rules/example.md\"}}"
expect_skipped "a relative write inside another repository"

# An apply_patch payload names its files in patch headers, not file_path.
run_hook "{\"tool_input\":{\"command\":\"*** Begin Patch\\n*** Update File: $root/rules/example.md\\n@@\\n-a\\n+b\\n*** End Patch\"}}"
expect_ran "an apply_patch into llm-root" "$root"

run_hook "{\"tool_name\":\"apply_patch\",\"tool_input\":{\"patch\":\"*** Begin Patch\\n*** Update File: $root/rules/example.md\\n*** Update File: $worktree/rules/example.md\\n*** End Patch\"}}"
expect_both_ran "an apply_patch into two checkouts"

run_hook "{\"tool_input\":{\"command\":\"*** Begin Patch\\n*** Update File: $other/a.md\\n+$root/rules/example.md\\n*** End Patch\"}}"
expect_skipped "an apply_patch elsewhere that mentions the source root path"

# A write into a linked llm-root worktree runs that worktree's reconciler,
# which keeps the worktree's own control-plane.md current.
run_hook "{\"tool_input\":{\"file_path\":\"$worktree/rules/example.md\"}}"
expect_ran "a write into a linked worktree" "$worktree"

# A Write larger than the argument size limit still reconciles: the payload
# reaches the parser on stdin, not through the environment.
big="$(head -c 2000000 /dev/zero | tr '\0' 'a')"
run_hook "{\"tool_input\":{\"file_path\":\"$root/rules/example.md\",\"content\":\"$big\"}}"
expect_ran "a write larger than the argument size limit" "$root"

# A write that never names llm-root starts no interpreter at all: the hook
# runs on every Write and Edit in every repository.
stubbin="$scratch/stub-bin"
mkdir -p "$stubbin"
printf '%s\n' '#!/usr/bin/env bash' "touch \"$scratch/python-started\"" 'exit 0' > "$stubbin/python3"
chmod +x "$stubbin/python3"
printf '%s' "{\"tool_input\":{\"file_path\":\"$other/a.md\",\"content\":\"plain\"}}" | PATH="$stubbin:$PATH" LLM_ROOT="$root" RECONCILE_MARKER="$marker" bash "$HOOK"
if [ ! -e "$scratch/python-started" ]; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  printf '%s\n' 'reconcile-control-plane.test: an unrelated write started python' >&2
fi

run_hook "not json at all $root/rules/example.md"
expect_skipped "an unparseable payload"

# End to end with the real reconciler: a write into a fixture source root runs
# it against $LLM_ROOT_PROJECTS_DIR, which writes each active project's
# .docs/llm-root-control-plane.md snapshot and nothing into a project named in
# projects-root/public-targets.json.
ENGINE_SCRIPTS="$DIR/../../../scripts"
if [ -f "$ENGINE_SCRIPTS/reconcile-control-plane.py" ]; then
real_root="$scratch/real/source-root"
real_projects="$scratch/real/projects"
mkdir -p "$real_root/rules" "$real_root/agents" "$real_root/projects-root"
# The whole scripts/ tree, since the reconciler's imports reach into it and
# ROOT is derived from the file's own resolved location (so no symlink).
cp -R "$ENGINE_SCRIPTS" "$real_root/scripts"
# scripts/public_targets.py loads hooks/lib/public-targets.py from the same tree.
mkdir -p "$real_root/hooks/lib"
cp "$DIR/lib/public-targets.py" "$real_root/hooks/lib/public-targets.py"
printf '%s\n' '# agents' > "$real_root/AGENTS.md"
printf '%s\n' '{"global": [], "common": [], "project": []}' > "$real_root/rules/tiers.json"
printf '%s\n' '# control-plane' > "$real_root/control-plane.md"
printf '%s\n' '{"targets": ["stratarc"]}' > "$real_root/projects-root/public-targets.json"
git -C "$real_root" init -q
git -C "$real_root" -c user.name=fixture -c user.email=fixture@example.invalid commit -q --allow-empty -m fixture
git init -q "$real_projects/active/demo"
git init -q "$real_projects/active/stratarc"
printf '%s' "{\"tool_input\":{\"file_path\":\"$real_root/rules/example.md\"}}" | LLM_ROOT="$real_root" LLM_ROOT_PROJECTS_DIR="$real_projects" bash "$HOOK"
if [ -f "$real_projects/active/demo/.docs/llm-root-control-plane.md" ] \
  && [ "$(ls -A "$real_projects/active/stratarc")" = ".git" ] \
  && ! grep -q stratarc "$real_root/control-plane.md" \
  && grep -q '| demo | active |' "$real_root/control-plane.md"; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  printf '%s\n' 'reconcile-control-plane.test: the real reconciler did not snapshot demo while leaving the public target alone' >&2
fi
else
  printf "%s\n" "reconcile-control-plane.test: skipped the real reconciler case: no scripts/reconcile-control-plane.py" >&2
fi

# STRATARC_SOURCE names the source root and wins over LLM_ROOT.
rm -f "$marker"
printf '%s' "{\"tool_input\":{\"file_path\":\"$root/rules/example.md\"}}" | STRATARC_SOURCE="$root" LLM_ROOT="$scratch/elsewhere" RECONCILE_MARKER="$marker" bash "$HOOK"
expect_ran "a write into the STRATARC_SOURCE root" "$root"

# With neither variable set the hook does nothing and exits 0.
rm -f "$marker"
printf '%s' "{\"tool_input\":{\"file_path\":\"$root/rules/example.md\"}}" | env -u STRATARC_SOURCE -u LLM_ROOT RECONCILE_MARKER="$marker" bash "$HOOK"
expect_skipped "an unset source root"

printf 'reconcile-control-plane.test: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
