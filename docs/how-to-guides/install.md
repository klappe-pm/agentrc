# install

This guide covers installing stratarc for someone who already knows they want it: the supported install methods, the Python versions it runs on, and how to upgrade and uninstall. It assumes nothing about your runtimes. If you have never used stratarc, start with [getting started](../tutorials/getting-started.md) instead, which walks the install as one step among several.

Once the engine is extracted this page will also describe installing the git hooks and verifying that each runtime's target directory is writable.

## requirements

Python 3.11 or newer. stratarc has no runtime dependencies beyond the standard library.

## install-from-the-repository

Until the first release is published, install straight from GitHub:

```bash
pipx install git+https://github.com/klappe-pm/stratarc
```

## install-from-pypi

Once released:

```bash
pipx install stratarc
```

## install-for-development

Contributors install an editable copy with the test dependencies. See [first contribution](../../developer-docs/tutorials/first-contribution.md) for the full setup.

```bash
pip install -e '.[test]'
```

## a-shorter-command

The command is `stratarc` only. To type less, add a shell alias such as `alias sa=stratarc` to your shell profile, choosing a name that is free on your machine.

## upgrade

```bash
pipx upgrade stratarc
```

For a repository install, run `pipx reinstall stratarc` to pick up the latest commit.

## uninstall

```bash
pipx uninstall stratarc
```

Uninstalling removes the command only. Your source root and anything already deployed into runtime directories stay where they are.
