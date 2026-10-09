# cli-backlog

This page orders the work that separates the shipped command line from the [design](cli-design.md). Each item names what it needs first, so a contributor can pick the next one without reading the whole design.

## shipped

The engine commands (`sync`, `check`, `diff`, `prune`, `reconcile`, `validate`, `projects`, `gen-rules-digest`, `components`, `init`) and `doctor`, with the global flags, the exit-code map and the first error-message catalog.

## order

1. The `config` resource: `get`, `list`, `explain`. It needs a layer resolver that reports the file and line behind every value, which the project and agent overlays do not expose yet.
2. The `source`, `project`, `runtime` and `agent` resources: `list`, `show`, `edit`. They need item 1 and the schema-validated editors.
3. The `provider` and `adapter` resources, including registration, `adapter status` against the installed runtime version, and the deprecation prompt. They need an adapter manifest format with a `supports` range.
4. The `~/.stratarc` home layout: `sources.toml`, `providers/`, `adapters/`, `state/`, `backups/`, and the cleaning rules from the design. It needs item 3 for the registry files.
5. The human readable log and the optional SQLite change log. They need a change-event envelope shared with the other tools that read it.
6. `verify run`, the recursive write-back test. It needs item 5 to record `verified` and `drift` per file.
7. The `log` resource: `show`, `tail`, `explain`, `export`, `prune`. It needs item 5.
8. The read-only local API and its published schema. It needs items 1 and 5.
9. The terminal interface. It needs items 1, 2 and 6, and a decision on the drawing library.
10. The documentation site, with the command reference and the error catalog generated from the code, and the first-run welcome screen.

## known-defects

None recorded.

## open-questions

- Whether list-valued settings default to replace or extend when a file omits `mode`.
- Whether `~/.stratarc` or the XDG path is the primary home.
- Which Python library draws the terminal interface.
- Whether the private validator checks that an operator keeps should run in the deploy gate or only under `validate`.
