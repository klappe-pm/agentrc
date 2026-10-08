# first-contribution

This tutorial takes you from a fresh clone to a merged-ready pull request. You set up a development environment, run the test suite, make one small change, check it the way CI will, and open the pull request. It follows one path and explains only what that path needs; [contributing](../../contributing.md) holds the guidelines it applies, and the [how-to guides](../how-to-guides/README.md) cover each step in more depth.

## what-you-need

- Python 3.11 or newer.
- `git`, and a GitHub account if you intend to open the pull request.
- `shellcheck`, if you will touch a shell script; CI lints every `.sh` file and the git hooks.

## clone-and-install

```bash
git clone https://github.com/klappe-pm/agentrc && cd agentrc && python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[test]'
```

This installs agentrc as an editable package with `pytest` and `jsonschema`, the only test dependencies. The command itself has no runtime dependencies.

## run-the-tests

```bash
python3 -m pytest
```

The full suite includes one slow test that builds a wheel, installs it into a fresh virtual environment and checks that `agentrc init` from the installed package reproduces the bundled template. While iterating, skip it:

```bash
python3 -m pytest -m 'not slow'
```

Run the full suite before you commit. See [run the tests](../how-to-guides/run-the-tests.md) for the other checks CI runs.

## make-a-change

Pick something small for a first change: a wording fix in a page under `docs/`, a missing edge case in `tests/`, a clearer error message in `agentrc/cli.py`. Before you edit, read the part of the user documentation that covers what you are changing, and the [source layout](../reference/source-layout.md) if the change touches the template.

Two conventions catch newcomers. Runtime assets under `agentrc/data/` are reached only through `importlib.resources`, never through a path built from `__file__`; see [package data](../reference/package-data.md) for why. And filenames and Markdown headings are lowercase kebab-case, with one line per paragraph and no hard-wrapped prose.

## check-it-the-way-ci-will

```bash
python3 -m pytest
```

```bash
bash scripts/ci/token-scan.sh
```

```bash
python3 scripts/ci/frontmatter-keys.py
```

The token scan fails on anything shaped like a credential anywhere in the tree. The frontmatter check fails on a `models`, `providers` or `session-link` key in any Markdown file; this repository never records session provenance.

## commit-and-open-the-pull-request

Commit with a [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) message, for example `docs: clarify the init target rule` or `fix: refuse an init target that is a file`. Add a line under `unreleased` in [changelog.md](../../changelog.md) if the change is visible to a user. Never add an agent, model or tool as an author or co-author; the repository's checks reject it.

Push your branch and open a pull request against `main`. Keep it to one change, say what it does and how you tested it, and make sure the CI checks pass. A maintainer will review it from there.

## next-steps

- Read [architecture](../explanation/architecture.md) before a change that touches more than one part of the pipeline.
- Read [add a runtime](../how-to-guides/add-a-runtime.md) if you want to support another agent runtime.
- Read the [decision records](../explanation/decisions/README.md) before proposing a change to something they settle.
