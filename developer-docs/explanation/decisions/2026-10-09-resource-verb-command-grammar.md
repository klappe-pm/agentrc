# 2026-10-09-resource-verb-command-grammar

## status

accepted

## context

The engine began as a handful of top-level commands (`sync`, `check`, `diff`, `prune`, `reconcile`) and is growing resources for settings, projects, providers, adapters, the log and verification. Without a shared shape each new command would invent its own flags, output and prompting, and scripts and the terminal interface would have to learn each one. People and CI already call the top-level commands, so any new grammar has to leave them working. The full command tree is in [cli-design](../cli-design.md).

## decision

Every command is `stratarc <resource> <verb> [args]`, with help at the root, the resource and the verb. Output is human readable on a terminal and a stable JSON envelope with `--json`. In noninteractive mode a command never prompts: it fails with the message that names the flag to pass. The legacy top-level `sync`, `check`, `diff`, `prune` and `reconcile` stay as aliases so existing scripts keep working.

## alternatives

- Keep flat top-level verbs and add new ones as needed. This is the smallest change, but the command list grows without structure and help cannot be scoped to a thing the user is managing.
- Replace the legacy commands outright. This gives one grammar, but breaks every script and CI job that already calls them for no gain to those callers.
- Prompt for missing input even when no terminal is attached. This is friendlier by hand, but a prompt in a pipeline hangs, so failing with the flag to pass was chosen.

## consequences

- A new capability is added as a verb on a resource, and its help, JSON envelope and noninteractive behavior come from the same rules.
- The command reference can be generated from the argparse tree, which the [documentation site](2026-10-09-static-docs-site-built-from-the-repo.md) relies on.
- The aliases are a standing maintenance cost: they must keep their current meaning until a later record supersedes this one.
- Scripts must pass every required flag explicitly, since nothing will ask for it.
