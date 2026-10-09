# 2026-10-09-starc-is-the-short-command-alias

## status

accepted, 2026-10-09

## context

The command is `stratarc`, which is long to type in a shell. The [rename record](2026-10-09-rename-to-stratarc.md) left a short alias unchecked. The alias only needs to be free as a command name on a user's path; it does not need a package name. Five candidates were checked against PyPI, npm, crates.io, Homebrew formulae and casks, the Debian package and contents searches, the local path on a macOS machine, and GitHub repository search: `strata`, `sarc`, `srt`, `stc` and `strc`, plus `starc`.

## decision

The alias is `starc`, installed as a second console script that points at the same entry point as `stratarc`. It is free on every registry checked, absent from the Debian package and contents searches, and not on the path of the machine used for the check. Of the others, `strata`, `sarc`, `srt` and `stc` are taken on all three language registries, `srt` is also a system package that installs `/usr/bin/srt`, `stc` is the binary of a widely used TypeScript checker, and `strc` is the name of an npm binary that a global install would shadow.

## alternatives

No alias was rejected because typing nine letters on every call is the cost the maintainer wanted removed. `strata` was rejected because an npm package ships a binary of that name and the word is also taken on PyPI and crates.io. A name shorter than five letters was rejected because every one found was a known tool.

## consequences

Documentation names `stratarc` and mentions `starc` once as the short form. One unverified risk remains: a desktop writing application named `starc` exists on GitHub, and it was not checked whether it installs a binary of that name. Windows system commands, Ubuntu, Fedora, Arch and Nix package names, and trademark registers were also not checked. If a collision is found, removing the alias is one line in `pyproject.toml` and does not affect `stratarc`.
