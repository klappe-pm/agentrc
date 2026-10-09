#!/usr/bin/env bash
# Deterministic linter for Claude Code hook scripts.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hooks_dir="$(cd "$script_dir/.." && pwd)/agentrc/data/hooks"
tests_dir="$(cd "$script_dir/.." && pwd)/tests/hooks"
strict=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --hooks-dir)
      hooks_dir="$2"
      shift 2
      ;;
    --tests-dir)
      tests_dir="$2"
      shift 2
      ;;
    --strict)
      strict=1
      shift
      ;;
    -h|--help)
      printf 'usage: hook-lint.sh [--hooks-dir DIR] [--tests-dir DIR] [--strict]\n'
      exit 0
      ;;
    *)
      printf 'ERROR unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

errors=0
warnings=0

emit_error() {
  printf 'ERROR %s\n' "$*" >&2
  errors=$((errors + 1))
}

emit_warning() {
  printf 'WARN  %s\n' "$*" >&2
  warnings=$((warnings + 1))
}

if [ ! -d "$hooks_dir" ]; then
  printf 'hook-lint: no hooks dir at %s\n' "$hooks_dir"
  exit 0
fi

while IFS= read -r hook; do
  [ -n "$hook" ] || continue
  rel="${hook#"$hooks_dir"/}"
  first="$(head -n 1 "$hook")"
  if [ "$first" != "#!/usr/bin/env bash" ]; then
    emit_error "$rel: shebang must be '#!/usr/bin/env bash'"
  fi
  if ! grep -qE '^set -euo pipefail|^set -u' "$hook"; then
    emit_error "$rel: missing set safety flags"
  fi
  if [ "$rel" != "worktree-create.sh" ] && ! grep -q 'RUNTIME_HOOKS_DISABLE' "$hook"; then
    if [ "$strict" -eq 1 ]; then
      emit_error "$rel: does not honor RUNTIME_HOOKS_DISABLE=1"
    else
      emit_warning "$rel: does not honor RUNTIME_HOOKS_DISABLE=1"
    fi
  fi
  # A sibling test sits beside the hook or in the tests directory.
  test_name="$(basename "${hook%.sh}").test.sh"
  case "$rel" in
    worktree-create.sh|worktree-remove.sh|worktree-validate.sh)
      test_name="worktree-lifecycle.test.sh"
      ;;
  esac
  if [ ! -f "$hooks_dir/$test_name" ] && [ ! -f "$tests_dir/$test_name" ]; then
    if [ "$strict" -eq 1 ]; then
      emit_error "$rel: missing sibling test"
    else
      emit_warning "$rel: missing sibling test"
    fi
  fi
done < <(find "$hooks_dir" -maxdepth 1 -type f -name '*.sh' -not -name '*.test.sh' | sort)

if [ "$errors" -gt 0 ]; then
  printf 'hook-lint: %s error(s), %s warning(s)\n' "$errors" "$warnings" >&2
  exit 1
fi

printf 'hook-lint: clean (%s warning(s))\n' "$warnings"
