# 2026-10-09-list-modes-use-a-modes-table

## status

accepted, 2026-10-09. Settles the open syntax point in [explicit list modes in layers](2026-10-09-explicit-list-modes-in-layers.md).

## context

The earlier record required an explicit mode on any list a higher layer overrides and left the syntax open. The resolver shipped with a `_modes` table beside the list, for example `"_modes": {"allow": "extend"}` in JSON or a `[permissions._modes]` table in TOML. The worked example's project permissions carry lists without modes, so `config list --project notes-cli` fails until they gain them.

## decision

The `_modes` table is the syntax. Each key names a list in the same object and maps to `replace` or `extend`. A mode on a key that is not a list is an error. Only the first layer that defines a list may omit its mode. The resolver never defaults a mode. The example's project files are updated to carry modes so the worked example resolves end to end.

## alternatives

A suffix on the key name (`allow+`) was rejected because it changes the key that every other reader of the file sees. A single global default mode was rejected for the reason in the earlier record: a silent default is how a permission list is replaced when extending was meant. A per-file mode was rejected because one file often holds both kinds of list.

## consequences

Files stay valid JSON and TOML and other tools ignore the extra table. A reader must look in two places to know what a list does, which `config explain` hides by printing the operation next to each step. Changing the syntax later needs a new record that supersedes this one.
