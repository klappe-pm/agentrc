# install

This guide covers installing agentrc for someone who already knows they want it: the supported install methods, the Python versions it runs on, and how to upgrade and uninstall. It assumes nothing about your runtimes. If you have never used agentrc, start with [getting started](../tutorials/getting-started.md) instead, which walks the install as one step among several.

Once the engine is extracted this page will also describe installing the git hooks and verifying that each runtime's target directory is writable.

## requirements

Python 3.11 or newer. agentrc has no runtime dependencies beyond the standard library.

## install-from-the-repository

Until the first release is published, install straight from GitHub:

```bash
pipx install git+https://github.com/klappe-pm/agentrc
```

## install-from-pypi

Once released:

```bash
pipx install agentrc
```

## install-for-development

Contributors install an editable copy with the test dependencies. See [first contribution](../../developer-docs/tutorials/first-contribution.md) for the full setup.

```bash
pip install -e '.[test]'
```

## upgrade

```bash
pipx upgrade agentrc
```

For a repository install, run `pipx reinstall agentrc` to pick up the latest commit.

## uninstall

```bash
pipx uninstall agentrc
```

Uninstalling removes the command only. Your source root and anything already deployed into runtime directories stay where they are.
