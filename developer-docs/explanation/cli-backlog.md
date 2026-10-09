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
- The `starc` alias, a second console script for the same entry point.
- The catalog message for every module code of the config, provider, adapter, log, verify and resource commands (`msg-1101` to `msg-1154`), including the generic `conflict` code.

## order

1. The `config` writers: `set`, `unset`, `edit`. The editors they need have shipped with the resources.
2. A `--set KEY=VALUE` flag for the flags layer, so one command can override a setting for its own run and `config explain` shows the flag as the highest layer. It needs a place in the layer resolver for values that come from the command line, which the environment layer does not cover.
3. The sub-agent relay chain in `config explain`: for a sub-agent started by another agent, show which parent settings it inherited, which it did not, and where the account came from. It needs the dispatch record that says which agent started which, which the layers do not store yet.
4. Rollback for project checkouts and the permission sweep. `--rollback-on-drift` restores runtime targets only.
5. Gaps in the resources:
   - `account edit` has no `--set`, so changing one value means opening the editor.
   - `agent` has no `add` or `remove`.
   - `project enable` and `project disable` change the opt-in cells and do not touch the status column of the manifest.
6. Gaps in the terminal interface:
   - `e` does not validate the file after the editor closes.
   - `s` and `v` run the sync preview and the verify on the interface thread, so the screen waits.
   - No CI job drives the interface through a pseudo-terminal; the tests drive it headless.
7. Checks of the `starc` name that were not made: Windows system commands, Ubuntu, Fedora, Arch and Nix package names, and the trademark registers.
8. The first-run welcome screen. The documentation site and the generated command reference and error catalog have shipped.

## open-questions

- Whether list-valued settings default to replace or extend when a file omits `mode`.
