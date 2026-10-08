# agentrc

agentrc compiles one configuration source root (instructions, rules, hooks, skills, commands, agents and permissions) into the native configuration of each agent runtime you use, then deploys it to that runtime's directory. You edit one tree; agentrc writes five formats. It ships as a Python package with shell hooks and needs only the Python 3.11 standard library at runtime.

Compatibility targets: Claude Code, Codex, Gemini CLI, Cursor and OpenCode.

## status

The engine is being extracted from a private repository in stages. This release is the scaffold: `agentrc init` works and scaffolds a source root from the bundled template. `agentrc sync`, `check`, `diff`, `prune` and `reconcile` are not yet extracted; they exit with status 2 until they land. Track progress in [changelog.md](changelog.md).

## quickstart

Install the command. Until the first release reaches PyPI, install from the repository.

```bash
pipx install git+https://github.com/klappe-pm/agentrc
```

Once released:

```bash
pipx install agentrc
```

Scaffold a source root:

```bash
agentrc init ~/agentrc-source
```

Edit `~/agentrc-source/agentrc.toml` to name the runtimes you use, then edit the rules, skills and hooks under the same directory.

Deploy to every enabled runtime once `sync` is extracted:

```bash
cd ~/agentrc-source && agentrc sync
```

## documentation

- [getting started](docs/getting-started.md)
- [install](docs/install.md)
- [migrating](docs/migrating.md)
- [configuration](docs/configuration.md)
- [cli reference](docs/cli-reference.md)
- [troubleshooting](docs/troubleshooting.md)
- [architecture](docs/architecture.md)
- [source layout](docs/source-layout.md)
- [adding a runtime](docs/adding-a-runtime.md)
- [security model](docs/security-model.md)
- [runtimes](docs/runtimes/README.md)

## contributing

See [contributing.md](contributing.md). Report vulnerabilities privately as described in [security.md](security.md).

## license

MIT. See [LICENSE](LICENSE).
