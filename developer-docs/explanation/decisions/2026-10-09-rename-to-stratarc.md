# 2026-10-09-rename-to-stratarc

## status

Accepted, 2026-10-09, by the maintainer.

## context

The product was first named agentrc. Another published project, released under the MIT license and widely starred, installs a binary named agentrc, so a machine that installed both would have one command shadow the other. The collision was found before the first release, which made a rename cheap because nothing had been published under the old name.

A registry check of three candidates (stratarc, loomrc and cairnrc) found all three free on PyPI, npm, crates.io, Homebrew, Debian source and winget, with no existing GitHub repository of that name. The check did not cover trademark registers or short aliases.

## decision

The product is named stratarc. The package, the console script, the home directory, the `STRATARC_*` environment variables and the `stratarc.toml` file all take the new name, and the repository is renamed before the first release. Anything that must still say the old name is limited to the changelog entry that records the rename.

## alternatives

- Keep agentrc and document the collision. Rejected: a user who installs both gets whichever binary comes first on the path, with no error, which is the failure the rename exists to remove.
- Choose loomrc or cairnrc. Both passed the same registry check. The maintainer chose stratarc.
- Ship a second console script under a different name only for the colliding install. Rejected: it leaves the product with a name that is not unique and splits the documentation between two commands.

## consequences

- Every public name derives from one word, so a later contributor has one name to search for.
- GitHub redirects the old repository URL after the rename, so earlier links keep working.
- Trademark registers and short aliases were not checked. A conflict found there would need a new record that supersedes this one.
- The rename is recorded under "changed" in the [changelog](../../../changelog.md), and [pyproject.toml](../../../pyproject.toml) carries the package name and the `stratarc` console script.
