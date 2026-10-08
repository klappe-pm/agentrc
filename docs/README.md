# docs

Documentation for people who use agentrc: you run the command, keep a source root and deploy it into your runtimes. It is arranged by the four [Diátaxis](https://diataxis.fr/) quadrants, so the folder a page sits in tells you what kind of page it is. If you change agentrc itself, the same shape exists under [developer-docs](../developer-docs/README.md).

## tutorials

Lessons that take you through a task from start to finish, for someone who has not done it before. Start with [getting started](tutorials/getting-started.md), which goes from an empty machine to a scaffolded source root and a first deployment. Index: [tutorials](tutorials/README.md).

## how-to-guides

Recipes for a specific goal, for someone who already knows the basics and wants the steps. [install](how-to-guides/install.md) covers every install method, [migrate existing config](how-to-guides/migrate-existing-config.md) brings a hand-maintained runtime directory under a source root, and [troubleshooting](how-to-guides/troubleshooting.md) matches a failure to its fix. Index: [how-to guides](how-to-guides/README.md).

## reference

Lookup pages that describe the machinery as it is, with no instruction attached. [cli](reference/cli.md) lists every subcommand and exit status, [configuration](reference/configuration.md) lists every key in `agentrc.toml` and the schemas that validate the JSON files, and [runtimes](reference/runtimes/README.md) names each supported runtime with a page per runtime: [claude](reference/runtimes/claude.md), [codex](reference/runtimes/codex.md), [cursor](reference/runtimes/cursor.md), [gemini](reference/runtimes/gemini.md) and [opencode](reference/runtimes/opencode.md). Index: [reference](reference/README.md).

## explanation

Discussion that gives background and reasons, for a reader who wants to understand rather than act. [security model](explanation/security-model.md) explains what agentrc protects against, what it trusts and what it leaves to the runtimes. Index: [explanation](explanation/README.md).

## documentation-guide

How this documentation is organised and how to add to it. [documentation guide](documentation-guide/README.md) says which folder a new page belongs in, and [feature documentation template](documentation-guide/feature-documentation-template.md) is the outline for a page that documents one feature end to end.
