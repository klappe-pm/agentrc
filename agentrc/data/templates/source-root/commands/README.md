# commands

Commands are prompts a user invokes by name, as `/<kebab-name>`. Each command is one Markdown file `commands/<kebab-name>.md` with YAML frontmatter carrying a `description` and, optionally, `argument-hint` (the arguments shown to the user) and `allowed-tools` (a comma separated tool list), followed by a Markdown body that is the prompt the agent runs.
