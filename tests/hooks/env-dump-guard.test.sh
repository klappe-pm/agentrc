#!/usr/bin/env bash
# Behavioral tests for env-dump-guard.sh
#
# PreToolUse hook: hard-denies a shell command that prints a process, shell or
# launchd environment (the no-secret-exposure rule). Every case is a command
# string fed to the guard inside a hook payload; no command under test is ever
# run.
#
# Proof that each case depends on the detection logic: after the real run,
# the whole suite runs again against two copies of the guard whose detector is
# replaced by a stub. With an always-allow stub every deny case must read
# allow, and with an always-deny stub every allow case must read deny. That
# proves the verdicts come from the detector, not from the wrapper. Which
# detector branch each case pins was checked separately by mutating one
# branch at a time; the cases marked "pins" below exist for a branch no other
# case would catch.
#
# Platform cases come from an optional lib/private/env-dump-patterns.json, which
# the detector reads from beside itself when it exists. A third copy of the
# guard runs with the real detector and a synthetic pattern file: the generic
# forms are denied with or without it, and a platform form only with it.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../agentrc/data/hooks" && pwd)"
SCRIPT="$HERE/env-dump-guard.sh"
# shellcheck source=lib/hook-test.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hook-test.sh"

PASS=0
FAIL=0

GLDIR="$(mktemp -d "${TMPDIR:-/tmp}/env-dump-guard-log-XXXXXX")"
FIXTURE_HOME="$(mktemp -d "${TMPDIR:-/tmp}/env-dump-guard-home-XXXXXX")"
STUBS="$(mktemp -d "${TMPDIR:-/tmp}/env-dump-guard-stubs-XXXXXX")"
trap 'rm -rf "$GLDIR" "$FIXTURE_HOME" "$STUBS"' EXIT

fail() {
  FAIL=$((FAIL+1))
  printf '  env-dump-guard.sh FAIL  %s\n' "$*" >&2
}

# payload <tool_name or -> <command>: a PreToolUse payload. "-" leaves
# tool_name out, as the OpenCode bridge's unlabeled form does.
payload() {
  python3 -c '
import json, sys
tool, command = sys.argv[1], sys.argv[2]
body = {"tool_input": {"command": command}}
if tool != "-":
    body["tool_name"] = tool
print(json.dumps(body))
' "$1" "$2"
}

# decision <guard script> <payload>: prints deny or allow.
decision() {
  local out
  out=$(printf '%s' "$2" | HOME="$FIXTURE_HOME" RUNTIME_HOOKS_DISABLE=0 ALLOW_ENV_DUMP=0 GUARD_LOG_DIR="$GLDIR" bash "$1" 2>/dev/null || true)
  case "$out" in *'"permissionDecision":"deny"'*) echo deny ;; *) echo allow ;; esac
}

NL=$'\n'

DENY_CASES=(
  "env"
  "env -i"
  "env -u HOME"
  "env FOO=1"
  "/usr/bin/env"
  "FOO=1 env"
  "env 2>&1"
  "env > /tmp/env-out.txt"
  "printenv"
  "printenv OPENAI_API_KEY"
  "set"
  "export"
  "export -p"
  "declare"
  "declare -p"
  "declare -p HOME"
  "declare -x"
  "typeset -x"
  "launchctl print gui/501/com.example.job"
  "launchctl print system"
  "launchctl getenv PATH"
  "launchctl export"
  "launchctl procinfo 1"
  "ps e"
  "ps eww"
  "ps aux e"
  "ps auxe"
  "ps -E"
  "ps -axE"
  "ps -A -E"
  "ps -o pid,user -E"
  "cat /proc/1/environ"
  "strings /proc/\$\$/environ"
  "tr '\\0' '\\n' < /proc/self/environ"
  # Unquoted and double-quoted backticks/\$(...) are real command
  # substitution: a shell evaluates them.
  "docker inspect web"
  "docker container inspect web"
  "podman inspect web"
  "security find-generic-password -s svc -w"
  "security find-internet-password -g -s host.example"
  "security find-generic-password -gs svc"
  "security dump-keychain -d"
  "git status && env"
  "echo a | printenv"
  "ls; set"
  "(env)"
  "{ env; }"
  "echo \"\$(printenv)\""
  "echo \`env\`"
  "x=\$(env)"
  "diff <(env) /dev/null"
  "if env | grep -q X; then echo y; fi"
  "for i in 1; do export -p; done"
  "bash -c \"env\""
  "bash -lc 'launchctl print system'"
  "sh -c 'ps eww'"
  "zsh -c \"echo hi; export -p\""
  "eval \"set\""
  "ssh host env"
  "ssh -p 22 host -- printenv"
  "ssh host 'launchctl print gui/501'"
  "op run --env-file=app.env -- printenv"
  "python3 scripts/credentials.py run --profile github -- env"
  "llm-auth run --profile github -- env"
  "sudo env"
  "sudo -u root printenv"
  "command env"
  "exec printenv"
  "nohup env > out.txt"
  "timeout 5 env"
  "echo | xargs env"
  "env -i bash -c env"
  "docker exec web env"
  "docker exec -e A=1 -it web printenv"
  "kubectl exec pod -- env"
  "echo a${NL}env"
  "echo a # a note${NL}printenv"
  "bash <<EOF${NL}env${NL}EOF"
  "echo 'unbalanced ; printenv"
  # A << inside quotes or arithmetic is not a heredoc and hides nothing.
  "echo '<<X'${NL}env"
  "echo \"a <<END b\"${NL}printenv"
  "echo \$((1<<n))${NL}env"
  "git commit -m 'use <<EOF heredocs'${NL}env"
  # A heredoc a shell receives behind a wrapper or a pipe is commands.
  "sudo -u root bash <<EOF${NL}env${NL}EOF"
  "timeout 5 bash <<EOF${NL}printenv${NL}EOF"
  "cat <<EOF | sh${NL}env${NL}EOF"
  # Commands a shell reads from a pipe or a here-string.
  "echo env | sh"
  "printf 'printenv' | zsh"
  "bash <<< printenv"
  # Shell value options before -c.
  "bash -o pipefail -c env"
  "bash --rcfile /dev/null -c printenv"
  # A value option ending a cluster (pins _skip_options clusters).
  "sudo -Eu root env"
  "security -v find-generic-password -s x -w"
  "docker compose exec svc env"
  "docker run --rm -e A=1 img printenv"
  "docker inspect -f '{{json .Config.Env}}' web"
  "docker inspect --format '{{json .}}' web"
  "\$'env'"
  "ssh host -t env"
  "find . -exec env \\;"
  "stdbuf -oL env"
  "script -q /dev/null printenv"
  "readonly -p"
  # pins env -S
  "env -S 'bash -c env'"
  # pins line continuation
  "sudo \\${NL}env"
  # pins nice and watch
  "nice env"
  "nice -n 5 printenv"
  "watch env"
  "watch -n 1 'printenv'"
)

ALLOW_CASES=(
  "env HOME=/x cmd"
  "env -i PATH=/usr/bin cmd"
  "env -i bash -c 'echo hi'"
  "env FOO=1 python3 script.py"
  "env -u HOME ls"
  "ps -e"
  "ps -ef"
  "ps -eo pid,command"
  "ps -axo pid,etime,command"
  "ps -o pid,user -p 1"
  "ps aux"
  "ps -p 123"
  "launchctl list"
  "launchctl list | grep example-job"
  "launchctl print-disabled gui/501"
  "echo \$PATH"
  "grep env file"
  "grep -rn printenv docs"
  "git log --format=%H"
  "set -euo pipefail"
  "set -o"
  "export FOO=bar"
  "export -n FOO"
  "declare -x FOO=bar"
  "declare -a arr"
  "declare -f"
  "[ -n \"\${OPENAI_API_KEY+x}\" ] && echo set"
  "git commit -m 'block launchctl print and printenv; env dumps leak keys'"
  "git commit -F - <<'EOF'${NL}set${NL}env${NL}EOF"
  "cat <<EOF${NL}printenv${NL}EOF"
  # A single-quoted mention is inert data: a shell does not expand a
  # backtick, \$(...) or a bare path inside single quotes.
  "git commit -m 'block reads of /proc/self/environ'"
  "gh issue create --title x --body 'the /proc/1/environ file leaks secrets'"
  "docker ps"
  "docker exec web ls"
  "security find-generic-password -s svc"
  "python3 scripts/components.py --services"
  "ssh host uptime"
  "bash -c 'echo env'"
  "man env"
  "which printenv"
  "echo '# env'"
  "echo x # env"
  "cat /proc/cpuinfo"
  "printf '%s\\n' environment"
  "echo \`date\`"
  "command -v env"
  "command -V set"
  "docker inspect -f '{{.State.Status}}' web"
  "docker inspect --format '{{.State.Health.Status}}' web"
  "env --version"
  # pins the ps option-value skip (the value holds an e)
  "ps -o user"
  "ps -O user"
  # pins comment stripping
  "echo x #; env"
  "echo x # \$(env)"
  "ls # it's printenv"
  # pins line continuation
  "env -i \\${NL}  bash -c 'echo hi'"
  "env FOO=1 \\${NL}  python3 x.py"
  # pins the security value-flag skip
  "security find-generic-password -s -gw"
  "bash script.sh"
  "echo printenv | bash script.sh"
  "echo env | grep x"
  "readonly FOO=1"
)

# Every case's Bash payload, built once in one interpreter and reused by all
# three suites (bash 3.2 on macOS has no mapfile -d, so a read loop).
bash_payloads() {
  python3 -c '
import json, sys
for command in sys.argv[1:]:
    sys.stdout.write(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}) + "\0")
' "$@"
}
DENY_PAYLOADS=()
while IFS= read -r -d '' p; do DENY_PAYLOADS+=("$p"); done < <(bash_payloads "${DENY_CASES[@]}")
ALLOW_PAYLOADS=()
while IFS= read -r -d '' p; do ALLOW_PAYLOADS+=("$p"); done < <(bash_payloads "${ALLOW_CASES[@]}")

run_suite() {
  local guard="$1" mode="$2" i got want
  for i in "${!DENY_CASES[@]}"; do
    want=deny
    [ "$mode" = "allow-stub" ] && want=allow
    got=$(decision "$guard" "${DENY_PAYLOADS[$i]}")
    if [ "$got" = "$want" ]; then PASS=$((PASS+1)); else fail "[$mode] deny case $(printf '%q' "${DENY_CASES[$i]}") got=$got"; fi
  done
  for i in "${!ALLOW_CASES[@]}"; do
    want=allow
    [ "$mode" = "deny-stub" ] && want=deny
    got=$(decision "$guard" "${ALLOW_PAYLOADS[$i]}")
    if [ "$got" = "$want" ]; then PASS=$((PASS+1)); else fail "[$mode] allow case $(printf '%q' "${ALLOW_CASES[$i]}") got=$got"; fi
  done
}

# 1. The real guard.
run_suite "$SCRIPT" real

# Every runtime's shell tool, and any other tool that runs a command, is
# judged; a tool with no command is not.
for tool in run_shell_command Shell Monitor mcp__desktop-commander__start_process -; do
  [ "$(decision "$SCRIPT" "$(payload "$tool" printenv)")" = deny ] && PASS=$((PASS+1)) || fail "tool $tool printenv not denied"
done
[ "$(decision "$SCRIPT" '{"tool_name":"Write","tool_input":{"file_path":"/tmp/x.sh","content":"printenv"}}')" = allow ] \
  && PASS=$((PASS+1)) || fail "a Write of the word was judged as a command"
[ "$(decision "$SCRIPT" '{"tool_name":"Bash","tool_input":{"command":["bash","-lc","env"]}}')" = deny ] \
  && PASS=$((PASS+1)) || fail "a list-form command (Codex) was not judged"
[ "$(decision "$SCRIPT" '{"tool_name":"exec_command","tool_input":{"cmd":"env"}}')" = deny ] \
  && PASS=$((PASS+1)) || fail "a cmd-keyed command was not judged"
# A command too long to judge inside the hook timeout is denied unread; one
# under the cap is judged.
long_arg="$(python3 -c 'print("a" * 70000)')"
[ "$(decision "$SCRIPT" "$(payload Bash "echo $long_arg")")" = deny ] \
  && PASS=$((PASS+1)) || fail "a command over the size cap was not denied"
[ "$(decision "$SCRIPT" "$(payload Bash "echo ${long_arg:0:60000}")")" = allow ] \
  && PASS=$((PASS+1)) || fail "a command under the size cap was denied"
# A payload shape the detector does not expect is still judged, never
# allowed through a detector exception.
[ "$(decision "$SCRIPT" '{"tool_name":["Bash"],"tool_input":{"command":"printenv"}}')" = deny ] \
  && PASS=$((PASS+1)) || fail "an odd payload shape failed open"

# The deny names the rule, the command class, and a safe alternative.
out=$(printf '%s' "$(payload Bash 'launchctl print gui/501/x')" | HOME="$FIXTURE_HOME" GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
case "$out" in
  *"(launchctl print)"*no-secret-exposure*) PASS=$((PASS+1)) ;;
  *) fail "deny message lacks the rule or the class: $out" ;;
esac
case "$out" in
  *"launchctl list | grep"*ALLOW_ENV_DUMP=1*) PASS=$((PASS+1)) ;;
  *) fail "deny message lacks the safe alternative or the bypass: $out" ;;
esac

# The bypass and the disable switch pass a dump through.
for var in ALLOW_ENV_DUMP RUNTIME_HOOKS_DISABLE; do
  out=$(printf '%s' "$(payload Bash env)" | env "$var=1" HOME="$FIXTURE_HOME" GUARD_LOG_DIR="$GLDIR" bash "$SCRIPT" 2>/dev/null || true)
  [ -z "$out" ] && PASS=$((PASS+1)) || fail "$var=1 did not pass the command through"
done
if hook_test_disable_guard "$SCRIPT"; then PASS=$((PASS+1)); else fail "disabled guard not silent"; fi

# Registered once in hooks.json with no matcher, so every runtime's shell tool
# reaches it whatever that tool is called.
registration="$(python3 -c '
import json, sys
groups = json.load(open(sys.argv[1]))["PreToolUse"]
hits = [g.get("matcher", "") for g in groups for h in g.get("hooks", []) if h.get("command", "").endswith("/env-dump-guard.sh")]
print("ok" if hits in ([""], ["*"]) else "bad " + repr(hits))
' "$HERE/hooks.json")"
[ "$registration" = ok ] && PASS=$((PASS+1)) || fail "registration on every tool: $registration"

# The OpenCode bridge runs the guard on its bash tool.
grep -q 'invoke("env-dump-guard.sh", payload("PreToolUse", id, cwd, "Bash"' "$HERE/opencode-runtime-hooks.ts" \
  && PASS=$((PASS+1)) || fail "opencode-runtime-hooks.ts does not run env-dump-guard.sh on bash"

# Deny records went to GUARD_LOG_DIR and nothing reached the fixture HOME.
home_files="$(find "$FIXTURE_HOME" -type f | wc -l | tr -d ' ')"
log_records="$(cat "$GLDIR"/* 2>/dev/null | grep -c 'env-dump-guard' || true)"
if [ "$home_files" = "0" ] && [ "${log_records:-0}" -gt 0 ]; then
  PASS=$((PASS+1))
else
  fail "telemetry isolation (fixture HOME files=$home_files, GUARD_LOG_DIR records=${log_records:-0})"
fi

# 2. The same cases without the detection logic: each must flip.
for stub in allow-stub deny-stub; do
  mkdir -p "$STUBS/$stub/lib"
  cp "$SCRIPT" "$STUBS/$stub/env-dump-guard.sh"
  for rel in log.sh guard-utils.sh guard-log.sh; do
    cp "$HERE/lib/$rel" "$STUBS/$stub/lib/$rel"
  done
done
printf 'import sys\nsys.stdin.read()\n' > "$STUBS/allow-stub/lib/env-dump-detect.py"
printf 'import sys\nsys.stdin.read()\nprint("stub")\n' > "$STUBS/deny-stub/lib/env-dump-detect.py"
run_suite "$STUBS/allow-stub/env-dump-guard.sh" allow-stub
run_suite "$STUBS/deny-stub/env-dump-guard.sh" deny-stub

# 3. The generic detector alone: the real guard and detector copied without
# lib/private/, as a checkout without the private pattern file runs them.
PRIVATE="$STUBS/synthetic-env-dump-patterns.json"
cat > "$PRIVATE" <<'JSON'
{
  "version": 1,
  "heredoc_receivers": ["cloudctl"],
  "commands": {
    "cloudctl": {
      "value_options": ["-s", "--service"],
      "prints": [
        {
          "subcommands": ["variables", "variable", "vars"],
          "label": "cloudctl variables (prints values)",
          "unless_flags": ["--help", "-h"],
          "unless_operand": ["set"]
        }
      ],
      "runs": [{"subcommands": ["ssh", "run", "shell"], "operands_without_dashes": ["run"]}]
    }
  },
  "fallback": [{"label": "cloudctl variables", "pattern": "\\bcloudctl\\b[^\\n;|&]*\\s(variables|variable|vars)(\\s|$)"}]
}
JSON
GENERIC="$STUBS/generic/env-dump-guard.sh"
mkdir -p "$STUBS/generic/lib"
cp "$SCRIPT" "$GENERIC"
for rel in log.sh guard-utils.sh guard-log.sh env-dump-detect.py; do
  cp "$HERE/lib/$rel" "$STUBS/generic/lib/$rel"
done
[ ! -e "$STUBS/generic/lib/private" ] || fail "the generic copy carries lib/private"
# Every generic form is still denied with no private file.
for c in "printenv" "env" "env -i" "launchctl print system" "ps eww" "cat /proc/1/environ" "bash -c env" "ssh host printenv" "docker inspect web"; do
  [ "$(decision "$GENERIC" "$(payload Bash "$c")")" = deny ] && PASS=$((PASS+1)) || fail "[generic] $c not denied without the private pattern file"
done
# The platform forms are not: they come from the pattern file and nowhere else.
for c in "cloudctl variables" "cloudctl ssh -- env" "cloudctl run env"; do
  [ "$(decision "$GENERIC" "$(payload Bash "$c")")" = allow ] && PASS=$((PASS+1)) || fail "[generic] $c judged without the private pattern file"
done
# A private file that cannot be read is treated as absent, never as a reason
# to stop judging: the generic forms stay denied.
mkdir -p "$STUBS/generic/lib/private"
printf '{not json' > "$STUBS/generic/lib/private/env-dump-patterns.json"
for c in "printenv" "env"; do
  [ "$(decision "$GENERIC" "$(payload Bash "$c")")" = deny ] && PASS=$((PASS+1)) || fail "[malformed private] $c not denied"
done
# A malformed entry inside a readable file is skipped, and the rest still loads.
printf '{"commands": {"cloudctl": {"prints": [7, {"subcommands": ["variables"], "label": "x"}]}}, "fallback": [{"pattern": "("}, 3]}' > "$STUBS/generic/lib/private/env-dump-patterns.json"
[ "$(decision "$GENERIC" "$(payload Bash "cloudctl variables")")" = deny ] && PASS=$((PASS+1)) || fail "[partial private] a well-formed entry beside a malformed one was not loaded"
[ "$(decision "$GENERIC" "$(payload Bash "printenv")")" = deny ] && PASS=$((PASS+1)) || fail "[partial private] printenv not denied"
[ "$(decision "$GENERIC" "$(payload Bash "git status")")" = allow ] && PASS=$((PASS+1)) || fail "[partial private] a bad fallback pattern denied an ordinary command"
# With the synthetic pattern file copied in, the copy judges the platform forms as the real guard does.
cp "$PRIVATE" "$STUBS/generic/lib/private/env-dump-patterns.json"
[ "$(decision "$GENERIC" "$(payload Bash "cloudctl variables")")" = deny ] && PASS=$((PASS+1)) || fail "[private copied] cloudctl variables not denied"
[ "$(decision "$GENERIC" "$(payload Bash "cloudctl variable set KEY=value")")" = allow ] && PASS=$((PASS+1)) || fail "[private copied] cloudctl variable set denied"

printf 'env-dump-guard.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
