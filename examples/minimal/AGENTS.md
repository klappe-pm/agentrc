# agents

This source root holds the instructions, rules, hooks, skills, commands, agents and permissions that `agentrc sync` compiles into each enabled agent runtime. Runtime directories such as `~/.claude` are generated output; edit the files here, not the deployed copies.

## source-and-output-boundary

Edit only this source root. A later `agentrc sync` overwrites every runtime target named in `agentrc.toml`, so a change made directly in a runtime directory is lost.

## editing-and-sync

Hook registration belongs in `hooks/hooks.json`. Permissions belong in `permissions.json`. External components (MCP servers, plugins, third-party skills, dependencies) belong in `components.json`. Project-local configuration belongs in `projects-root/<project>/`. After an edit, run `agentrc sync` to deploy it, or `agentrc diff` first to preview what would change.

## authority-order

Inside a project, apply that project's validators and CI first, its own instruction files second, and this source root's configuration third.
