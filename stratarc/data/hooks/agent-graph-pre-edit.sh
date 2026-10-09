#!/usr/bin/env bash
# PreToolUse (Edit|Write) launcher for agent-graph's Claude Code wiring.
#
# agent-graph keeps the hook source in its own checkout (wiring/hooks/pre-edit.sh).
# This launcher runs that script and prints its context only when it is worth the
# time. It never blocks an edit and it fails open: every missing piece, error and
# timeout prints nothing and exits 0.
#
# Cost control, because this fires on every Edit and Write in every project:
#   1. The agent-graph checkout, the global agent-graph binary and the indexed
#      project list (written by agent-graph-session-start.sh) must all exist.
#      Without the binary the wiring would fall back to `uv run`, which adds
#      start-up to every edit, so the hook stays silent instead.
#   2. The edited file must sit under $LLM_ROOT_PROJECTS_DIR/<project>/ (default
#      $HOME/projects/active) or $LLM_WORKTREE_ROOT/<project>/ (default
#      $HOME/projects/_worktrees), and <project> must be a line of the
#      indexed project list. Any other file costs one small perl run and no CLI.
#   3. The wiring script runs with a hard budget of the hook's total wall time
#      (default 300 ms, set with AGENT_GRAPH_PRE_EDIT_BUDGET_MS, start-up included).
#      Past it the wiring script's process group is killed and nothing is printed. A project is "indexed" when the graph holds a Project
#      node for its slug; a project with a node but no indexed file for this path
#      costs one capped call and prints nothing.
#
# Claude Code only: the wiring script prints Claude Code's hookSpecificOutput shape,
# so hooks/claude-agent-graph-hooks.json registers this for no other runtime.
#
# Env:
#   AGENT_GRAPH_ROOT               the agent-graph checkout; empty or unset means the hook does nothing
#   LLM_ROOT_PROJECTS_DIR          project checkout base, default $HOME/projects/active
#   LLM_WORKTREE_ROOT              linked worktree base, default $HOME/projects/_worktrees
#   AGENT_GRAPH_BIN                default $HOME/.local/bin/agent-graph when executable
#   AGENT_GRAPH_PRE_EDIT_BUDGET_MS default 300
#   XDG_CACHE_HOME                 default $HOME/.cache
#   RUNTIME_HOOKS_DISABLE          set to 1 to print nothing at all

# -u only: -e and pipefail would let a failed command end the hook early, against fail-open.
set -u

[ "${RUNTIME_HOOKS_DISABLE:-}" = "1" ] && exit 0

AGENT_GRAPH_ROOT="${AGENT_GRAPH_ROOT:-}"
[ -n "$AGENT_GRAPH_ROOT" ] || exit 0
export AGENT_GRAPH_ROOT
target="$AGENT_GRAPH_ROOT/wiring/hooks/pre-edit.sh"
[ -r "$target" ] || exit 0

if [ -z "${AGENT_GRAPH_BIN:-}" ] && [ -x "$HOME/.local/bin/agent-graph" ]; then
  AGENT_GRAPH_BIN="$HOME/.local/bin/agent-graph"
  export AGENT_GRAPH_BIN
fi
[ -n "${AGENT_GRAPH_BIN:-}" ] && [ -x "$AGENT_GRAPH_BIN" ] || exit 0

cache="${XDG_CACHE_HOME:-$HOME/.cache}/agent-graph/indexed-repos"
[ -r "$cache" ] || exit 0

perl -e '
  use strict; use warnings;
  use JSON::PP; use IO::Select; use Time::HiRes qw(time);
  my ($target, $cache, $ms) = @ARGV;
  my $t0 = time;
  $ms = 300 unless defined $ms && $ms =~ /^\d+$/;
  my $raw = do { local $/; <STDIN> };
  exit 0 unless defined $raw && length $raw;
  my $d = eval { decode_json($raw) } or exit 0;
  ref $d eq "HASH" or exit 0;
  my $ti = $d->{tool_input}; ref $ti eq "HASH" or exit 0;
  my $p = $ti->{file_path}; (defined $p && !ref $p && length $p) or exit 0;
  my $home = $ENV{HOME} // ""; length $home or exit 0;
  my $slug;
  my $projects = length($ENV{LLM_ROOT_PROJECTS_DIR} // "") ? $ENV{LLM_ROOT_PROJECTS_DIR} : "$home/projects/active";
  my $trees = length($ENV{LLM_WORKTREE_ROOT} // "") ? $ENV{LLM_WORKTREE_ROOT} : "$home/projects/_worktrees";
  s{/+$}{} for ($projects, $trees);
  for my $base ("$projects/", "$trees/") {
    next unless index($p, $base) == 0;
    ($slug) = substr($p, length $base) =~ m{^([^/]+)};
    last if defined $slug;
  }
  defined $slug or exit 0;
  open(my $fh, "<", $cache) or exit 0;
  my $hit = 0;
  while (my $l = <$fh>) { chomp $l; if ($l eq $slug) { $hit = 1; last } }
  close $fh;
  $hit or exit 0;

  pipe(my $out_r, my $out_w) or exit 0;
  pipe(my $in_r, my $in_w) or exit 0;
  my $pid = fork(); defined $pid or exit 0;
  if (!$pid) {
    setpgrp(0, 0);
    close $out_r; close $in_w;
    open(STDIN, "<&", $in_r) or exit 0;
    open(STDOUT, ">&", $out_w) or exit 0;
    open(STDERR, ">", "/dev/null");
    exec("/bin/bash", $target);
    exit 0;
  }
  setpgrp($pid, $pid);
  close $in_r; close $out_w;
  $SIG{PIPE} = "IGNORE";
  syswrite($in_w, $raw); close $in_w;
  # The budget is the hook wall time, not this process time: bash and perl start-up before
  # $t0 cost about 30 ms, so the child deadline is that much earlier.
  my $deadline = $t0 + $ms / 1000 - 0.035;
  my $sel = IO::Select->new($out_r);
  my ($buf, $eof) = ("", 0);
  while (!$eof) {
    my $left = $deadline - time;
    last if $left <= 0;
    next unless $sel->can_read($left);
    my $n = sysread($out_r, my $chunk, 65536);
    if (!defined $n) { last } elsif ($n == 0) { $eof = 1 } else { $buf .= $chunk }
  }
  if (!$eof) {
    kill "KILL", -$pid; kill "KILL", $pid;
    waitpid($pid, 0);
    exit 0;
  }
  waitpid($pid, 0);
  print $buf if $? == 0 && length $buf;
  exit 0;
' "$target" "$cache" "${AGENT_GRAPH_PRE_EDIT_BUDGET_MS:-300}" 2>/dev/null
exit 0
