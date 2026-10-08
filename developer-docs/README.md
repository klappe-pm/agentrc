# developer-docs

Documentation for people who change agentrc: you clone the repository, run its tests, add an adapter or fix a bug, and open a pull request. It uses the same four [Diátaxis](https://diataxis.fr/) quadrants as the user documentation under [docs](../docs/README.md), so the folder a page sits in tells you what kind of page it is. Nothing here is needed to use agentrc; everything here assumes you have read the user documentation for the part you are changing.

## tutorials

Lessons that take you through a task from start to finish, for someone new to the codebase. Start with [first contribution](tutorials/first-contribution.md), which goes from a fresh clone to a passing test suite and a pull request. Index: [tutorials](tutorials/README.md).

## how-to-guides

Recipes for a specific development goal. [add a runtime](how-to-guides/add-a-runtime.md) covers writing an adapter for another agent runtime, [run the tests](how-to-guides/run-the-tests.md) covers the suite, its markers and the CI checks, and [cut a release](how-to-guides/cut-a-release.md) covers versioning, the changelog and tagging. Index: [how-to guides](how-to-guides/README.md).

## reference

Lookup pages that describe the codebase as it is. [source layout](reference/source-layout.md) describes the source root that `agentrc init` creates and which runtimes consume each part, and [package data](reference/package-data.md) describes how the assets under `agentrc/data/` are packaged and resolved through `importlib.resources`. Index: [reference](reference/README.md).

## explanation

Discussion of why agentrc is built the way it is. [architecture](explanation/architecture.md) explains how one source root becomes several runtime configurations, and [decisions](explanation/decisions/README.md) holds agentrc's own decision records, one per choice that shaped the code. Index: [explanation](explanation/README.md).
