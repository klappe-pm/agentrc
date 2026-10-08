# getting-started

This tutorial takes a new user from an empty machine to a first deployment. You install the command, scaffold a source root with `agentrc init`, choose runtimes in `agentrc.toml`, add a first rule and skill, and run `agentrc sync`. It is a lesson rather than a reference: it follows one path and explains only what that path needs. For the full command surface see the [cli reference](../reference/cli.md), and for every configuration key see [configuration](../reference/configuration.md).

Until the engine is extracted only the install and `init` steps work; the remaining steps are written here as `sync` lands.

## what-you-need

- Python 3.11 or newer.
- `pipx`, or a virtual environment you are willing to install into.
- At least one of the supported runtimes installed: Claude Code, Codex, Gemini CLI, Cursor or OpenCode.

## install-the-command

Until the first release reaches PyPI, install from the repository:

```bash
pipx install git+https://github.com/klappe-pm/agentrc
```

Confirm the command is on your path:

```bash
agentrc --version
```

The [install guide](../how-to-guides/install.md) covers other install methods and upgrading.

## scaffold-a-source-root

Pick a directory that does not exist yet, or is empty, and scaffold into it:

```bash
agentrc init ~/agentrc-source
```

`init` copies the bundled template: `AGENTS.md`, `agentrc.toml`, `permissions.json`, `components.json`, `control-plane.md` and the `rules/`, `hooks/`, `skills/`, `commands/`, `agents/` and `projects-root/` directories, each with a README that explains what belongs in it. It refuses to write into a directory that already holds files.

## choose-your-runtimes

Open `~/agentrc-source/agentrc.toml`. One table per runtime names whether it is enabled and which directory it deploys to. Set `enabled = true` for each runtime you use and leave the rest disabled.

## add-a-rule-and-a-skill

A rule is a Markdown file under `rules/` with an H1 equal to its filename stem and a `## binding` section. A skill is a directory under `skills/` holding a `SKILL.md` with `name` and `description` frontmatter. The README in each directory describes the shape, and the [notes-cli example](../../examples/notes-cli/README.md) in the repository is a complete source root that walks through every command with a rule, a hook, a skill, a command, an agent and a managed project.

## deploy

Once `sync` is extracted, deploy to every enabled runtime from the source root:

```bash
cd ~/agentrc-source && agentrc sync
```

Until then the command exits with status 2 and says so.

## next-steps

- Move an existing hand-maintained configuration in: [migrate existing config](../how-to-guides/migrate-existing-config.md).
- Understand what agentrc protects against: [security model](../explanation/security-model.md).
- See what each runtime receives: [runtimes](../reference/runtimes/README.md).
