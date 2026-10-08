# source-layout

This page describes the source root that `agentrc init` creates: each file and directory, what it holds, and which runtimes consume it. It is the reference for a contributor changing the template under `agentrc/data/templates/source-root/` or the code that reads a source root. The user-facing description of the same files is in [configuration](../../docs/reference/configuration.md). It is completed as the engine that reads each part is extracted.

## files

| path | holds | read by |
| --- | --- | --- |
| `AGENTS.md` | the instructions every agent session starts from, rendered into each runtime's instruction file | every runtime |
| `agentrc.toml` | the enabled runtimes, their targets, the projects root and the repository owner | `agentrc` itself |
| `permissions.json` | the single permission policy, translated into each runtime's native allow, deny and ask lists | every runtime with a permission model |
| `components.json` | external components: MCP servers, plugins, foreign hook groups, third-party skills and dependencies | every runtime that supports the component |
| `control-plane.md` | generated inventory of every source item and project with its opt-in cells; written by `agentrc reconcile`, never by hand | `agentrc` itself |

## directories

| path | holds | read by |
| --- | --- | --- |
| `rules/` | one Markdown file per standing rule, with `tiers.json` naming which tier each rule belongs to | every runtime, through the rules digest |
| `hooks/` | `hooks.json`, the hook registry, and the hook scripts it names | every runtime with a hook system |
| `skills/` | one directory per skill holding a `SKILL.md` | every runtime with skills |
| `commands/` | one Markdown file per slash command | every runtime with commands |
| `agents/` | one Markdown file per subagent definition | every runtime with subagents |
| `projects-root/` | one directory per managed project holding its project-local configuration | `agentrc` itself, when deploying into that project |

Each directory the template creates holds a `README.md` describing the shape of the files that belong in it. The bundled template is the authority for that shape; [examples/minimal](../../examples/minimal) shows a populated one.
