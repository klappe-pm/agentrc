# cli-backlog

This page orders the work that separates the shipped command line from the [design](cli-design.md). Each item names what it needs first, so a contributor can pick the next one without reading the whole design.

## shipped

- The engine commands (`sync`, `check`, `diff`, `prune`, `reconcile`, `validate`, `projects`, `gen-rules-digest`, `components`, `init`) and `doctor`, with the global flags, the exit-code map and the message catalog.
- The `config` resource: `get`, `list`, `explain`, with the file and line behind every value.
- The `provider` resource (`list`, `show`, `add`, `edit`, `remove`, `test`) and the `adapter` resource (`list`, `show`, `register`, `remove`, `status`, `deprecate`), including the adapter manifest with its `supports` range and the deprecation prompt.
- The `~/.stratarc` home layout: `config.toml`, `sources.toml`, `providers/`, `adapters/`, `state/`, `backups/`, the backup-before-write rule and the cleaning rules, created by the first write run.
- The human readable log and the optional SQLite change log with its JSONL mirror, recorded by `sync` for the derived files, the plugin declarations, each runtime, the project deliveries, the permission sweep, the deploy record and every refusal.
- `verify run`, `last` and `show`, and `sync --verify` with `--rollback-on-drift`.
- The `log` resource: `show`, `tail`, `explain`, `export`, `enable`, `disable`, `prune`.
- The deploy gate: `sync` refuses an unsupported adapter and warns once for an outdated one.
- `doctor --permissions`, `--clean` (with `--yes`) and `--report`.
- The read-only local API and its published schema, as `stratarc api serve` and `stratarc api schema`.
- The `source`, `project`, `runtime`, `agent` and `account` resources, with the schema-validated editors behind their write verbs, the backup before every write and `--dry-run`. `source use` writes the active source root and every command reads it ([record](decisions/2026-10-09-the-active-source-root-is-resolved-in-one-place.md)).
- The terminal interface, `stratarc ui`, drawn with the optional Textual extra (`pip install 'stratarc[ui]'`).
- Version ranges in adapter manifests, compared against the detected runtime version.
- The private checks gate: a source root opts in to running its own checks before a deploy.
- The flags layer: `--set`, `--set-json` and `--set-mode` on `config get`, `list` and `explain`, shown by `config explain` as the highest layer ([record](decisions/2026-10-09-explain-follows-the-dispatch-relay-and-flags-are-a-layer.md)).
- The sub-agent relay chain in `config explain`: the inherited and withheld settings of a child agent and where its account came from.
- File modes: a file written into a source root keeps its mode and a new one is 0644, while the home stays 0600 ([record](decisions/2026-10-10-source-root-files-keep-their-mode-and-the-home-stays-private.md)). `account edit --set` and `--unset` also change JSON account files ([record](decisions/2026-10-10-json-accounts-support-set-and-unset.md)).
- `account edit --set` and `--unset` for TOML account files, `agent add` and `agent remove`, and the project status column, which `project enable` and `project disable` now keep in step ([record](decisions/2026-10-09-resource-verbs-fill-the-grammar-gaps.md)).
- In the terminal interface: validation of an edit before it is kept, and worker threads for the sync preview and verify ([record](decisions/2026-10-09-the-interface-validates-edits-and-keeps-slow-work-off-its-thread.md)), plus a CI workflow that drives the interface through a pseudo-terminal.
- Mutation proofs for the new command line tests ([record](decisions/2026-10-09-new-cli-tests-carry-mutation-proofs.md)).
- The `starc` alias was added and then dropped, so the command is `stratarc` only ([record](decisions/2026-10-09-drop-the-starc-alias.md)).
- The catalog message for every module code of the config, provider, adapter, log, verify and resource commands (`msg-1101` to `msg-1159`), including the generic `conflict` code, with a test that scans the modules for codes. `msg-1160` covers `stratarc ui` without a terminal, and `stratarc ui` checks the source root first.

## order

1. The `config` writers: `set`, `unset`, `edit`. The editors they need have shipped with the resources.
2. Rollback for project checkouts and the permission sweep. `--rollback-on-drift` restores runtime targets only.
3. The sync preview in the terminal interface captures standard output for the whole process, so only one such job can run at a time.
4. The `ui` CI workflow has not been run on GitHub, so its pseudo-terminal steps are unproven there.
5. If a short alias is ever shipped again, check it on macOS, Fedora and Alpine, in PyPI entry points and in the trademark registers, as well as the platforms checked for `starc`.
6. Move llm-root onto the published package, which the migration records adr-0007 and adr-0009 in that repository describe.
7. The first-run welcome screen. The documentation site and the generated command reference and error catalog have shipped.

## decided

- List-valued settings never default to replace or extend: the resolver refuses a list override that omits its mode ([record](decisions/2026-10-10-list-overrides-never-default-their-mode.md)).
