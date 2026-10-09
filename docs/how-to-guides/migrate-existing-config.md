# migrate-existing-config

This guide is for someone who already maintains runtime configuration by hand, in `~/.claude`, `~/.codex` or another runtime directory, and wants to bring it under one stratarc source root without losing anything. It will explain which existing files map to rules, skills, commands, agents, hooks and permissions, how to import them without losing local changes, and how to compare the first `stratarc diff` against what each runtime holds today.

It will be written once `sync` and `diff` are extracted. Until then, the safe preparation is to scaffold a source root with `stratarc init` beside your existing configuration and leave the runtime directories untouched; nothing stratarc does today writes into them.
