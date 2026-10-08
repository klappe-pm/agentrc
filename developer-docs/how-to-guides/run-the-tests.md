# run-the-tests

This guide covers every check a change must pass: the test suite and its markers, and the lint and scan steps the CI workflows run. Each command below is what CI runs, so a change that passes them locally passes on the pull request. It assumes the editable install from [first contribution](../tutorials/first-contribution.md).

## the-test-suite

```bash
python3 -m pytest
```

Tests live under `tests/` and read only fixtures under `tests/fixtures/`, never a real home directory or runtime configuration. CI runs the suite on Python 3.11 and 3.13.

## the-slow-marker

One test is marked `slow`: it builds a wheel from the checkout, installs it into a fresh virtual environment and runs the installed `agentrc init` outside the checkout, checking that the result matches the bundled template byte for byte. It is the test that proves the package data ships correctly; see [package data](../reference/package-data.md). Skip it while iterating and run it before you commit:

```bash
python3 -m pytest -m 'not slow'
```

## shellcheck

CI lints every tracked `.sh` file and every file under `agentrc/data/git-hooks/`:

```bash
git ls-files -z '*.sh' 'agentrc/data/git-hooks/*' | xargs -0 shellcheck -x -P SCRIPTDIR
```

## the-token-shaped-value-scan

```bash
bash scripts/ci/token-scan.sh
```

Scans every tracked and untracked, non-ignored file with the detector in `agentrc/data/hooks/lib/guard-utils.sh` and fails on anything shaped like a credential. It prints the file and the kind of match, never the value.

## the-frontmatter-key-check

```bash
python3 scripts/ci/frontmatter-keys.py
```

Fails on a `models`, `providers` or `session-link` key in the frontmatter of any Markdown file, including every page under `docs/` and `developer-docs/`. The repository never records session provenance; `python -m agentrc.strip_provenance PATH` removes the keys from a file that arrived with them.

## the-attribution-check

CI also runs `scripts/ci/attribution-check.py` over the pull request title, body and commit messages, and over each pushed commit. It needs a GitHub event payload, so it is not run locally; the `commit-msg` hook under `agentrc/data/git-hooks/` strips the same forms from a commit message before it lands.
