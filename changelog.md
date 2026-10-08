# changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## unreleased

### added

- Package scaffold: `pyproject.toml` with the hatchling build backend and the `agentrc` console script.
- `agentrc init`, which scaffolds a source root from the bundled template.
- `agentrc sync`, `check`, `diff`, `prune` and `reconcile` as placeholders that exit with status 2 until the engine is extracted.
- Runtime assets under `agentrc/data/` (templates, schema, hooks, git hooks), resolved through `importlib.resources`.
- Repository documents: `README.md`, `AGENTS.md`, `contributing.md`, `security.md` and `support.md`.
- Documentation in two trees arranged by Diátaxis quadrant: `docs/` for people who use agentrc and `developer-docs/` for people who change it, each with tutorials, how-to guides, reference and explanation folders and a `README.md` per folder listing its pages.
