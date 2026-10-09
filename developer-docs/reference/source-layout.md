# source-layout

This page describes the source root that `stratarc init` creates: each file and directory, what it holds, and which runtimes consume it. It is the reference for a contributor changing the template under `stratarc/data/templates/source-root/` or the code that reads a source root. The user-facing description of the same files is in [configuration](../../docs/reference/configuration.md).
## files

| path | holds | read by |
| --- | --- | --- |
| `AGENTS.md` | the instructions every agent session starts from, rendered into each runtime's instruction file | every runtime |
| `stratarc.toml` | the enabled runtimes, their targets, the projects root and the repository owner | `stratarc` itself |
| `permissions.json` | the single permission policy, translated into each runtime's native allow, deny and ask lists | every runtime with a permission model |
| `components.json` | external components: MCP servers, plugins, foreign hook groups, third-party skills and dependencies | every runtime that supports the component |
| `control-plane.md` | generated inventory of every source item and project with its opt-in cells; written by `stratarc reconcile`, never by hand | `stratarc` itself |

## where-configuration-is-read

`stratarc/paths.py` and `stratarc/config.py` are the only modules that read `STRATARC_HOME`, `STRATARC_SOURCE`, `LLM_ROOT_PROJECTS_DIR`, `STRATARC_GITHUB_OWNER` and `stratarc.toml`; every other module asks them. The home is `STRATARC_HOME`, then `HOME`, then the platform home. The source root is an explicit argument, then `STRATARC_SOURCE`, then the nearest directory at or above the current directory that holds `stratarc.toml`, then the current directory. The projects root is `LLM_ROOT_PROJECTS_DIR`, then `<home>/projects/active`. The owner is `STRATARC_GITHUB_OWNER`, then `owner` in `stratarc.toml`, then empty. All of it is read when called, never at import. The command line flags `--root`, `--home`, `--projects-root` and `--owner` set `STRATARC_SOURCE`, `STRATARC_HOME`, `LLM_ROOT_PROJECTS_DIR` and `STRATARC_GITHUB_OWNER` for the length of one command, so a flag wins over the environment and the environment over `stratarc.toml`; `stratarc doctor` prints which of them resolved the source root. The [cli reference](../../docs/reference/cli.md) lists every flag.

## directories

| path | holds | read by |
| --- | --- | --- |
| `rules/` | one Markdown file per standing rule, with `tiers.json` naming which tier each rule belongs to | every runtime, through the rules digest |
| `hooks/` | `hooks.json`, the hook registry, and the hook scripts it names | every runtime with a hook system |
| `skills/` | one directory per skill holding a `SKILL.md` | every runtime with skills |
| `commands/` | one Markdown file per slash command | every runtime with commands |
| `agents/` | one Markdown file per subagent definition | every runtime with subagents |
| `projects-root/` | one directory per managed project holding its project-local configuration | `stratarc` itself, when deploying into that project |

Each directory the template creates holds a `README.md` describing the shape of the files that belong in it. The bundled template is the authority for that shape; [examples/notes-cli](../../examples/notes-cli/README.md) shows a populated one.
