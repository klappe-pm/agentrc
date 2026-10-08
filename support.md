# support

This page says where to get help with agentrc and where to file what. agentrc is maintained in the open on GitHub and has no paid or private support channel; every question and report goes through the repository, where it is visible to the next person with the same problem.

## before-you-ask

- Match your symptom against [troubleshooting](docs/how-to-guides/troubleshooting.md).
- Check the [changelog](changelog.md): in the scaffold release, `sync`, `check`, `diff`, `prune` and `reconcile` exit with status 2 by design.
- Search the existing [issues](https://github.com/klappe-pm/agentrc/issues), open and closed.

## where-to-file

| what you have | where it goes |
| --- | --- |
| a bug: agentrc did something other than what the documentation says | an [issue](https://github.com/klappe-pm/agentrc/issues/new) with the command you ran, what you expected, what happened and the output of `agentrc --version` |
| a question about using agentrc | an [issue](https://github.com/klappe-pm/agentrc/issues/new) with what you are trying to do and what you tried |
| a request for a feature or a new runtime | an [issue](https://github.com/klappe-pm/agentrc/issues/new) describing the need before any code; see [contributing](contributing.md) |
| a suspected security vulnerability | a private report as described in [security.md](security.md); never a public issue |
| a mistake or gap in the documentation | an issue, or a pull request against the page; [documentation guide](docs/documentation-guide/README.md) says where a page goes |

## what-to-include

Paste command output as text inside a code fence rather than as a screenshot, and remove anything shaped like a credential before posting. If the problem involves a source root, say which files are in it; do not attach a `permissions.json` or `components.json` that names private services.

## response-times

The project is maintained by one person alongside other work. Issues are read within a few days; fixes land as time allows and in order of impact.
