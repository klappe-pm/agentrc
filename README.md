# stratarc

stratarc compiles one configuration source root (instructions, rules, hooks, skills, commands, agents and permissions) into the native configuration of each agent runtime you use, then deploys it to that runtime's directory. You edit one tree; stratarc writes five formats. It ships as a Python package with shell hooks and needs only the Python 3.11 standard library at runtime.

Compatibility targets: Claude Code, Codex, Gemini CLI, Cursor and OpenCode.

## status

The engine is being extracted from a private repository in stages. This release is the scaffold: `stratarc init` works and scaffolds a source root from the bundled template. `stratarc sync`, `check`, `diff`, `prune` and `reconcile` are not yet extracted; they exit with status 2 until they land. Track progress in [changelog.md](changelog.md).

## quickstart

Install the command. Until the first release reaches PyPI, install from the repository.

```bash
pipx install git+https://github.com/klappe-pm/stratarc
```

Once released:

```bash
pipx install stratarc
```

Scaffold a source root:

```bash
stratarc init ~/stratarc-source
```

Edit `~/stratarc-source/stratarc.toml` to name the runtimes you use, then edit the rules, skills and hooks under the same directory.

Deploy to every enabled runtime once `sync` is extracted:

```bash
cd ~/stratarc-source && stratarc sync
```

## documentation

Two trees, one per audience, each arranged by [Diátaxis](https://diataxis.fr/) quadrant: tutorials, how-to guides, reference and explanation.

- I use stratarc, where do I start: [getting started](docs/tutorials/getting-started.md), then the rest of [docs](docs/README.md).
- I change stratarc, where do I start: [first contribution](developer-docs/tutorials/first-contribution.md), then the rest of [developer-docs](developer-docs/README.md).

## support-and-contributing

[support.md](support.md) says where to ask for help and where to file an issue. [contributing.md](contributing.md) holds the contribution guidelines. Report vulnerabilities privately as described in [security.md](security.md).

## license

MIT. See [LICENSE](LICENSE).
