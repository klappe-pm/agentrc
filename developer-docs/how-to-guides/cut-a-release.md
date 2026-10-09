# cut-a-release

This guide is for a maintainer releasing a new version of stratarc: bumping the version, settling the changelog, tagging and publishing the package. It describes the steps as they stand for the scaffold release; the publish step is filled in when the first release reaches PyPI. It assumes a clean checkout of `main` with CI green.

## where-the-version-lives

The version is `__version__` in `stratarc/__init__.py`. `pyproject.toml` declares the version as dynamic and reads it from that file through hatchling, so it is set in one place. The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## steps

1. Decide the new version from the `unreleased` section of [changelog.md](../../changelog.md): a breaking change bumps the major version, a new capability bumps the minor, a fix bumps the patch.
2. Set `__version__` in `stratarc/__init__.py`.
3. In `changelog.md`, rename `unreleased` to the version and the date, and open a new empty `unreleased` section above it.
4. Run the full test suite, including the slow wheel test:

```bash
python3 -m pytest
```

5. Commit as `chore: release <version>` and push to `main` through a pull request like any other change.
6. Tag the merge commit `v<version>` and push the tag:

```bash
git tag -a v<version> -m "stratarc <version>" && git push origin v<version>
```

7. Build the distributions and check them:

```bash
python3 -m pip wheel --no-deps -w dist . && ls dist
```

8. Publish. Until the PyPI project exists this step is a GitHub release on the tag with the changelog section as its notes; once it exists, the upload is documented here.

## after-the-release

Confirm `pipx install stratarc==<version>` (or the repository install at the tag) scaffolds a source root with `stratarc init`, and update the install commands in the [user documentation](../../docs/how-to-guides/install.md) if the first PyPI release changes them.
