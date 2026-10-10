# 2026-10-09-the-interface-validates-edits-and-keeps-slow-work-off-its-thread

## status

accepted, 2026-10-09. Builds on [Textual is an optional extra for the terminal interface](2026-10-09-textual-is-an-optional-extra-for-the-terminal-interface.md).

## context

The first version of the interface handed a file to the editor and rebuilt the tree without checking what came back, so a typo in `stratarc.toml` or a permissions file could be saved by a screen that promises to refuse an invalid file. It also ran the sync preview and verify on the thread that reads keys, so the screen froze for as long as they took and `q` did nothing until they finished.

## decision

After the editor closes, the interface validates the file with `resources_cmd.problems_in`, the validator `stratarc config edit` uses, with the role it gives the root's `stratarc.toml` and `permissions.json`. A valid file is kept and the tree is rebuilt. An invalid file is listed in the pane and the original is put back with `home_layout.safe_write`. Before the editor opens, the interface writes the unchanged file through `safe_write` once so the home's `backups/` holds the original, and the rejected text lands there when the original is restored.

The sync preview and verify run through `run_worker(thread=True)`. The key handler only starts the job, shows a busy line in the help row and returns. The result arrives in `on_worker_state_changed`. `q` cancels the job and exits. A second start while a job runs is refused. The validation and the backup logic live in `stratarc.ui.model`, which still imports no terminal library.

## alternatives

Editing a temporary copy and saving it only when valid, as `stratarc config edit` does, was rejected because the editor then no longer opens the owning file itself, and the interface's tests and users rely on that path. Validating but leaving an invalid file in place for the next edit was rejected because it breaks the rule that no command leaves an invalid file behind. An asyncio task instead of a thread was rejected because the sync and verify libraries are blocking and write to the standard streams. A modal progress screen was rejected because it would block the other keys.

## consequences

Every `e` writes one extra backup of an unchanged file, which the home's cleaning keeps within its retention. A thread worker cannot be interrupted, so `q` abandons a running job instead of stopping it, and the process leaves once the job returns. The sync preview captures standard output for the duration of the job, so a second capture started by another thread would collide; the interface starts only one job at a time for that reason. The tests prove the interface answers keys during a slow stub job.
