# cli

This page is the reference for the `agentrc` command: every subcommand, its arguments, its exit statuses and what it prints. It describes what the command does, not when to use it; the [tutorial](../tutorials/getting-started.md) and the [how-to guides](../how-to-guides/README.md) cover that. Once the engine is extracted it will document every option and example for each subcommand.

## global-options

| option | effect |
| --- | --- |
| `--version` | print `agentrc <version>` and exit 0 |
| `--help` | print the usage summary and exit 0 |

## init

```bash
agentrc init TARGET
```

Scaffolds a source root from the bundled template into `TARGET`. `TARGET` must be absent or an empty directory.

| exit status | meaning |
| --- | --- |
| 0 | the template was written; the count of files is printed |
| 1 | `TARGET` exists and is not a directory, or is not empty, or the packaged template is missing |

## sync-check-diff-prune-reconcile

```bash
agentrc sync
```

`sync`, `check`, `diff`, `prune` and `reconcile` are placeholders in this release. Each prints `agentrc <name>: not yet extracted` to standard error and exits 2. Their options and behavior are documented here as each one lands.
