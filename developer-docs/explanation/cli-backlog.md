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
- The catalog message for every module code of the config, provider, adapter, log and verify commands (`msg-1121` to `msg-1138`); only the generic `conflict` code is still shown in the module's own words.

## order

1. The `source`, `project`, `runtime` and `agent` resources: `list`, `show`, `edit`. They need the schema-validated editors; the `config` resolver they build on has shipped.
2. The `config` writers: `set`, `unset`, `edit`. They need the editors from item 1.
3. A `--set KEY=VALUE` flag for the flags layer, so one command can override a setting for its own run and `config explain` shows the flag as the highest layer. It needs a place in the layer resolver for values that come from the command line, which the environment layer does not cover.
4. The sub-agent relay chain in `config explain`: for a sub-agent started by another agent, show which parent settings it inherited, which it did not, and where the account came from. It needs the dispatch record that says which agent started which, which the layers do not store yet.
5. Rollback for project checkouts and the permission sweep. `--rollback-on-drift` restores runtime targets only.
6. The terminal interface. It needs items 1 and 2, and a decision on the drawing library.
7. The documentation site, with the command reference and the error catalog generated from the code, and the first-run welcome screen.

## open-questions

- Whether list-valued settings default to replace or extend when a file omits `mode`.
- Whether `~/.stratarc` or the XDG path is the primary home.
- Which Python library draws the terminal interface.
- Whether the private validator checks that an operator keeps should run in the deploy gate or only under `validate`.
