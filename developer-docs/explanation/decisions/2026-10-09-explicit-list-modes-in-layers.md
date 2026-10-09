# 2026-10-09-explicit-list-modes-in-layers

## status

accepted for the resolver, 2026-10-09

proposed for the `_modes` syntax, 2026-10-09: the spelling is open to change before the first release.

## context

A setting can be defined in several layers, and a list such as a permission allow list can be redefined by a higher layer. The author may mean "replace the list" or "add to it", and the two outcomes differ sharply for a permission list. The open question is recorded in [cli-design.md](../cli-design.md).

## decision

A list that a higher layer redefines carries an explicit mode, `replace` or `extend`, in a `_modes` table beside the list. The resolver in [layers.py](../../../stratarc/layers.py) never defaults one: a redefined list with no mode fails with `list-mode-missing`. The first layer that defines a list needs no mode. The syntax of the `_modes` table is a proposal and may change before the first release; the rule that the resolver never guesses is accepted.

## alternatives

- Default to replace. Predictable, but an author who meant to extend silently drops every inherited entry.
- Default to extend. Friendlier for adding entries, but an author who meant to replace silently keeps entries they wanted gone. This was rejected: a silent default is how a permission list is replaced when extending was meant, or extended when replacing was meant.
- A per-key marker inside the list values. It avoids a second table but mixes data with control and complicates every consumer of the list.

## consequences

- An ambiguous override is caught at resolution time, with the file and line, instead of surfacing as a wrong deployed permission.
- Authors write one extra line when they override a list.
- If the `_modes` spelling changes, the files in the example and template must change with it, and the resolver can accept both for one release.
