# troubleshooting

This guide collects known failure modes and their fixes, one problem per heading, so you can match the message you see and act on it. It will grow as the engine is extracted and as issues are reported. If your problem is not here, see [support](../../support.md) for where to ask.

## init-refuses-a-target-that-is-not-empty

`agentrc init` writes only into a directory that does not exist or holds no files, and says `is not empty; refusing to scaffold into it` otherwise. Pick a new directory, or empty the one you named. It never merges a template into existing files.

## a-command-exits-with-status-2

`sync`, `check`, `diff`, `prune` and `reconcile` print `not yet extracted` and exit 2 until their engine lands. This is expected in the scaffold release; nothing is misconfigured. Track their arrival in the [changelog](../../changelog.md).

## planned-sections

Once the engine is extracted this page will also cover a runtime directory agentrc cannot write, drift reported by `agentrc check`, a guard hook that blocks an action, and files left behind that `agentrc prune` removes.
