# run-the-tests

This guide covers every check a change must pass: the test suite and its markers, and the lint and scan steps the CI workflows run. Each command below is what CI runs, so a change that passes them locally passes on the pull request. It assumes the editable install from [first contribution](../tutorials/first-contribution.md).

## the-test-suite

```bash
python3 -m pytest
```

Tests live under `tests/` and read only fixtures under `tests/fixtures/`, never a real home directory or runtime configuration. CI runs the suite on Python 3.11 and 3.13.

## the-slow-marker

One test is marked `slow`: it builds a wheel from the checkout, installs it into a fresh virtual environment and runs the installed `stratarc init` outside the checkout, checking that the result matches the bundled template byte for byte. It then runs `stratarc sync --dry-run` and `stratarc --json doctor` against that scaffold under a temporary `HOME`, checking that nothing is written. It is the test that proves the package data and the adapters ship correctly; see [package data](../reference/package-data.md). Skip it while iterating and run it before you commit:

```bash
python3 -m pytest -m 'not slow'
```

## the-golden-example

`tests/test_examples.py` runs `stratarc diff`, `sync` and `check` on a copy of `examples/notes-cli/source` in a temporary home and compares what `sync` wrote with `examples/notes-cli/expected`, byte for byte. In the expected files `<home>` stands for the temporary home path. A sync also writes guard libraries, CI templates, the OpenCode plugin and per-target stamp files that `expected/` does not hold; the `ENGINE_OWNED` pattern in the test lists them. When an adapter or the engine changes what a sync writes, the test names the files that differ; update them from the output of a sync in a throwaway home, as the [example](../../examples/notes-cli/README.md) describes.

## shellcheck

CI lints every tracked `.sh` file and every file under `stratarc/data/git-hooks/`:

```bash
git ls-files -z '*.sh' 'stratarc/data/git-hooks/*' | xargs -0 shellcheck -x -P SCRIPTDIR
```

## the-token-shaped-value-scan

```bash
bash scripts/ci/token-scan.sh
```

Scans every tracked and untracked, non-ignored file with the detector in `stratarc/data/hooks/lib/guard-utils.sh` and fails on anything shaped like a credential. It prints the file and the kind of match, never the value.

## the-frontmatter-key-check

```bash
python3 scripts/ci/frontmatter-keys.py
```

Fails on a `models`, `providers` or `session-link` key in the frontmatter of any Markdown file, including every page under `docs/` and `developer-docs/`. The repository never records session provenance; `python -m stratarc.strip_provenance PATH` removes the keys from a file that arrived with them.

## the-attribution-check

CI also runs `stratarc/data/ci/attribution-check.py` over the pull request title, body and commit messages, and over each pushed commit. It needs a GitHub event payload, so it is not run locally; the `commit-msg` hook under `stratarc/data/git-hooks/` strips the same forms from a commit message before it lands.
