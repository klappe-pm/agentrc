# 2026-10-09-first-run-is-a-choice-not-a-scan

## status

accepted

## context

People who install stratarc usually already have agent configuration in their runtime directories, written by hand or by other tools. A tool that scans and imports on first run risks copying settings the user did not mean to adopt, and one that overwrites on the first sync risks destroying them. The design for first run is under first-run-and-docs in [cli-design](../cli-design.md). The welcome screen and migrations are not built yet and sit in the documentation item of the [backlog](../cli-backlog.md).

## decision

Nothing scans or migrates existing configuration without being told. `init` writes a template, `sync` previews first through `diff` and backs up before it replaces anything. Migrations, when they exist, are checksummed with a verified backup and leave a recovery instruction on failure. The welcome screen is one line, and the start time budget is under 150 ms, measured by `doctor`.

## alternatives

- Scan the runtime directories and offer to import what is found. It shortens setup, but it reads files the user never pointed at and can adopt unwanted settings.
- Overwrite on first sync and rely on version control. Many users do not keep their runtime directories under version control.
- A long guided welcome. It explains more, but it slows every start and trains users to skip it.
- No start time budget. It is simpler, but startup cost creeps up unnoticed without a measured limit.

## consequences

- Setup takes explicit steps, and the template from `init` is the starting point rather than an import of what exists.
- The preview and backup rules apply to every write path, which the [home layout](../cli-design.md) records under backups.
- A migration that fails must leave the user able to recover without reading code.
- The 150 ms budget limits what the entry point may import before it prints.
