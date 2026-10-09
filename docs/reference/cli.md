# cli

This page is the reference for the `stratarc` command: every subcommand, its arguments, its exit statuses and what it prints. It describes what the command does, not when to use it; the [tutorial](../tutorials/getting-started.md) and the [how-to guides](../how-to-guides/README.md) cover that. `tests/test_cli.py` fails when a command or option in the parser is missing from this page. The design for the commands that do not exist yet is in [cli-design](../../developer-docs/explanation/cli-design.md).

## global-options

Global options are accepted before or after the command. Each one lasts for the one command and is undone afterwards. The engine reads the variables they set when it needs them, never when it is imported.

| option | effect |
| --- | --- |
| `--root PATH` | the source root; sets `STRATARC_SOURCE` |
| `--home PATH` | the home every runtime target and `~` hangs under; sets `STRATARC_HOME` |
| `--projects-root PATH` | the directory that holds project checkouts; sets `LLM_ROOT_PROJECTS_DIR` |
| `--owner NAME` | the GitHub owner whose repositories are managed; sets `STRATARC_GITHUB_OWNER` |
| `--json` | print one JSON envelope instead of text |
| `--debug` | print a traceback when a command stops unexpectedly |
| `--version` | print `stratarc <version>` and exit 0 |
| `--help` | print the usage summary and exit 0 |

Without a flag, each value comes from its variable, then from `stratarc.toml`, then from the default.

## exit-statuses

| status | meaning |
| --- | --- |
| 0 | ok |
| 1 | failure: the command ran and did not succeed |
| 2 | invalid input: a bad argument, an unknown source root, an invalid `stratarc.toml`, or a refusal by the engine |
| 3 | denied: the home is missing or not writable |
| 4 | conflict: the target already holds something |
| 5 | unavailable: a packaged resource is missing |
| 6 | drift: the deployed files differ from what the source renders |
| 130 | interrupted |

A command that reports drift (`check`, `reconcile --check`, `projects --check`, `projects --verify`, `gen-rules-digest --check`) exits 6 where its module returns 1. The engine returns 2 for every refusal, so the branch and stale-deploy guards exit 2 rather than 3.

## the-json-envelope

With `--json` a command prints exactly one object on standard output and exits with the same status as without it.

```json
{"ok": false, "data": null, "error": {"code": "msg-1001", "message": "...", "param": "root", "hint": "..."}}
```

`ok` is true only for status 0. `data` is the command's result, or null when there is none. For an engine command it holds `command`, `exit`, and the `stdout` and `stderr` the module wrote. `error.code` is a message id from the catalog below, or one of `failure`, `invalid-input`, `denied`, `conflict`, `unavailable`, `drift` and `interrupted` when an engine module failed without one.

## error-messages

Without `--json`, an error prints two lines to standard error: the id and the problem, then the recovery. The wording lives in `stratarc/messages.py`.

| id | status | problem | recovery |
| --- | --- | --- | --- |
| `msg-1001` | 2 | The source root does not exist or is not a directory. | Pass an existing directory with `--root`, or create one with `stratarc init`. |
| `msg-1002` | 2 | The configuration cannot be used. | Fix `stratarc.toml` in the source root, or remove it to use the defaults. |
| `msg-1003` | 3 | The home directory is missing or cannot be written to. | Make it writable, or point at another one with `--home` or `STRATARC_HOME`. |
| `msg-1004` | 2 | The runtime is not known. | Use one of the runtimes listed in the message. |
| `msg-1005` | 4 | The target exists and is not empty. | Choose a path that does not exist, or empty the directory first. |
| `msg-1006` | 2 | The target exists and is not a directory. | Choose a path that does not exist, or an empty directory. |
| `msg-1007` | 5 | The packaged source-root template is missing from this install. | Reinstall stratarc. |
| `msg-1008` | 1 | The command stopped unexpectedly. | Run it again with `--debug` to see the traceback. |

## init

```bash
stratarc init TARGET
```

Scaffolds a source root from the bundled template into `TARGET`. `TARGET` must be absent or an empty directory. It prints `stratarc init: wrote N files to TARGET`.

| exit status | meaning |
| --- | --- |
| 0 | the template was written |
| 2 | `TARGET` exists and is not a directory (`msg-1006`) |
| 4 | `TARGET` is not empty (`msg-1005`) |
| 5 | the packaged template is missing (`msg-1007`) |

## doctor

```bash
stratarc doctor
```

Reports the health of the install without changing anything: the Python and stratarc versions, the home and whether it is writable, which source root was resolved and from what, the registered adapter count, each runtime with whether it is enabled and whether its target directory exists, and the measured start time in milliseconds (from loading the command line module to the start of the checks). With `--json` the same facts are the envelope's `data`. It exits with the status of the first problem it found, or 0.

## sync

```bash
stratarc sync [--only RUNTIME] [--allow-branch BRANCH]... [--dry-run] [--list]
```

Renders the source root into every enabled runtime that has a target directory, then into the managed project checkouts. It writes only the files it owns. `--only` limits the run to one runtime and skips project delivery. `--allow-branch` names an extra branch the source may deploy from when `components.json` declares environments, and can be repeated. `--dry-run` prints what would change and writes nothing, the same as `diff`. `--list` prints each runtime and whether its target exists.

The home must exist and be writable (`msg-1003`) unless `--dry-run` or `--list` is given. An unknown `--only` value exits 2 (`msg-1004`). The engine returns 0 on success and 2 when it refuses to deploy.

## check

```bash
stratarc check [--only RUNTIME] [--allow-branch BRANCH]...
```

Validates the source, then compares what each runtime directory holds with what the source would render. It changes nothing and lists every file that is out of date and what a runtime holds that the source does not own. It exits 6 when anything is stale.

## diff

```bash
stratarc diff [--only RUNTIME] [--allow-branch BRANCH]...
```

Prints what a sync would change, runs the same checks as `sync`, writes nothing and exits 0. Each runtime is listed under `would:` with the files it would write, or `current` when nothing would change.

## prune

```bash
stratarc prune [--only RUNTIME] [--allow-branch BRANCH]... [--dry-run]
```

Deletes the files a runtime directory holds that the source no longer produces. It touches only paths stratarc owns. `--dry-run` lists what would be deleted. The home must be writable unless `--dry-run` is given.

## reconcile

```bash
stratarc reconcile [--check]
```

Brings `control-plane.md` in line with the source tree: a row for each new source file and project, no row for a removed one, and every existing opt-in cell kept. It also refreshes the control-plane snapshot in each active project checkout. `--check` writes nothing and exits 6 when anything is stale. The source root comes from `--root`.

## validate

```bash
stratarc validate [--strict] [--checks generic|private|all]
```

Checks a source root against the bundled schemas and the engine's rules, prints each finding, and ends with a count line. `--strict` exits 1 when any finding is an error. `--checks generic` runs the built-in checks only, `private` runs only the checks the source root supplies in `scripts/private/validate_checks.py`, and `all`, the default, runs both. The command forwards every argument to the module's own parser, so `stratarc validate --help` shows its full usage.

## projects

```bash
stratarc projects [--check] [--only NAME] [--adopt NAME] [--verify]
```

Delivers `projects-root/<project>/` into each managed project checkout. `--check` writes nothing and exits 6 on drift. `--verify` reports disagreement between the manifest and the filesystem and exits 6 if any. `--adopt` records an existing checkout as managed.

## gen-rules-digest

```bash
stratarc gen-rules-digest (--print | --embed FILE... | --check FILE...)
```

Renders the rules digest from the source root's `rules/` tree and tiers. `--print` writes it to standard output, `--embed` replaces the marked block in each file, and `--check` exits 6 when a file's block is stale.

## components

```bash
stratarc components [--manifest FILE] [--list] [--services | --install-service NAME]
```

Validates `components.json`. `--list` prints what is declared, `--services` checks each declared service and its port, and `--install-service` installs a declared launch agent. It exits 1 on an invalid manifest.
