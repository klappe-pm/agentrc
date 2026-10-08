# contributing

Thank you for helping improve agentrc. The engine is still being extracted in stages, so open an issue to discuss a change before writing a large pull request.

## setup

```bash
git clone https://github.com/klappe-pm/agentrc && cd agentrc && python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[test]'
```

## tests

Run the suite before every commit. The slow marker builds a wheel and installs it into a fresh virtual environment.

```bash
python3 -m pytest
```

```bash
python3 -m pytest -m 'not slow'
```

## guidelines

- Python 3.11 or newer, standard library only at runtime. `pytest` and `jsonschema` are test dependencies.
- Runtime assets live under `agentrc/data/` and are read through `importlib.resources`, never through a path built from `__file__`.
- Tests read fixtures only: never the live home directory or a user's real runtime configuration.
- Filenames and Markdown headings are lowercase kebab-case. Reserved names such as `README.md`, `AGENTS.md`, `LICENSE` and `COMMENTS-PROCESS.md` keep their spelling.
- Write one line per Markdown paragraph and list item; do not hard-wrap prose.
- Never commit a secret, credential or `.env` file.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

## pull-requests

Keep each pull request to one change, describe what it does and how you tested it, and make sure the test suite passes. Add a line under `unreleased` in [changelog.md](changelog.md) for any user-visible change.

## license

By contributing, you agree that your contributions are licensed under the MIT license in [LICENSE](LICENSE).
