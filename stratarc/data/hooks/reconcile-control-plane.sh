#!/usr/bin/env bash
# PostToolUse hook on Write, Edit and NotebookEdit. When a file the tool wrote
# lives in the stratarc source root, run that checkout's scripts/reconcile-control-plane.py so
# the control plane inventory (and, from the primary checkout, the active
# project snapshots) stays current after an agent authored source change.
# This hook writes nothing itself: the reconciler decides which checkouts get
# a snapshot, and it skips every project named in
# projects-root/public-targets.json.
#
# The written paths come from the payload's tool_input: file_path, filePath,
# notebook_path or path, and the "*** Add/Update/Delete File:" headers of an
# apply_patch body. A path that merely appears in written content is not a
# write into the source root and never triggers a run. The source root is
# $STRATARC_SOURCE, else $LLM_ROOT; with neither set the hook exits 0. A path
# inside it runs its reconciler; a path inside a linked worktree of the source
# root runs that worktree's reconciler when it holds both control-plane.md and
# scripts/reconcile-control-plane.py and shares the Git common directory with
# the source root.
set -euo pipefail

[ "${RUNTIME_HOOKS_DISABLE:-0}" = "1" ] && exit 0

root="${STRATARC_SOURCE:-${LLM_ROOT:-}}"
[ -n "$root" ] || exit 0
payload="$(cat)"

# Cheap prefilter before starting an interpreter on every Write and Edit: a
# write into the source root, relative or absolute, carries the checkout's path (or
# a worktree path that keeps the root's directory name) somewhere in the payload.
# Passing it only means the parser below looks; the parser decides.
case "$payload" in
  *"$root"*|*"/$(basename "$root")/"*) ;;
  *) exit 0 ;;
esac

command -v python3 >/dev/null 2>&1 || exit 0

# The payload goes to the parser on stdin, never through the environment: a
# large Write would push an environment variable past the argument size
# limit and the exec would fail before the parser ran.
targets="$(printf '%s' "$payload" | RECONCILE_HOOK_ROOT="$root" python3 -c '
import json, os, re, subprocess, sys

try:
    data = json.loads(sys.stdin.read())
except ValueError:
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)
tool_input = data.get("tool_input")
cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""

paths = []
if isinstance(tool_input, dict):
    for key in ("file_path", "filePath", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
header = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)

if isinstance(tool_input, dict):
    for key in ("command", "input", "patch"):
        value = tool_input.get(key)
        if isinstance(value, str) and (
            data.get("tool_name") == "apply_patch" or value.startswith("*** Begin Patch")
        ):
            paths.extend(header.findall(value))
elif data.get("tool_name") == "apply_patch" and isinstance(tool_input, str):
    paths.extend(header.findall(tool_input))

def real(path):
    return os.path.realpath(os.path.expanduser(path))

root = real(os.environ["RECONCILE_HOOK_ROOT"])

def git_common_dir(checkout):
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            env=git_env, capture_output=True, text=True, check=True, timeout=2,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return real(result.stdout.strip()) if result.stdout.strip() else None

def checkout_for(path):
    if not os.path.isabs(path):
        if not cwd:
            return None
        path = os.path.join(cwd, path)
    path = real(path)
    if path == root or path.startswith(root + os.sep):
        return root
    here = os.path.dirname(path)
    while True:
        if os.path.isfile(os.path.join(here, "control-plane.md")) and os.path.isfile(
            os.path.join(here, "scripts", "reconcile-control-plane.py")
        ):
            root_common = git_common_dir(root)
            if root_common and git_common_dir(here) == root_common:
                return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent

found_roots = []
for path in paths:
    found = checkout_for(path)
    if found and found not in found_roots:
        found_roots.append(found)
for found in found_roots:
    print(found)
' 2>/dev/null)" || exit 0

[ -n "$targets" ] || exit 0
while IFS= read -r target; do
  [ -f "$target/scripts/reconcile-control-plane.py" ] || continue
  python3 "$target/scripts/reconcile-control-plane.py" >/dev/null
done <<< "$targets"
