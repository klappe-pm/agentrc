#!/usr/bin/env bash
# SessionStart launcher for agent-graph's Claude Code wiring.
#
# agent-graph keeps the hook source in its own checkout (wiring/hooks/session-start.sh).
# This launcher is the only agentrc file: it runs that script when the checkout is
# present and prints nothing and exits 0 when it is not, so a machine without the
# checkout starts sessions exactly as before. Nothing is copied out of the checkout.
#
# Before it runs the wiring script it starts one detached refresh of the indexed
# project list (${XDG_CACHE_HOME:-$HOME/.cache}/agent-graph/indexed-repos, one project
# slug per line, built from `agent-graph list-nodes --kind Project`). The pre-edit
# launcher reads that list to skip every checkout the graph does not hold, without
# calling the CLI. The refresh runs at most once an hour and never delays the hook.
#
# Claude Code only: the wiring script prints Claude Code's hookSpecificOutput shape,
# so hooks/claude-agent-graph-hooks.json registers this for no other runtime.
#
# Fail-open: any missing piece prints nothing and exits 0. This hook never blocks.
#
# Env:
#   AGENT_GRAPH_ROOT    the agent-graph checkout; empty or unset means the hook does nothing
#   AGENT_GRAPH_BIN     default $HOME/.local/bin/agent-graph when executable
#   XDG_CACHE_HOME      default $HOME/.cache
#   RUNTIME_HOOKS_DISABLE  set to 1 to print nothing at all

# -u only: -e and pipefail would let a failed command end the hook early, against fail-open.
set -u

[ "${RUNTIME_HOOKS_DISABLE:-}" = "1" ] && exit 0

AGENT_GRAPH_ROOT="${AGENT_GRAPH_ROOT:-}"
[ -n "$AGENT_GRAPH_ROOT" ] || exit 0
export AGENT_GRAPH_ROOT
target="$AGENT_GRAPH_ROOT/wiring/hooks/session-start.sh"
[ -r "$target" ] || exit 0

if [ -z "${AGENT_GRAPH_BIN:-}" ] && [ -x "$HOME/.local/bin/agent-graph" ]; then
  AGENT_GRAPH_BIN="$HOME/.local/bin/agent-graph"
  export AGENT_GRAPH_BIN
fi

refresh_indexed_repos() {
  local cache_dir cache lock
  [ -n "${AGENT_GRAPH_BIN:-}" ] && [ -x "$AGENT_GRAPH_BIN" ] || return 0
  cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/agent-graph"
  cache="$cache_dir/indexed-repos"
  lock="$cache.lock"
  [ -z "$(find "$cache" -mmin -60 2>/dev/null)" ] || return 0
  mkdir -p "$cache_dir" 2>/dev/null || return 0
  if [ -n "$(find "$lock" -maxdepth 0 -mmin +5 2>/dev/null)" ]; then
    rmdir "$lock" 2>/dev/null
  fi
  mkdir "$lock" 2>/dev/null || return 0
  (
    "$AGENT_GRAPH_BIN" list-nodes --kind Project --limit 10000 2>/dev/null \
      | perl -MJSON::PP -e '
          local $/; my $d = eval { decode_json(<STDIN>) } or exit 1;
          ref $d eq "HASH" && ref $d->{rows} eq "ARRAY" or exit 1;
          my @slugs = map { (ref $_ eq "HASH" && defined $_->{id} && $_->{id} =~ /^project:(.+)$/) ? $1 : () } @{$d->{rows}};
          exit 1 unless @slugs;
          print "$_\n" for sort @slugs;' >"$cache.tmp" 2>/dev/null \
      && mv "$cache.tmp" "$cache"
    rm -f "$cache.tmp"
    rmdir "$lock" 2>/dev/null
  ) </dev/null >/dev/null 2>&1 &
}

refresh_indexed_repos

exec /bin/bash "$target"
