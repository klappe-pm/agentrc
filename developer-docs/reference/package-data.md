# package-data

This page describes how the runtime assets under `stratarc/data/` are packaged and resolved, for a contributor adding an asset or changing code that reads one. The assets are the source-root template, the JSON Schemas, the guard libraries, the git hooks, the CI attribution check and helper executables. They are read through `importlib.resources`, never through a path built from `__file__` or relative to the repository root, so a checkout and an installed wheel behave identically.

## what-lives-under-data

| path | contents |
| --- | --- |
| `stratarc/data/templates/source-root/` | the tree `stratarc init` copies into a new source root |
| `stratarc/data/schema/` | `hooks.schema.json`, `permissions.schema.json` and `components.schema.json`, with a README describing each |
| `stratarc/data/hooks/lib/` | `guard-utils.sh`, the token-shaped value detector, and `attribution-detect.py`, the attribution detector |
| `stratarc/data/git-hooks/` | the `commit-msg`, `pre-commit` and `post-commit` hooks, `strip-commit-attribution.sh` and the `check-staged-*.sh` checks they run |
| `stratarc/data/ci/` | `attribution-check.py` and the two workflow templates, `attribution-check.yml` and `attribution-check-comment.yml`, that stratarc renders into a managed project |
| `stratarc/data/bin/` | helper executables the install script copies onto the user's PATH, currently `caffeinate-shim.sh` |

## how-code-reaches-an-asset

`stratarc/resources.py` exposes one function:

```python
from stratarc.resources import data_dir

template = data_dir("templates/source-root")
```

`data_dir` starts from `importlib.resources.files("stratarc") / "data"` and walks the slash-separated name one segment at a time, returning a `Traversable`. Callers use the `Traversable` interface (`iterdir`, `is_dir`, `is_file`, `read_bytes`) rather than converting it to a filesystem path, because in an installed package it may not be one. `stratarc/cli.py` copies the template that way: `_copy_tree` iterates the `Traversable` and writes each file's bytes to the target, skipping `__pycache__`.

## how-the-assets-ship

`pyproject.toml` builds with hatchling and lists `stratarc/data/**` under `artifacts` for both the wheel and the sdist. That line is what carries non-Python files, dotfiles included, into the distribution; without it only `.py` files would ship and `stratarc init` from an installed wheel would find no template. The sdist additionally includes `tests`, `examples`, `README.md`, `LICENSE` and `pyproject.toml`.

## how-it-is-tested

`tests/test_wheel_install.py`, marked `slow`, builds a wheel from the checkout, installs it into a fresh virtual environment and runs the installed `stratarc init` with the working directory outside the checkout, so the installed package and not the source tree is imported. It compares the scaffold with the template byte for byte. `tests/test_cli.py` checks the same copy from the checkout. Together they catch an asset that was added to the tree but not to the distribution, and a reader that works only when `data/` is a real directory.

## adding-an-asset

1. Put the file under the right directory in `stratarc/data/`.
2. Reach it through `data_dir("<path under data>")` and the `Traversable` interface.
3. Run the full test suite including the slow test; see [run the tests](../how-to-guides/run-the-tests.md).

No change to `pyproject.toml` is needed: the `artifacts` glob already covers everything under `stratarc/data/`.
