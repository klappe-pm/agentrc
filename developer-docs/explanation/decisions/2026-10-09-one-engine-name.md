# 2026-10-09-one-engine-name

## status

Accepted, 2026-10-09, by the maintainer.

## context

Before the engine was published, many generated names were hardcoded to the operator's own project: marker comments in generated files, permission profile names, ledger and stamp file names, git config keys and table names in runtime configuration. A published engine cannot carry one operator's name, and a user who renames the product should not have to edit the code in many places.

## decision

Every generated file, marker, table and state name that used to be hardcoded derives from one engine name. The default is `stratarc`. `paths.engine_name()` in [paths.py](../../../stratarc/paths.py) resolves it from `STRATARC_NAME`, then from `name` in the source root's `stratarc.toml`, and the value must match `^[a-z][a-z0-9-]*$` ([config.py](../../../stratarc/config.py)). A stage records its name in a one-line `stratarc.toml` so that adapters, which read only the stage, resolve the same name. Setting the name to the previous hardcoded value reproduces the old output byte for byte, and a test covers it in [test_engine_name.py](../../../tests/test_engine_name.py).

## alternatives

- Hardcode `stratarc` everywhere. Rejected: a source root migrating from the earlier names would see every marker and state file change, and its existing deployed files would no longer be recognized as owned.
- Give each kind of name its own setting. Rejected: it multiplies the places a user can get them out of step, and nothing needs the names to differ.
- Let adapters read the name from the environment alone. Rejected: an adapter runs against a stage and must see the same name the stage was built with, whatever the environment says later.

## consequences

- One setting renames everything the engine generates, and the byte-for-byte test guards the migration path.
- A malformed name is refused early, with an error naming the setting.
- Code that builds a marker, table or state name must go through the engine name and never spell one literally.
