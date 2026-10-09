# control-plane

Generated inventory and deployment declaration for this source root. Do not add rows by hand. Run `stratarc reconcile` after adding, moving or deleting a source file or project, and the reconciler fills the tables below: one row per project, rule, hook, skill, command, agent and runtime, with one column for `global` and one per active project. An `x` in a cell opts that resource in for that scope; the reconciler keeps existing opt-in cells and adds new rows with their defaults.

## projects

| project | status | tier | template | origin |
|---|---|---|---|---|
| notes-cli | active | normal | base | example-owner/notes-cli |

## configuration

Each table has `global` first, then active project columns in alphabetical order. Column A links to the source file. `x` opts in and any other cell opts out.

## instruction-file

| option | global | notes-cli |
|---|---|---|
| [AGENTS.md](AGENTS.md) | x | x |

## rules

| option | tier | global | notes-cli |
|---|---|---|---|
| [rule:conventional-commit-messages](rules/conventional-commit-messages.md) | common | x | x |
| [rule:never-commit-local-databases](rules/never-commit-local-databases.md) | global | x | x |
| [rule:tests-use-a-temporary-directory](rules/tests-use-a-temporary-directory.md) | project |  | x |

## hooks-pre-tool-use

| option | global | notes-cli |
|---|---|---|
| [hook:block-database-commits](hooks/block-database-commits.sh) | x |  |

## skills

| option | global | notes-cli |
|---|---|---|
| [skill:write-changelog-entry](skills/write-changelog-entry/SKILL.md) | x | x |

## commands

| option | global | notes-cli |
|---|---|---|
| [command:test-summary](commands/test-summary.md) | x |  |

## agents

| option | global | notes-cli |
|---|---|---|
| [agent:diff-reviewer](agents/diff-reviewer.md) | x |  |

## project-local-configuration

| option | global | notes-cli |
|---|---|---|
| [project:AGENTS.md](projects-root/notes-cli/AGENTS.md) |  | x |
| [project:rules](projects-root/notes-cli/rules/storage-format-is-versioned.md) |  | x |

## runtimes

| option | global | notes-cli |
|---|---|---|
| [runtime:claude](stratarc.toml) | x |  |
| [runtime:codex](stratarc.toml) | x |  |
| [runtime:cursor](stratarc.toml) | x |  |
| [runtime:gemini](stratarc.toml) | x |  |
| [runtime:opencode](stratarc.toml) | x |  |
