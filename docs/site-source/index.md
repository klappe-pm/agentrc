# stratarc

stratarc keeps one agent configuration source and renders it into every agent runtime you use: Claude Code, Codex, Gemini CLI, Cursor and OpenCode. Settings inherit through layers, and every value can be traced to the file and line that set it.

## see-where-a-value-came-from

This is the output of a real run against the bundled `notes-cli` example. The key `permissions.defaultMode` is set in the base file and again in the project's own file; `config explain` prints each layer that touched it and which one decided the result.

{{capture:config-explain}}

Each line names the layer, the file and line, and the value it set. The last line names the layer that won. Nothing is sent anywhere, and nothing is written.

## install

stratarc needs Python 3.11 or newer. Until the first release reaches PyPI, install from the repository:

```bash
pipx install git+https://github.com/klappe-pm/stratarc
```

Check that it is on your path:

```bash
stratarc --version
```

Other install methods are in [install](../how-to-guides/install.md).

## your-first-source-root

A source root is a normal directory you can commit. These steps scaffold one, check it and preview what a deployment would change. Nothing is written to a runtime directory until you run `stratarc sync` without `--dry-run`.

1. Scaffold a source root into a new directory:

   ```bash
   stratarc init ~/stratarc-source
   ```

2. Check it against the schemas and the engine's rules:

   ```bash
   stratarc validate --root ~/stratarc-source
   ```

3. Choose the runtimes you use in `stratarc.toml`, then preview the deployment:

   ```bash
   stratarc sync --dry-run --root ~/stratarc-source
   ```

The full lesson, including adding a first rule and skill, is [getting started](../tutorials/getting-started.md).

## where-to-go-next

- [command reference](../reference/generated/commands.md), generated from the command line itself
- [error catalog](../reference/generated/errors.md), one entry per message id
- [configuration](../reference/configuration.md), every key in `stratarc.toml`
- [security model](../explanation/security-model.md), what stratarc protects against
- [first run](../../load.html), a one-screen page with the three first steps
