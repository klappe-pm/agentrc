# 2026-10-09-guards-overlay-package-data

## status

Accepted, 2026-10-09, by the maintainer.

## context

The engine ships guard scripts (hooks that check prose, attribution, environment dumps and similar) and a registry, `hooks.json`, that says which events run them. A source root may want the shipped guards unchanged, may replace one, or may add its own. The question is where the guards live and who owns them.

## decision

Guard scripts ship as package data under `stratarc/data/hooks`, and the staging step overlays the source root's own `hooks/` directory on top of them file by file, with the source winning. A hook script in the source root replaces the packaged script of the same name. The `hooks.json` registries merge by the set of hook scripts a group runs, so a source group replaces the packaged group that runs the same scripts and any other group is appended. The `lib/` directory is copied from the package first and from the source root second, and a `private/` directory under either `lib/` is never staged. The behavior is implemented in [staging.py](../../../stratarc/staging.py).

## alternatives

- Have `stratarc init` copy the guards into the source root. Rejected: the user would own those copies and drift from them, and an upgrade of the package would never reach a source root that already holds a copy.
- Ship the guards as package data with no overlay. Rejected: a source root could neither replace a guard nor add its own without forking the engine.
- Replace the registry wholesale when the source root has one. Rejected: a source root that only adds one group would silently lose every packaged group.

## consequences

- A package upgrade improves the guards for every source root that has not overridden them, with no migration step.
- A source root overrides a single script or registry group without copying the rest.
- Because the source wins per file, an override of a packaged script does not receive later fixes to that script, and its owner must carry them.
- The private directory exclusion keeps a source root's private material out of every staged copy, as the [staging module](../../../stratarc/staging.py) documents.
