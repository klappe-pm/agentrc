# cli-design

This page is the design for the command line and terminal interface of agentrc. It covers the command tree, the `~/.agentrc` home, how settings are inherited and explained, provider and adapter registration, the optional change log, verification, error messages, the local read API, and the first-run and documentation experience. It is a design, not shipped behavior; the [architecture](architecture.md) page describes what exists today.

## principles

- One tool for the whole tree. Everything the engine knows (base, project, agent, provider, adapter) is reachable by the same `resource verb` grammar, in scripts and in the terminal interface.
- Nothing hidden. Every value shown can be traced to the file and line that set it, and every write names the file it changed.
- Local first. All state lives in the source root and in `~/.agentrc`. Nothing is sent anywhere.
- Safe by default. Writes preview first, back up before replacing, and refuse rather than guess.
- Same answers everywhere. The command line, the terminal interface, the local read API and CI all call one library layer, so they cannot disagree.

## command-grammar

Commands are `agentrc <resource> <verb> [args]`. Help exists at the root, the resource and the verb. Output is human readable on a terminal and a stable JSON envelope with `--json`. In noninteractive mode a command never prompts; it fails with the message that says what flag to pass.

| resource | verbs | what it manages |
|---|---|---|
| `source` | `init`, `show`, `use`, `list`, `move` | the source root: where it lives, which one is active |
| `config` | `get`, `set`, `unset`, `edit`, `list`, `explain` | layered settings and the files that hold them |
| `project` | `add`, `list`, `show`, `edit`, `remove`, `enable`, `disable` | managed projects and their overrides |
| `agent` | `list`, `show`, `edit`, `explain` | per-agent settings inside a project or account |
| `account` | `list`, `show`, `add`, `edit`, `remove` | named accounts and the settings tied to them |
| `runtime` | `list`, `show`, `enable`, `disable`, `target` | the supported agent runtimes and where each deploys |
| `provider` | `list`, `show`, `add`, `edit`, `remove`, `test` | inference providers and their models |
| `adapter` | `list`, `show`, `add`, `register`, `status`, `deprecate`, `remove` | translators from the source to a runtime |
| `rule`, `hook`, `skill`, `command`, `permission` | `list`, `show`, `add`, `edit`, `remove` | the source kinds an adapter renders |
| `sync` | `plan`, `apply`, `check`, `diff`, `prune` | rendering and deploying |
| `verify` | `run`, `last`, `show` | the recursive write-back test |
| `log` | `show`, `tail`, `export`, `enable`, `disable`, `prune`, `explain` | the change history |
| `doctor` | none | health of the install, the source and every target |
| `ui` | none | the terminal interface |
| `api` | `serve`, `schema` | the local read API |

`sync check` and the top-level `check` keep their current meaning; the top-level `sync`, `check`, `diff`, `prune` and `reconcile` stay as aliases so existing scripts keep working. `edit` opens the owning file in `$EDITOR`, validates on save and refuses to leave an invalid file in place.

## the-home-directory

The tool keeps its own state in `~/.agentrc`, or `$XDG_CONFIG_HOME/agentrc` where that variable is set, or the path in `AGENTRC_HOME`. The home holds the machine, not the project. The source root stays a normal directory you can commit.

```text
~/.agentrc/
  config.toml          machine settings: active source root, log switch, ui preferences
  sources.toml         known source roots and which one is active
  providers/           one file per registered provider
  adapters/            one directory per registered adapter, with its manifest
  state/               deploy stamps, last verify report, adapter status
  backups/             timestamped copies of every file replaced by a write
  logs/
    changes.db         optional SQLite change log
    agentrc.log        human readable log
    agentrc.jsonl      machine log, same events as the database
  cache/               disposable
```

Cleaning and maintenance rules:

- `backups/` keeps the last 20 versions of each file and anything newer than 30 days, whichever is more. `agentrc doctor --clean` shows what would be removed and removes it only with `--yes`.
- `cache/` can be deleted at any time. `state/` can be rebuilt by `sync check`. `config.toml`, `sources.toml`, `providers/` and `adapters/` are user data and are never deleted by a clean.
- Every file in the home carries a schema version. A newer file than the tool understands is never rewritten; the tool says so and exits with the unavailable code.
- The home is created mode 0700. Secrets are never stored in it; a provider file holds a `secret://` reference, never the value.

## layers-and-inheritance

Settings resolve through layers, lowest to highest precedence:

1. base: the source root's shared files
2. runtime: values specific to one runtime
3. account: values tied to a named account
4. project: the project's own overrides under `projects-root/<project>/`
5. agent: values for one agent or sub-agent inside that project
6. environment and flags: `AGENTRC_*` variables, then command-line flags

A higher layer replaces a scalar, merges a table key by key and, for lists, either replaces or extends according to an explicit `mode` in the file; there is no implicit append.

`agentrc config explain <key> [--project P] [--agent A] [--account X]` prints the resolution chain for one key:

```text
permissions.network.allow   (project: notes-cli, agent: reviewer, runtime: codex)
  base       permissions.json:41              ["api.example.com"]
  runtime    runtimes/codex.toml:7            + ["registry.example.org"]
  project    projects-root/notes-cli/permissions.json:12   mode=replace  ["api.example.com"]
  agent      agents/reviewer.json:5           (not set)
result: ["api.example.com"]   decided by: project (replace)
```

`explain` also covers dispatch. For a sub-agent started by another agent, the relay is shown as part of the chain: which parent's settings it inherited, which it did not, and where the account came from. `agentrc config explain --tree` prints the whole inheritance tree for a project as a pruned outline, down to the file and line that wrote every value.

## providers-and-adapters

A provider is data: a file under `providers/` (or in the source root's `models/providers/`) naming an endpoint, the models it serves and a secret reference. `provider add` validates the file against the bundled schema, runs `provider test` against a harmless endpoint and writes it. Nothing is rendered until a runtime asks for a model.

An adapter is the translator from the source to one runtime. Registration is explicit and recorded:

- `adapter register <path-or-package>` reads the adapter's manifest (name, runtime, supported runtime versions, schema range, the source kinds it renders) and stores it under `adapters/`. A bundled adapter is registered by the install.
- Each adapter declares a `supports` range for the runtime version it targets. `adapter status` compares that range with the installed runtime and the schema the source uses, and prints one line per adapter: `ok`, `outdated`, `unsupported` or `unknown`.
- `sync` refuses to deploy through an `unsupported` adapter. For `outdated` it warns once per run and continues.
- `adapter deprecate <name>` marks an adapter as no longer supported, with a reason and an end date. The next interactive command prompts once, naming the adapter, what it affects and the replacement, and records the answer. Noninteractive runs print the same message and continue without prompting.
- Maintenance when a runtime changes: a contributor commits an adapter update with its new `supports` range and a fixture captured from the runtime. `agentrc adapter status` and CI then show which adapters fall outside which runtime versions.

Adapters declare what they write, so `prune` and `verify` know which files are theirs.

## the-change-log

Logging is off until enabled with `agentrc log enable`, or `logging = true` in `config.toml`. The human readable log is on by default and is a plain file.

The optional log is a SQLite database at `~/.agentrc/logs/changes.db`. Each change is one row in `changes`, linked to rows in `targets` and `projects`.

| column | meaning |
|---|---|
| `id`, `ts`, `date` | stable id, UTC timestamp, UTC day |
| `actor_kind` | `human`, `agent`, `hook`, `ci` |
| `actor` | the account, agent or hook that caused the change, when it can be determined |
| `command` | the command or API call, with arguments redacted |
| `file`, `layer`, `key` | what changed and in which layer |
| `before_digest`, `after_digest` | content digests, never the content |
| `status` | `written`, `propagated`, `partial`, `failed`, `verified`, `drift` |
| `projects` | projects the change reaches, from the inheritance tree |
| `source_ref` | commit, branch or `uncommitted` for the source root |
| `cause_id` | the change that caused this one, for propagated writes |

Values pass the same token-shape redaction as every other captured content before they are written. The same events are appended to `agentrc.jsonl` in a fixed envelope (stable id, actor, digests, timestamp, status), so another tool can read the file without the database. `log explain <id>` prints a change as a short story: who asked for it, what layer it touched, which projects and runtimes it reached, and whether verification confirmed it.

## verification

`agentrc verify run` is the recursive test that a change landed. For a chosen scope (everything, one project, or one change id) it:

1. re-resolves every layer from the source files,
2. re-renders each affected runtime into a temporary stage,
3. compares the stage with the deployed files byte for byte,
4. walks into each project the change reaches and repeats the comparison there,
5. records `verified` or `drift` per file in the log and writes a report under `state/`.

It exits 0 when everything matches and with the drift code when anything differs, so CI and hooks can gate on it. `verify` never writes to a deployed file. `sync apply --verify` runs it immediately after a write and rolls back from `backups/` if you pass `--rollback-on-drift`.

## errors-and-logs

Every message has an id (`msg-` plus a number) and lives in one catalog file, so wording is edited in one place. A message is one sentence that states the problem and one that states the recovery, plus the file and line where relevant. Tracebacks appear only with `--debug`, which also writes a bundle with versions and redacted state for an issue report.

```text
error msg-1042  The adapter "codex" does not support Codex 0.9.
  It declares support for 0.4 to 0.8. Update the adapter with `agentrc adapter register`, or pin Codex to 0.8.
  See: agentrc adapter status
```

Exit codes: 0 ok, 1 failure, 2 invalid input, 3 denied, 4 conflict, 5 unavailable, 6 drift or verification failed, 130 interrupted. Human output goes to standard error for messages and standard output for data; with `--json` the envelope has `ok`, `data`, `error` (`code`, `message`, `param`, `hint`) and paging fields where a list is truncated.

The catalog starts with the failures the commands can hit: missing or unreadable source root, invalid or newer schema, unknown key, layer conflict, list mode missing, unknown project, unknown account, unsupported or outdated adapter, provider unreachable, missing secret reference, write refused because the target changed since the last sync, drift found by verify, backup missing, log database locked, home not writable, and a runtime directory owned by another tool. Each gets an id, wording, a recovery and a test. The wording is meant to be reviewed and edited as a set.

## permissions

Every command declares the access it needs, and `agentrc doctor --permissions` prints the table. Read commands need read access to the source root, the home and the target directories. Write commands need write access to the same places and nothing else. No command needs network access except `provider test`. No command runs a program except an adapter's declared hooks, which are listed before they run. A command that would write outside the source root, the home or a registered target stops with the denied code.

## local-read-api

`agentrc api serve` starts a read-only interface on a Unix socket (or loopback port with `--port`), with no authentication beyond the socket's file mode. It exposes the same data as `list`, `show`, `explain` and `log` commands, with a versioned schema published by `api schema`. Paging, error envelope and exit semantics match the command line. A graph or dashboard service can read it directly, or ingest `agentrc.jsonl`, to show inheritance trees and change history without reading the source files. Writes through the API are out of scope for the first release.

## terminal-interface

`agentrc ui` opens a full-screen view built for an 80 by 24 terminal with a monochrome fallback. Left, the tree of source, projects and agents; right, the selected node with its resolved values; below, a one-line key help. `e` edits the owning file, `x` explains the selected value, `l` opens its log entries, `s` previews a sync and `v` runs verify. Every action is the same library call its command uses, and the view is tested in a pseudo-terminal.

## first-run-and-docs

First run is a choice, not a scan. `agentrc` with no arguments in a new home shows a one-screen welcome: the version, the detected runtimes, and three numbered steps (`source init`, `runtime enable`, `sync plan`). It never reads or migrates existing configuration without being told. The banner is one line and the start time budget is under 150 ms for a command that does not touch the disk beyond the home; `doctor` reports the measured start time.

The documentation site is built from the files in `docs/`, with the command reference and the error catalog generated from the code so they cannot drift. The landing page leads with a 30 second demo of `config explain`, then the install, then the tutorial for a first source root. Every error message links to its catalog entry by id. A load screen is a static page with the wordmark and the three first-run steps; it contains no animation and no script that blocks the page.

## debugging-and-bugs

`--debug` turns on step logging, writes a redacted bundle to `~/.agentrc/state/debug/` and prints its path. `agentrc doctor --report` produces the same bundle for an issue. An import or adapter failure names the module and the registered path, so a broken third-party adapter is not mistaken for a broken install. A bug found by `verify` or `doctor` is recorded in the log with its command and digests, which is the reproduction a maintainer needs.

## ci

Every check a contributor can run is the check CI runs, with the same command: `agentrc check`, `agentrc verify run --scope all` over the bundled example, the schema and catalog checks, and the test suite. Documentation tables for commands and errors are regenerated in CI and the build fails if they differ from the committed files.

## open-decisions

- The command name. Another published project already installs a binary called `agentrc`, so two installs on one machine would collide; the name needs a decision before the first publish.
- Whether `~/.agentrc` should be the primary home or an alias for the XDG path.
- Which Python library draws the terminal interface; Textual is the working choice.
- Whether list-valued settings default to replace or extend when a file leaves `mode` out. The design refuses; a default would be friendlier.
