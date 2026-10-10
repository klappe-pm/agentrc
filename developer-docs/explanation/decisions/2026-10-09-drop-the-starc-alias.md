# 2026-10-09-drop-the-starc-alias

## status

accepted, 2026-10-09. Supersedes [starc is the short command alias](2026-10-09-starc-is-the-short-command-alias.md).

## context

The alias record named one unverified risk: a desktop writing application called Story Architect, shortened to STARC, with a few hundred stars on GitHub. A follow-up check read its packaging and the packaging other projects built around it. Its Linux desktop file launches `starc`, so the AppImage contains an executable of that name. The nixpkgs package sets `starc` as its main program, and an AUR package installs `/usr/bin/starc`. A Windows installer places `starcapp.exe` and no `starc` on the path. The name is free as a package on PyPI, npm and crates.io. Fedora, Alpine, macOS app bundle contents, PyPI entry points and the trademark registers could not be checked, and a search found an old cancelled mark for a semiconductor consortium and a pending aircraft testing application with the same name.

## decision

The `starc` console script is removed. The command is `stratarc` only. A user who wants a shorter name can add a shell alias or a symlink, and the documentation says so in one line. No replacement alias is shipped until one has passed the same checks on every platform the package supports.

## alternatives

Keeping `starc` was rejected because two Linux package ecosystems already put a different program of that name on the path, and a command that silently shadows or is shadowed by another program is worse than a longer name. Choosing another short name without the full checks was rejected because the first choice showed how a name that is free on every language registry can still collide as a command. Shipping the alias only on some platforms was rejected as inconsistent documentation for a tool whose commands should read the same everywhere.

## consequences

The command name is longer to type and has no collision found. The change is one line in `pyproject.toml`, one test and a few lines of documentation. A future alias needs a new record that names the checks it passed.
