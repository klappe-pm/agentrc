# add-a-runtime

This guide is for a contributor who wants stratarc to support another agent runtime. It will explain how to write an adapter under `stratarc/adapters/`, map each source kind (instructions, rules, hooks, skills, commands, agents and permissions) to the runtime's native format, register the runtime so `stratarc.toml` can enable it, document it under [docs/reference/runtimes](../../docs/reference/runtimes/README.md), and add golden tests for its output. It will be written once the existing adapters are extracted and their shared interface is settled.

Until then, the useful preparation is to collect, for the runtime you have in mind, the directory it reads configuration from, the file it reads instructions from, and whether it has native equivalents for rules, hooks, skills, commands, agents and permission lists. That inventory is the first section of the adapter and of the runtime's reference page.
