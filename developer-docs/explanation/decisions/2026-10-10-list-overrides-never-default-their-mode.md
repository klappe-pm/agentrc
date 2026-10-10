# 2026-10-10-list-overrides-never-default-their-mode

## status

accepted, 2026-10-10

## context

[Explicit list modes in layers](2026-10-09-explicit-list-modes-in-layers.md) and [list modes use a modes table](2026-10-09-list-modes-use-a-modes-table.md) require a list that a higher layer redefines to carry `replace` or `extend`, and both say the resolver never defaults a mode. The backlog kept an open question anyway: whether list-valued settings should default to replace or extend when a file omits the mode.

## decision

They do not default. The resolver keeps failing with `list-mode-missing` when a higher layer redefines a list without a mode, and no code changes. Only the first layer that defines a list may omit its mode. This closes the open question in the backlog and confirms the two earlier records, which are not edited.

## alternatives

Defaulting to replace was rejected because an author who meant to extend silently drops every inherited entry. Defaulting to extend was rejected because an author who meant to replace silently keeps entries they wanted gone, which for a permission list is the worse failure. A per-file or per-layer default was rejected because one file often holds both kinds of list.

## consequences

An author writes one extra line per overridden list, and the error names the key and the layer, so the fix is mechanical. A future change of mind needs a record that supersedes this one and the two it confirms.
