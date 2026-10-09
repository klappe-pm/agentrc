# expected

The rendered runtime output a sync of `../source` produces, one directory per runtime plus `project/notes-cli/` for the managed project. The source root lives at `~/stratarc-source` and the checkout at `~/projects/active/notes-cli`. `tests/test_examples.py` runs `stratarc sync` on a copy of `../source` in a temporary home and requires every file here to match the output byte for byte.

`<home>` in `codex/config.toml` stands for the home directory the sync ran under, which the engine writes into the project trust table.

A sync also writes files that are not kept here: the guard libraries under `hooks/lib/`, the project's `.claude/hooks/` and `.github/` templates, the project's `.docs/` snapshot of the control plane, the OpenCode plugin `plugins/stratarc-hooks.ts`, and the per-target `.stratarc-deploy.json` and `stratarc-delivered.json` stamps. They come from the engine's package data or record times and absolute paths, and the test lists them in `ENGINE_OWNED`.
