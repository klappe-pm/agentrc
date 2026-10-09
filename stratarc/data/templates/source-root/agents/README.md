# agents

Agents are subagent definitions a session can delegate a task to. Each agent is one Markdown file `agents/<kebab-name>.md` with YAML frontmatter carrying `name` (equal to the filename stem), `description` (when to delegate to it) and, optionally, `tools` (a comma separated tool list) and `model` (a runtime alias such as `opus`, `sonnet` or `haiku`), followed by a Markdown body that is the agent's system prompt.
