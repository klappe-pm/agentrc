# cli

This page is the reference for the `stratarc` command: every subcommand, its arguments, its exit statuses and what it prints. It describes what the command does, not when to use it; the [tutorial](../tutorials/getting-started.md) and the [how-to guides](../how-to-guides/README.md) cover that. `tests/test_cli.py` fails when a command or option in the parser is missing from this page. The design, including the commands that do not exist yet, is in [cli-design](../../developer-docs/explanation/cli-design.md).

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

Without a flag, each value comes from its variable, then from `stratarc.toml`, then from the default. The source root also falls back to the active entry of `.stratarc/sources.toml`, which `stratarc source use` writes, before the current directory.

## exit-statuses

| status | meaning |
| --- | --- |
| 0 | ok |
| 1 | failure: the command ran and did not succeed |
| 2 | invalid input: a bad argument, an unknown source root, an invalid `stratarc.toml`, or a refusal by the engine |
| 3 | denied: the home is missing or not writable |
| 4 | conflict: the target already holds something |
| 5 | unavailable: a packaged resource is missing, a file in the home is newer than this stratarc, a provider did not answer, an adapter does not support the installed runtime, or the change log database is locked |
| 6 | drift: the deployed files differ from what the source renders |
| 130 | interrupted |

A command that reports drift (`check`, `reconcile --check`, `projects --check`, `projects --verify`, `gen-rules-digest --check`) exits 6 where its module returns 1. The engine returns 2 for every refusal, so the branch and stale-deploy guards exit 2 rather than 3.

## the-json-envelope

With `--json` a command prints exactly one object on standard output and exits with the same status as without it.

```json
{"ok": false, "data": null, "error": {"code": "msg-1001", "message": "...", "param": "root", "hint": "..."}}
```

`ok` is true only for status 0. `data` is the command's result, or null when there is none. For an engine command it holds `command`, `exit`, and the `stdout` and `stderr` the module wrote. For `config`, `log`, `verify`, `provider`, `adapter`, `source`, `project`, `runtime`, `agent` and `account` it is the resource's own result (the module prints the envelope and the command line only resolves the error). `error.code` is a message id from the catalog below, or one of `failure`, `invalid-input`, `denied`, `conflict`, `unavailable`, `drift` and `interrupted` when a module failed without one. A resource error whose module code has a catalog entry (`unknown-key` is `msg-1105`, `provider-unreachable` is `msg-1113`, and so on) shows the catalog id, the catalog recovery, and the module's own sentence inside the problem. A usage error from one of these commands (a missing verb, an unknown option) prints an envelope with the code `invalid-input` and the usage message, so `--json` always prints exactly one object.

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
| `msg-1101` | 2 | A list value has no mode. | Set the mode for that key to `replace` or `extend` in the file that holds the list. |
| `msg-1102` | 2 | A mode is not valid. | Use `replace` or `extend`, and set a mode only on a list. |
| `msg-1103` | 2 | A key has a different type in two layers. | Give the key one type in every layer. |
| `msg-1104` | 2 | A layer file cannot be parsed. | Fix the syntax in the file named in the message. |
| `msg-1105` | 2 | No layer sets the key. | Run `stratarc config list` to see the keys that are set. |
| `msg-1106` | 2 | The project is not known. | Check the spelling, or list the projects with `stratarc projects --check`. |
| `msg-1107` | 2 | The agent is not known. | Check the spelling, or pass `--project` when the agent belongs to a project. |
| `msg-1108` | 2 | The account is not known. | Create `accounts/<name>.toml` in the source root, or check the spelling. |
| `msg-1109` | 2 | The runtime is not known to the layers. | Use a runtime named under `[runtimes]` in `stratarc.toml`. |
| `msg-1110` | 2 | The source root cannot be read. | Pass an existing directory with `--root`, or create one with `stratarc init`. |
| `msg-1111` | 2 | The command needs a project. | Pass `--project NAME`. |
| `msg-1112` | 5 | A file was written by a newer stratarc and was left unchanged. | Upgrade stratarc, then run the command again. |
| `msg-1113` | 5 | A provider did not answer its test. | Check the endpoint and the network, or register the provider with `--no-test`. |
| `msg-1114` | 5 | An adapter does not support this install. | Update the adapter with `stratarc adapter register`, or pin the runtime to a supported version. |
| `msg-1115` | 5 | An adapter is older than the installed runtime. | Update the adapter with `stratarc adapter register`; the deploy continues. |
| `msg-1116` | 5 | The change log database is locked or unusable. | Close the other process that uses the database and run the command again; the event is kept in the human readable log. |
| `msg-1117` | 6 | The deployed files differ from what the source renders. | Run `stratarc sync` to redeploy, or inspect the report with `stratarc verify show`. |
| `msg-1118` | 3 | The home directory cannot be created or written to. | Make it writable, or point at another one with `--home` or `STRATARC_HOME`. |
| `msg-1119` | 5 | The backup of a file is missing from the home backups. | Restore the file by hand, or run `stratarc sync` again to redeploy it. |
| `msg-1120` | 2 | A project was not delivered because its `.claude/settings.json` is not valid JSON. | Fix or remove the file and run the command again; nothing was delivered to the project. |
| `msg-1121` | 2 | A version range is not valid. | Use comma separated comparators such as `>=0.4,<0.9`, or `*`. |
| `msg-1122` | 2 | An adapter manifest is not valid. | Fix the fields the message lists; the adapter manifest schema describes each one. |
| `msg-1123` | 2 | An adapter manifest cannot be read. | Fix or replace the file, then register the adapter again. |
| `msg-1124` | 2 | No adapter manifest was found. | Pass a `manifest.json` file, a directory that holds one, or an installed package that ships one. |
| `msg-1125` | 2 | The adapter is not registered. | List the registered adapters with `stratarc adapter list`. |
| `msg-1126` | 4 | The adapter is already registered. | Pass `--replace` to register it again. |
| `msg-1127` | 2 | A deprecation has no reason. | Pass `--reason` with the explanation operators should read. |
| `msg-1128` | 2 | The end date of a deprecation is not a date. | Pass `--end-date` as `YYYY-MM-DD`. |
| `msg-1129` | 2 | The adapter is not deprecated. | Check the name, or mark the adapter first with `stratarc adapter deprecate`. |
| `msg-1130` | 2 | A runtime version is not in the form `RUNTIME=VERSION`. | Pass it as, for example, `--runtime-version codex=0.9.1`. |
| `msg-1131` | 2 | A provider is not valid. | Fix the fields the message lists, and reference secrets as `secret://NAMESPACE/KEY`. |
| `msg-1132` | 2 | The provider name is not valid. | Use lowercase letters, digits and hyphens, starting with a letter. |
| `msg-1133` | 2 | The provider is not registered. | List the registered providers with `stratarc provider list`. |
| `msg-1134` | 2 | A provider file cannot be read. | Fix or remove the file under the home's providers directory. |
| `msg-1135` | 4 | The provider is already registered. | Change it with `stratarc provider edit`, or remove it first. |
| `msg-1136` | 2 | The provider does not serve the model. | List the models it serves with `stratarc provider show`. |
| `msg-1137` | 2 | The command was given input it cannot use. | Check the arguments against `stratarc COMMAND --help`, then run it again. |
| `msg-1138` | 5 | The data the command needs is not available. | Run the command that produces it first, or check that the path exists and can be read. |
| `msg-1139` | 2 | The edit would leave the file invalid. | Fix the listed problems and run the command again, the file was left unchanged. |
| `msg-1140` | 2 | No editor is configured to edit the file. | Set `$EDITOR` or `$VISUAL`, then run the command again. |
| `msg-1141` | 2 | The command deletes files and needs confirmation. | Pass `--yes` to confirm, or `--dry-run` to preview. |
| `msg-1142` | 2 | The source root is not registered or no longer exists. | Run `stratarc source list` for the registered ones, or register one with `stratarc source init`. |
| `msg-1143` | 2 | The name cannot be used. | Use lowercase letters, digits and hyphens, starting with a letter. |
| `msg-1144` | 2 | The value cannot be used. | Check the value against `stratarc COMMAND --help`, then run the command again. |
| `msg-1145` | 2 | The path cannot be used. | Choose a path the command allows, which `stratarc COMMAND --help` describes. |
| `msg-1146` | 2 | The configuration cannot be used. | Fix `stratarc.toml` in the source root, then run the command again. |
| `msg-1147` | 4 | The project already exists. | Change it with `stratarc project edit`, or pick another name. |
| `msg-1148` | 4 | The account already exists. | Change it with `stratarc account edit`, or pick another name. |
| `msg-1149` | 4 | The source name is already registered. | Choose another `--name`, or use `stratarc source use`. |
| `msg-1150` | 4 | The path already exists and is not empty. | Choose a new or empty directory. |
| `msg-1151` | 5 | The control plane has no entry for the project. | Run `stratarc reconcile` to add it, then run the command again. |
| `msg-1152` | 1 | The editor did not finish. | Check `$EDITOR`, then run the command again, nothing was saved. |
| `msg-1153` | 5 | The terminal interface needs Textual, which is not installed. | Install it with `pip install 'stratarc[ui]'`, then run `stratarc ui` again. |
| `msg-1154` | 4 | The change conflicts with what already exists. | Resolve the conflict, or choose another name or path, then run the command again. |
| `msg-1155` | 4 | The agent already exists. | Change it with `stratarc agent edit`, or pick another name. |
| `msg-1156` | 2 | The agents relay to each other in a loop. | Remove one `parent` so the chain ends at an agent with no parent. |
| `msg-1157` | 2 | The relay chain of the agent is too long. | Shorten the chain, or give the child the settings directly. |
| `msg-1158` | 2 | An agent's `parent` or `relay` is not valid. | Name an existing agent in `parent` and give `relay` an `inherit` list of key patterns, or remove the key. |
| `msg-1159` | 2 | A `--set`, `--set-json` or `--set-mode` flag is not valid. | Write `KEY=VALUE`, quote JSON for your shell, and pass `--set-mode` only with the `--set` or `--set-json` that sets the key. |
| `msg-1160` | 5 | The terminal interface needs a terminal, and its input or output is redirected. | Run `stratarc ui` in an interactive terminal, or use the commands that print text, such as `stratarc config list`. |

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
stratarc doctor [--permissions] [--clean [--yes]] [--report]
```

Reports the health of the install without changing anything: the Python and stratarc versions, the home and whether it is writable, which source root was resolved and from what, the registered adapter count, each runtime with whether it is enabled and whether its target directory exists, and the measured start time in milliseconds (from loading the command line module to the start of the checks). With `--json` the same facts are the envelope's `data`. It exits with the status of the first problem it found, or 0.

`--permissions` adds a table of what each command reads, what it writes, whether it uses the network and what program it may run (`data.permissions`). `--clean` lists the backups beyond the newest 20 per file that are also 30 days old or more, and the disposable cache, and removes them only when `--yes` is also given (`data.clean`); settings, sources, providers and adapters are never listed. `--report` writes a bundle of versions, the doctor data, the problems and the `STRATARC_` variables to `.stratarc/state/debug/doctor-<time>.json` after redacting token-shaped values and replacing the home with `~`, and prints its path (`data.report`). `--clean` and `--report` write to the home and exit 3 (`msg-1118`) when it cannot be written.

## sync

```bash
stratarc sync [apply] [--only RUNTIME] [--allow-branch BRANCH]... [--dry-run] [--list] [--verify] [--rollback-on-drift]
```

Renders the source root into every enabled runtime that has a target directory, then into the managed project checkouts. It writes only the files it owns. `apply` is the same as no verb. `--only` limits the run to one runtime and skips project delivery. `--allow-branch` names an extra branch the source may deploy from when `components.json` declares environments, and can be repeated. `--dry-run` prints what would change and writes nothing, the same as `diff`. `--list` prints each runtime and whether its target exists.

`--verify` runs `verify run` for this run's change straight after the write and exits 6 (`msg-1117`) when a deployed file differs. The change is the rules digest the run wrote, which reaches every runtime and every project the control plane lists; with the change log off, `--verify` verifies with scope `all`, which walks every runtime and every active managed project. `--rollback-on-drift` implies `--verify`; before each write it copies the runtime files the adapter will change, each runtime's deploy stamp and the deploy record into the home backups, and on drift it puts them back and removes the files the run created. It restores runtime targets only, not project checkouts, and exits 5 (`msg-1119`) when a backup is missing. Both flags check the whole deployment, so they apply to a full write run: with `--dry-run`, `--list` or `--only` they exit 2.

The first write run creates the `.stratarc` layout under the home and stores the bundled adapter manifests. Before a write each adapter is checked against the installed runtime: an unsupported adapter blocks the runtime and the run exits 5 (`msg-1114`), an outdated one prints a warning once and continues (`msg-1115`). While the change log is enabled the run records the changes it makes (the derived files, the plugin declarations, each runtime, the project deliveries, the permission sweep and the deploy record); with the log off it records nothing, and a locked database never stops the sync.

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

## config

```bash
stratarc config get KEY [--project P] [--agent A] [--account X] [--runtime R] [FLAGS]
stratarc config list [--project P] [--agent A] [--account X] [--runtime R] [FLAGS]
stratarc config explain (KEY | --tree --project P) [--agent A] [--account X] [--runtime R] [FLAGS]
```

Reads the layered settings (base, runtime, account, project, agent, environment, flags) and never writes. `get` prints the resolved value of one key, or the nested table under a prefix. `list` prints every resolved key. `explain` prints the chain for a key with the file and line of every layer that set it, which layer decided the result and why, or with `--tree` the outline of everything a project changes. The command forwards every argument to the module's parser, so `stratarc config --help` shows the full usage. An unresolvable key or an unreadable layer file exits 2 with one of `msg-1101` to `msg-1111`.

`FLAGS` is the top layer, for this one command and written nowhere: `--set KEY=VALUE` sets a key to a literal string, `--set-json KEY=JSON` sets it to a JSON value, and `--set-mode KEY=MODE` (`replace` or `extend`) says how a `--set-json` list that redefines a list combines with the lists below it. Each is repeatable. A malformed pair, invalid JSON, or a `--set-mode` with no `--set` or `--set-json` for the key exits 2 (`msg-1159`).

When `--agent` names an agent with a `parent`, `explain` also prints the dispatch relay: each child to parent edge with its `inherit` patterns, the parent that supplied each inherited key, the values the child did not take, and where the account came from. A loop in the chain exits 2 (`msg-1156`), a chain longer than 8 agents exits 2 (`msg-1157`), and a malformed `parent` or `relay` exits 2 (`msg-1158`). The full output is in [config](config.md).

## log

```bash
stratarc log show [--limit N] [--status S] [--project P] [--file F] [--layer L] [--key K] [--actor-kind K] [--actor A] [--command C] [--cause ID] [--since DATE] [--until DATE]
stratarc log tail [-n LINES]
stratarc log explain ID
stratarc log export [--format json|jsonl|csv] [--output PATH] [the filters of show]
stratarc log enable
stratarc log disable
stratarc log prune --before YYYY-MM-DD [--dry-run]
```

The change history under `.stratarc/logs`. The human readable log is always written by commands that record; the database and the JSONL mirror are written only while the log is enabled (`log enable`, `logging = true` in the home's `config.toml`, or `STRATARC_LOG=1`, which wins over the file). `show` lists changes newest first, `tail` prints the last lines of the human readable log, `explain` prints one change as a short story, `export` prints or writes changes, and `prune` removes stored changes dated before a day and never trims the human readable log. `enable`, `disable` and `prune` write to the home and exit 3 (`msg-1003`) when it cannot be written. A locked database never loses an event: it is kept in the files and one message says so (`msg-1116`).

## verify

```bash
stratarc verify run [--scope all|project:NAME|change:ID] [--verbose]
stratarc verify last [--verbose]
stratarc verify show [ID] [--verbose]
```

The recursive write-back test. Scope `all` covers every runtime and every active managed project, `project:NAME` one project and no runtime, `change:ID` the runtimes and the projects that change reached. `run` re-resolves the source, re-renders each affected runtime into a temporary stage, compares it with the deployed files, repeats the comparison in each project the scope reaches, records `verified` or `drift` per file in the log and writes a report under `.stratarc/state`. It never writes to a deployed file. It exits 0 when everything matches and 6 (`msg-1117`) when anything differs. `last` and `show` print a saved report and exit 5 when none exists.

## api

```bash
stratarc api serve [--socket PATH | --port N] [--root R]
stratarc api schema
```

The read-only local API. `serve` listens on a Unix socket in the home, or on a loopback port, and prints `listening on <address>`; `schema` prints the published JSON schema. The global `--root` sets the source root for `serve`. Standard output passes through unchanged (the schema is already JSON, so `--json` adds no envelope to it), and a failure shows its catalog message, or one error envelope with `--json`. A bad port or a missing source root exits 2, a socket that cannot be bound exits 5, and an interrupt exits 130. `serve` on a socket needs a writable home and exits 3 (`msg-1003`) otherwise. The routes, the transport and the limits are in [api](api.md).

## provider

```bash
stratarc provider list
stratarc provider show NAME
stratarc provider add NAME --endpoint URL [--model ID]... [--secret secret://NAMESPACE/KEY] [--description TEXT] [--no-test] [--replace]
stratarc provider edit NAME [--endpoint URL] [--secret REF | --clear-secret] [--description TEXT] [--add-model ID]... [--remove-model ID]...
stratarc provider remove NAME
stratarc provider test NAME
```

Inference providers registered under `.stratarc/providers`. A provider file holds the endpoint, the models it serves and a `secret://` reference, never the secret. `add` probes the endpoint first unless `--no-test` is given. `test` is the only command that uses the network. `add`, `edit` and `remove` keep a backup of the file they replace. A provider that does not answer exits 5 (`msg-1113`); a name already registered exits 4.

## adapter

```bash
stratarc adapter list
stratarc adapter show NAME
stratarc adapter register PATH-OR-PACKAGE [--replace]
stratarc adapter remove NAME
stratarc adapter status [--runtime-version RUNTIME=VERSION]...
stratarc adapter deprecate NAME --reason TEXT --end-date YYYY-MM-DD [--replacement NAME]
```

The translators from the source to a runtime. Each adapter has a manifest naming its runtime, the runtime versions it supports, the source schema versions it reads, the source kinds it renders and the files it writes; the bundled adapters are registered by the first write run. `register` validates a manifest from a file, a directory or an installed package and stores it; a registered manifest overrides the bundled one of the same name. `status` compares each adapter with the installed runtime and prints `ok`, `outdated`, `unsupported` or `unknown`; `--runtime-version` overrides a detected version. `sync` refuses to deploy through an unsupported adapter (`msg-1114`) and warns once for an outdated one (`msg-1115`). `deprecate` marks an adapter as unsupported from a date; the next interactive command asks once, a noninteractive run prints the notice and continues.

## source

```bash
stratarc source init PATH [--name N] [--use] [--dry-run]
stratarc source show
stratarc source list
stratarc source use NAME-or-PATH [--dry-run]
stratarc source move NEW_PATH [--name N] [--dry-run] --yes
```

Where the source root lives and which one is active. `init` scaffolds a source root from the bundled template into `PATH` (absent or empty) and registers it in `.stratarc/sources.toml` under `--name` (taken from the folder name when absent); `--use` makes it the active one. `show` prints the effective source root, its registered name, the active name, whether `stratarc.toml` is present and the number of projects. `list` prints the registered roots and marks the active one. `use` makes a registered name, or an existing directory (which it registers), the active source root. `move` moves a registered root to `NEW_PATH` and updates `sources.toml`; it needs `--yes`. The active source root is read by every command: the order is `--root`, `STRATARC_SOURCE`, the nearest `stratarc.toml` above the current directory, the active entry of `sources.toml`, then the current directory. A `sources.toml` that is unreadable or written by a newer stratarc is ignored when resolving the root, and `source use` and `source list` report it. `init`, `use` and `move` write to the home and exit 3 (`msg-1003`) when it cannot be written, except with `--dry-run`. A name already registered exits 4 (`msg-1149`), a path that is not empty exits 4 (`msg-1150`), and an unknown source exits 2 (`msg-1142`).

## project

```bash
stratarc project list
stratarc project show NAME
stratarc project add NAME [--dry-run]
stratarc project edit NAME [--file REL] [--dry-run]
stratarc project remove NAME [--dry-run] --yes
stratarc project enable NAME [--dry-run]
stratarc project disable NAME [--dry-run]
```

The projects under `projects-root/` and their overrides. `add` creates the project folder. `edit` opens `$VISUAL` or `$EDITOR` on a temporary copy of the project's owning file (or of `--file`, a path inside the project folder) and saves it only when the result validates. `remove` deletes the folder after copying each file into the home backups, and needs `--yes`. `enable` and `disable` change the project's opt-in cells in `control-plane.md` and do not touch the manifest status column. A project that has no column yet exits 5 (`msg-1151`): run `stratarc reconcile` first. An unknown project exits 2 (`msg-1106`), an existing one exits 4 (`msg-1147`), a missing `--yes` exits 2 (`msg-1141`) and a missing editor exits 2 (`msg-1140`).

## runtime

```bash
stratarc runtime list
stratarc runtime show NAME
stratarc runtime enable NAME [--dry-run]
stratarc runtime disable NAME [--dry-run]
stratarc runtime target NAME PATH [--dry-run]
```

The supported agent runtimes and where each deploys. `enable`, `disable` and `target` change the `[runtimes.NAME]` table of `stratarc.toml` with a line editor that keeps comments and layout, and refuse the change when anything but the intended value would differ (`msg-1139`). An unknown runtime exits 2 (`msg-1109`) and an empty target exits 2 (`msg-1144`).

## agent

```bash
stratarc agent list [--project P]
stratarc agent show NAME [--project P]
stratarc agent add NAME [--parent P] [--tools TOOL...] [--description TEXT] [--dry-run]
stratarc agent remove NAME [--project P] [--dry-run] --yes
stratarc agent edit NAME [--project P] [--definition] [--dry-run]
stratarc agent explain NAME [--project P] [--account X] [--runtime R] [--key KEY]
```

Per-agent settings inside a project or the source root. `add` creates the agent's definition in the source root; `--parent` names an existing agent whose tools and description are the defaults, `--tools` and `--description` set them, and an existing name exits 4 (`msg-1155`). `remove` backs up the agent's files and deletes them, and needs `--yes`. `edit` opens the agent's settings file, or with `--definition` its `.md` definition, in `$VISUAL` or `$EDITOR` and saves it only when the result validates. `explain` prints where each of the agent's values comes from, the same chain `config explain` prints. An unknown agent exits 2 (`msg-1107`).

## account

```bash
stratarc account list
stratarc account show NAME
stratarc account add NAME [--set KEY=VALUE]... [--dry-run]
stratarc account edit NAME [--set KEY=VALUE]... [--unset KEY]... [--dry-run]
stratarc account remove NAME [--dry-run] --yes
```

Named accounts and the settings tied to them, kept as files under `accounts/`. `add` creates the file from the `--set` pairs (strings, integers, booleans or lists of strings). `edit` with no flags opens the file in `$VISUAL` or `$EDITOR`; with `--set` or `--unset` it changes those keys without an editor and keeps the comments of a TOML file. A key both set and unset exits 2 (`msg-1144`), an unset key that is not in the file exits 2 (`msg-1105`), and a JSON account file does not take the flags and exits 2 (`msg-1139`). `remove` backs the file up and needs `--yes`. An unknown account exits 2 (`msg-1108`), an existing one exits 4 (`msg-1148`) and a malformed pair exits 2 (`msg-1144`).

The five resources above take the global `--root` and `--json`, accept the options shown after their verbs, and share the rules of every write: the new content is validated before it is saved, the old version is copied into the home backups first, and a file that declares a newer schema is never rewritten. Every write verb takes `--dry-run`, which reports the change and writes nothing. An editor that exits non-zero exits 1 (`msg-1152`).

## ui

```bash
stratarc ui
```

Opens the full-screen terminal interface on the source root: a tree of the projects, runtimes, agents and accounts, a detail pane, and keys to edit (`e`), explain (`x`), read the log (`l`), preview a sync (`s`) and verify (`v`). It writes only through the same paths as the commands above and uses no network. The global `--root` sets the source root. The interface needs Textual, installed with `pip install 'stratarc[ui]'`; without it the command prints `msg-1153` and exits 5. A source root that does not exist exits 2 (`msg-1001`), and input or output that is not a terminal exits 5 (`msg-1160`); both are checked before the screen opens. The checks run in that order, so a missing root is reported before a missing extra. The statuses are in [ui](ui.md).
