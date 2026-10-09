# 2026-10-09-private-extensions-live-in-the-source-root

## status

Accepted, 2026-10-09, by the maintainer.

## context

Some behavior belongs to one operator's tree and not to the published engine: extra validation checks, renderers for particular projects, deploy platforms that components.json can name, and patterns the environment-dump guard should recognize. The engine needs a way to load these without containing them.

## decision

Optional extensions live in the source root and are found through `paths.source_root()`. Private validator checks are read from `scripts/private/validate_checks.py` ([validate.py](../../../stratarc/validate.py)), project renderers from `scripts/private/project_renderers.py` ([projects.py](../../../stratarc/projects.py)), deploy platforms from `scripts/private/components_platforms.py` ([components.py](../../../stratarc/components.py)), and environment-dump patterns from `hooks/lib/private/env-dump-patterns.json`. Each is loaded by file path. An absent extension is a no-op, and a broken one is reported and skipped so the rest of the run still completes.

## alternatives

- Python entry points. Rejected: the operator's tree is not an installable package, so an entry point would force it to become one just to add a check.
- Configuration in `stratarc.toml` naming a module to import. Rejected: it adds a second place to look for something the source root's layout already locates.
- Build the extensions into the engine behind switches. Rejected: it would publish material that belongs to a single operator and grow the engine for every user.

## consequences

- A source root with no extensions behaves as a plain one, with the generic checks only.
- Extensions are versioned with the source root that uses them, so they change in the same commit as the files they check.
- An extension runs arbitrary code from the source root, so loading one is a trust decision made by whoever owns that tree.
- A failing extension never loses the gate: the validator reports it as an error finding and keeps running the other checks.
