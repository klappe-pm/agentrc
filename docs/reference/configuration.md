# configuration

This page documents `agentrc.toml`, the file that names the enabled runtimes, their target paths, the projects root and the repository owner, together with the environment variables that override them and the JSON Schemas that validate `hooks.json`, `permissions.json` and `components.json`. It is a lookup page: every key with its type, default and an example. It will be completed as the engine that reads each key is extracted.

## agentrc-toml

`agentrc init` writes this file at the root of the source root with every key present and commented.

| key | type | default | meaning |
| --- | --- | --- | --- |
| `owner` | string | `""` | the GitHub user or organization login whose repositories count as managed projects; empty treats none as managed by owner |
| `projects_root` | string | `"~/projects"` | the directory that holds your project checkouts; `~` expands to your home directory |
| `runtimes.<name>.enabled` | boolean | `true` for `claude`, `false` otherwise | whether `agentrc sync` builds and deploys this runtime |
| `runtimes.<name>.target` | string | the runtime's conventional directory | the directory the runtime reads its configuration from |

The recognised runtime names and their default targets are `claude` (`~/.claude`), `codex` (`~/.codex`), `gemini` (`~/.gemini`), `cursor` (`~/.cursor`) and `opencode` (`~/.config/opencode`). See [runtimes](runtimes/README.md) for what each receives.

## schemas

Three JSON Schemas ship with the package under `agentrc/data/schema/` and validate the JSON files in a source root.

| schema | validates |
| --- | --- |
| `hooks.schema.json` | `hooks/hooks.json`, the hook registry: each canonical event maps to a list of hook groups with an optional tool matcher and command hooks |
| `permissions.schema.json` | `permissions.json`, the single permission policy every runtime adapter translates |
| `components.schema.json` | `components.json`, the manifest of external components: MCP servers, plugins, foreign hook groups, third-party skills and dependencies |

## environment-variables

None are read yet. Overrides will be listed here as the engine lands.
