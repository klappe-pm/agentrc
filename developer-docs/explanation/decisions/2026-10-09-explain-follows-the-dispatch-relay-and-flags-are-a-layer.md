# 2026-10-09-explain-follows-the-dispatch-relay-and-flags-are-a-layer

## status

accepted, 2026-10-09. Extends [layer order and provenance](2026-10-09-layer-order-and-provenance.md) and [list modes use a modes table](2026-10-09-list-modes-use-a-modes-table.md).

## context

The design says `explain` shows the relay for a sub-agent started by another agent: which parent's settings it inherited, which it did not and where the account came from. The resolver had no notion of a parent, so nothing could be shown. It also had a flags layer in its order with no way to set it from the command line, so a one-off override meant editing a file or exporting a variable.

## decision

An agent file may carry three reserved top-level keys, read by the resolver and never treated as settings: `parent` (an agent name), `relay` with `inherit` (a list of dotted-key patterns, default none) and `account`. The child takes from its parent's effective settings only the keys its `inherit` matches, so nothing is inherited implicitly. Inherited values enter the agent layer below the child's own values. The chain is followed to its root with each link applying its own `inherit`. A cycle fails with `relay-cycle` and prints the loop, and a chain longer than 8 agents fails with `relay-depth`. The account is `--account`, else the child's `account`, else the nearest parent's. `explain` prints each edge, the parent that supplied the key, a parent value left behind with the reason, and the account origin; `explain --tree` lists the relay edges of the project's agents.

`--set KEY=VALUE`, `--set-json KEY=JSON` and `--set-mode KEY=replace|extend` feed the flags layer on `get`, `list` and `explain`. The layer is applied last. Its provenance is layer `flags`, file `--set`, line 0. A flag list follows the list-mode rule: it needs `--set-mode` when it redefines a list, and a mode on a non-list is an error. `--set` is always a literal string and `--set-json` carries typed values.

## alternatives

- Inherit everything from the parent unless blocked. It is shorter to write, but a permission set would flow down by default, which is the silent behaviour the list-mode rule exists to prevent.
- Name the parent from the command line instead of the agent file. The relay would not be reviewable in the source root and `explain --tree` could not show it.
- Parse `--set` values as JSON when they look like numbers or booleans. It saves a flag, but `--set k=1` and `--set k=true` would change meaning with the value, and a string that happens to be valid JSON would change type.
- Let a flag list append by default. Rejected for the same reason as a file list: a silent default is how a permission list gets replaced when extending was meant.

## consequences

- A child's settings are reproducible from its own file and its chain; the relay answer comes from the same data structure as every other `explain`.
- Because the default inherits nothing, adding `parent` to an agent file changes only its account until `inherit` is written.
- `parent`, `relay` and `account` can no longer be used as top-level setting names inside an agent file.
- `--set permissions.timeout=5` yields the string `"5"`; a number needs `--set-json`.
- The depth limit and the cycle check run on every load that names an agent, and `explain --tree` reads every agent file of the project to list its edges, so a malformed relay in any of them surfaces there.
