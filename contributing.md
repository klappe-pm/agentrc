# contributing

Thank you for helping improve stratarc. The engine is still being extracted in stages, so open an issue to discuss a change before writing a large pull request. This page holds the rules every contribution follows; the walkthroughs and reference live under [developer-docs](developer-docs/README.md), starting with [first contribution](developer-docs/tutorials/first-contribution.md).

## setup

```bash
git clone https://github.com/klappe-pm/stratarc && cd stratarc && python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[test]'
```

## tests

Run the suite before every commit. The slow marker builds a wheel and installs it into a fresh virtual environment. [run the tests](developer-docs/how-to-guides/run-the-tests.md) covers the other checks CI runs.

```bash
python3 -m pytest
```

```bash
python3 -m pytest -m 'not slow'
```

## guidelines

- Python 3.11 or newer, standard library only at runtime. `pytest` and `jsonschema` are test dependencies.
- Runtime assets live under `stratarc/data/` and are read through `importlib.resources`, never through a path built from `__file__`. See [package data](developer-docs/reference/package-data.md).
- Tests read fixtures only: never the live home directory or a user's real runtime configuration.
- Filenames and Markdown headings are lowercase kebab-case. Reserved names such as `README.md`, `AGENTS.md` and `LICENSE` keep their spelling.
- Write one line per Markdown paragraph and list item; do not hard-wrap prose.
- Documentation goes in `docs/` for people who use stratarc and `developer-docs/` for people who change it, in the folder the [documentation guide](docs/documentation-guide/README.md) names for its kind.
- Never commit a secret, credential, private key, `.env` file or `settings.local.json`. `.env.example` with placeholder values is allowed.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

## public-repository

Everything committed here is visible to anyone, so treat every file, commit, pull request and comment as published.

- Never add `models`, `providers` or `session-link` keys to the frontmatter of any Markdown file, and never add a session link anywhere: files, commit messages, pull request titles or bodies, or comments.
- Never credit an agent, model, model provider or agent runtime as author or contributor in files, commits, pull requests or comments. No co-author trailers, no "generated with" lines, no session tokens comment on a pull request.
- Agent instruction files and runtime directories at the repository root (`AGENTS.md`, `CLAUDE.md`, `CODEX.md`, `GEMINI.md`, `.claude/` and the like) are gitignored and never tracked; a contributor's own agent configuration stays on their machine. The copies under `stratarc/data/templates/` and `examples/` are product content and stay.
- No process log is committed. When you observe friction in the work itself, end the commit message that carries the work with a section headed `Process observations:` followed by one or two line bulleted entries.

## pull-requests

Keep each pull request to one change, describe what it does and how you tested it, and make sure the test suite passes. Add a line under `unreleased` in [changelog.md](changelog.md) for any user-visible change.

## license

By contributing, you agree that your contributions are licensed under the MIT license in [LICENSE](LICENSE).
