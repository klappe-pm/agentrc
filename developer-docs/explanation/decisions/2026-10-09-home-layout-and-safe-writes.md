# 2026-10-09-home-layout-and-safe-writes

## status

accepted, 2026-10-09

proposed for one point, 2026-10-09: whether the XDG configuration path should be the primary home or an alias for `.stratarc` is open.

## context

The tool keeps machine state (settings, known source roots, providers, adapters, backups, logs, cache) apart from any project. That state is edited by commands and by hand, survives upgrades and can be written by a newer or older version of the tool. Losing or silently downgrading it would be costly. See [home_layout.py](../../../stratarc/home_layout.py) and the home section of [cli-design.md](../cli-design.md).

## decision

The `.stratarc` home holds `config.toml`, `sources.toml`, `providers/`, `adapters/`, `state/`, `backups/`, `logs/` and `cache/`. Directories are mode 0700 and files 0600, and every file carries a schema version. A file with a newer schema is never rewritten and the command exits 5. Every write first copies the file it replaces into `backups/`. A clean removes only backups and cache, and only with `--yes`; it keeps the newest 20 versions of each file plus anything under 30 days old. Whether the XDG path should be primary is left open and this point stays proposed.

## alternatives

- Write in place with no backup. Simplest, but a bad write is unrecoverable.
- Silently migrate a newer file down. It would corrupt data written by a newer release.
- Let clean remove any state. It risks deleting user data such as providers and adapters, so only disposable directories are candidates.
- Use the XDG path only. Standard on some systems, but it splits the layout across platforms; undecided.

## consequences

- A downgrade is safe: the old tool refuses to touch what it cannot understand.
- Backups grow until a clean runs, bounded by the keep rules.
- Secrets never live in the home; providers hold references only, as in [the provider decision](2026-10-09-providers-hold-secret-references-only.md).
- If the XDG question resolves toward XDG, a superseding record must describe the migration of existing homes.
