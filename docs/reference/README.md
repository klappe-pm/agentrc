# reference

Reference pages describe the machinery: the commands, the keys, the files each runtime receives. They are for looking something up while you work, so they are structured around the thing they describe rather than around a task, they state facts without instruction, and they aim to be complete rather than friendly. A reference page says what an option does; the how-to guides say when to reach for it.

## pages

- [cli](cli.md): every subcommand of the `stratarc` command with its arguments and exit statuses.
- [config](config.md): the `stratarc config` commands, the layers and their precedence, the flags layer and the agent relay.
- [resources](resources.md): the resource verbs for projects, runtimes, agents, accounts and sources.
- [ui](ui.md): the terminal interface, its keys, editing, slow work and exit statuses.
- [api](api.md): the read-only local API, its transport, routes, response envelope and limits.
- [configuration](configuration.md): every key in `stratarc.toml`, the JSON Schemas that validate the source root, and the environment variables that override them.
- [runtimes](runtimes/README.md): the supported runtimes, their default targets and one page per runtime: [claude](runtimes/claude.md), [codex](runtimes/codex.md), [cursor](runtimes/cursor.md), [gemini](runtimes/gemini.md) and [opencode](runtimes/opencode.md).
