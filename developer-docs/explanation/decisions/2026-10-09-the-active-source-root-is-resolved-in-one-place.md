# 2026-10-09-the-active-source-root-is-resolved-in-one-place

## status

accepted, 2026-10-09. Builds on [projects root from config](2026-10-09-projects-root-from-config.md) and [home layout and safe writes](2026-10-09-home-layout-and-safe-writes.md).

## context

`stratarc source use` writes the active source root into `sources.toml` in the home. Nothing but the resource module read that file, so `sync`, `check`, `config` and every other command still resolved the root from the flag, the environment variable, the nearest `stratarc.toml` and the current directory, and ignored the choice the user had just made. The command wrote a value that no other command honoured.

## decision

`stratarc.paths.source_root` is the one place the source root is resolved, in this order: the `--root` flag, `STRATARC_SOURCE`, the nearest `stratarc.toml` at or above the current directory, the active entry of `<home>/.stratarc/sources.toml`, then the current directory. The lookup is lazy, read on each call and never at import, and tolerant: a missing, unreadable, malformed or newer-schema file, an active name no entry carries, and a directory that no longer exists are all ignored for resolution and never raise. The commands that manage the file still report such a file, because they are the ones that can fix it. `stratarc doctor` names the origin as `active source (sources.toml)` when this step decided.

## alternatives

Each command resolving its own root was rejected because the resource module already did so with its own copy of the order, and a second copy is how the defect arose. Storing the active source in `stratarc.toml` was rejected because that file lives inside a source root, so it cannot say which root to open, and because it is committed while the choice is per user and per machine. Placing the active source above the nearest `stratarc.toml` was rejected because standing in a source root should always win over a remembered choice, the same way a repository's own configuration wins over a global one.

## consequences

`source use` now changes what every later command reads, with no flag, variable or configuration file. A stale active entry costs nothing: resolution falls through to the current directory, as if none were set. The precedence is stated once in `paths.py` and repeated in the command reference. The home override moves the whole lookup, since the file is found through the home, so a test or a clean-room run that sets `STRATARC_HOME` sees its own active source. The schema ceiling is duplicated in `paths.py` because importing the home layout there would be circular; a test fails if the two numbers differ.
