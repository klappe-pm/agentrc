# changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## unreleased

### added

- Package scaffold: `pyproject.toml` with the hatchling build backend and the `stratarc` console script.
- `stratarc init`, which scaffolds a source root from the bundled template.
- `stratarc sync`, `check`, `diff`, `prune` and `reconcile` as placeholders that exit with status 2 until the engine is extracted.
- Runtime assets under `stratarc/data/` (templates, schema, hooks, git hooks), resolved through `importlib.resources`.
- `stratarc/paths.py` and `stratarc/config.py`, the single readers of the home, source root, projects root and owner settings and of `stratarc.toml`, and a pytest collector for `tests/**/*.test.sh` and `tests/**/*.test.ts` files.
- Repository documents: `README.md`, `contributing.md`, `security.md` and `support.md`.
- Documentation in two trees arranged by Diátaxis quadrant: `docs/` for people who use stratarc and `developer-docs/` for people who change it, each with tutorials, how-to guides, reference and explanation folders and a `README.md` per folder listing its pages.
- `examples/notes-cli/`, a worked example: a complete source root, the runtime output a sync of it should produce, and a walkthrough of `init`, `check`, `diff`, `sync`, `reconcile` and `prune`, with `tests/test_examples.py` to keep it valid.

### changed

- Renamed from agentrc to stratarc before the first release, because another published project installs a binary named agentrc. The package, console script, `STRATARC_*` environment variables, `stratarc.toml` and the `~/.stratarc` home follow the new name.

### removed

- The `minimal` example, replaced by `examples/notes-cli/`.
- The root `AGENTS.md`. Agent instruction files and runtime directories at the repository root are gitignored; their contributor rules moved to `contributing.md`.
- The `NOTICE` file and the `licenses/` directory. Third-party skills, agents and commands are out of scope for this repository, which ships the engine, templates and examples only, so there is no derived content for a notice to describe. `LICENSE` (MIT) is unchanged.
