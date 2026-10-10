# resources

This page is the reference for the verbs that change the resources of a source root: `source`, `project`, `runtime`, `agent` and `account`. The argument lists of every command are in [cli](cli.md); this page states what the write verbs do to files. The grammar they share is recorded in [the resource verb command grammar](../../developer-docs/explanation/decisions/2026-10-09-resource-verb-command-grammar.md), and the verbs added to fill its gaps in [the gaps decision](../../developer-docs/explanation/decisions/2026-10-09-resource-verbs-fill-the-grammar-gaps.md).

## rules-every-write-follows

- The new content is validated before anything is saved. JSON and TOML must parse, list modes must be valid, `permissions.json` must satisfy the bundled schema and `stratarc.toml` must load. An invalid result exits 2 with `invalid-edit` and the file is left as it was.
- The file is replaced through the home's safe write: the old version is copied into the home's `backups/` first, and a file that declares a newer schema is never rewritten (exit 5).
- A file written into the source root keeps the mode of the file it replaces, and a new file is 0644 and a new directory 0755, subject to the umask, as `source init` writes them. Files in the home stay 0600 and its directories 0700, and so do the backups ([record](../../developer-docs/explanation/decisions/2026-10-10-source-root-files-keep-their-mode-and-the-home-stays-private.md)).
- Every write verb accepts `--dry-run`, which validates, reports the change and writes nothing. A verb that deletes needs `--yes`, except under `--dry-run`.
- Every verb accepts `--root PATH` and `--json`. With `--json` the output is one envelope `{ok, data, error}`.

## account-edit

```text
stratarc account edit NAME
stratarc account edit NAME --set KEY=VALUE [--set KEY=VALUE ...] [--unset KEY ...]
```

Without a value flag the verb opens `$VISUAL` or `$EDITOR` on a temporary copy of the account file and saves it only when the result validates. With `--set` or `--unset` no editor is opened. The values are changed by a line editor that keeps comments, spacing and trailing comments, and the verb then parses the result and refuses it unless it equals the old document with only the requested changes.

- `KEY` is dotted. Everything before the last dot names a `[table]`, so `permissions.timeout=95` sets `timeout` in `[permissions]`. A key with no dot is a top-level key and is written before the first table.
- `VALUE` is read as JSON first (an integer, `true`, `false` or a list of strings), and as a plain string when it is not JSON. Floats, `null` and objects are refused.
- An existing key keeps its position. A new key is added after the table's last key. A new table is appended at the end of the file.
- `--unset KEY` deletes the key's line. A key that is not set exits 2 with `unknown-key`. The table header stays even when it ends up empty.
- Setting and unsetting the same key in one command exits 2 with `invalid-value`.
- A `.json` account file is changed with the same flags and keys ([record](../../developer-docs/explanation/decisions/2026-10-10-json-accounts-support-set-and-unset.md)). It is parsed, changed and written back with two-space indentation and a final newline. Existing keys keep their order and a new key goes last. A file that does not parse, or whose top level is not an object, exits 2 with `invalid-edit`.
- A value that makes the file invalid, such as `--set permissions._modes.allow=sideways`, exits 2 with `invalid-edit` and nothing is written.

## agent-add

```text
stratarc agent add NAME [--parent P] [--tools TOOL ...] [--description TEXT]
```

Creates `agents/NAME.md` in the source root with the frontmatter the validator requires:

```text
---
name: NAME
description: "..."
tools: Read, Grep, Glob
model: inherit
---
```

- `NAME` follows the project name rule: lowercase letters, digits and hyphens, starting with a letter. The frontmatter `name` always equals the file stem.
- `--tools` takes space or comma separated names. Each must be a tool name the validator knows or an `mcp__server__tool` name; anything else exits 2 with `invalid-value`. The default is `Read, Grep, Glob`.
- `--description` is one line. It is always written as a quoted string, so a colon in it is safe. The default is `The NAME agent.`
- `--parent P` names an existing agent with a definition file. Its tools, description and model are the defaults for any of these the command line leaves out. Nothing else is inherited and the new file does not refer to the parent.
- A name that already has an `agents/NAME.md`, `.toml` or `.json` exits 4 with `agent-exists`. The file is created through the safe write, so the generic checks of `stratarc validate` pass on the result.

## agent-remove

```text
stratarc agent remove NAME [--project P] --yes
```

Deletes every file of the agent (`.md`, `.toml`, `.json`) from `agents/` in the source root, or from `projects-root/P/agents/` with `--project`. Each file is copied into the home's `backups/` first. Without `--yes` the verb exits 2 with `needs-yes`; under `--dry-run` it lists the files and deletes nothing. An agent with no file in that place exits 2 with `unknown-agent`.

## project-enable-and-disable

```text
stratarc project enable NAME
stratarc project disable NAME
```

Both change two things in `control-plane.md`, in one write with one backup:

- the project's `project:*` rows in its own column are set (`x`) or cleared;
- the project's `status` cell in the manifest table is set to `active` on enable and `inactive` on disable.

The result carries `control_plane.changed` (the rows), `control_plane.status` (`{from, to, changed}`, or null) and `reconcile_needed`. `reconcile_needed` is true when the manifest has no status cell for the project (no manifest row, or no `status` column); the cells are still changed and `stratarc reconcile` adds the status. A project with no column at all exits 5 with `not-reconciled`. When nothing needs to change the verb reports that and writes nothing.

## source-init

```text
stratarc source init PATH [--name N] [--use]
```

Scaffolds the bundled template into a new or empty directory, registers it in `sources.toml` and makes it active when none is. The directory and the files it creates get the permissions the process umask gives any new file (mode 0644 and 0755 under the usual 022). They are not narrowed to 0600: that mode is for files in the home, not for a source root that is meant to be committed.

## error-codes

The codes are stable strings. The command line maps them to catalog ids where the catalog has one.

| code | exit | cause |
| --- | --- | --- |
| `agent-exists` | 4 | `agent add` found a file for that name |
| `unknown-key` | 2 | `account edit --unset` named a key that is not set |
| `invalid-edit` | 2 | the result would be invalid, or the layout cannot be followed |
| `invalid-value` | 2 | a malformed `KEY=VALUE`, tool or description |
| `needs-yes` | 2 | a delete without `--yes` |
| `not-reconciled` | 5 | the control plane has no column for the project |
