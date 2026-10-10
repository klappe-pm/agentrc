# 2026-10-10-rollback-places-every-adapter-action-line

## status

accepted, 2026-10-10. Refines [sync verifies and rolls back on drift](2026-10-09-sync-verifies-and-rolls-back-on-drift.md), which stays as written.

## context

`stratarc sync --rollback-on-drift` backed up only the files named by the adapters' dry-run action lines, and it found them by matching absolute paths under the runtime target. The claude adapter emits absolute paths. The codex, gemini and opencode adapters emit relative lines such as `copy commands/commit.md -> prompts/commit.md`, `translate commands/x.md`, `command x.md -> x.toml` and `render permissions.json into config.toml`, and some lines name no file at all (`map PreToolUse -> BeforeTool`, `trust 3 repositories in config.toml`). `verify._drifted_paths` returned those lines as unplaced and `sync._back_up_planned` discarded them. After a rollback those files stayed at the new content and files the run created were not removed. A study over redirected homes found it.

## decision

`_drifted_paths` takes the set of files the adapter's render owns. A relative token counts as a file when it is an owned path or the tail of one. Without that set (the render failed), it places only the right side of `a -> b` and only when that side contains a slash, because a bare name could sit under any subdirectory.

Before a write, `_back_up_planned` renders the adapter into a throwaway directory and backs up every named path plus the whole owned set. A line that names no file is therefore covered by the set the adapter manages, not ignored. When the render fails and a line is still unplaced, the run records it, and the rollback refuses with msg-1119 (exit 5) instead of reporting a restore it cannot perform.

Tests: every runtime's dry-run lines are placeable against the owned set (all five runtimes), a full-tree snapshot of every runtime target is byte identical after a rolled-back run, and an unplaceable line refuses the rollback.

## alternatives

Parsing each adapter's line format was rejected because the formats differ per adapter, several are not file paths, and any new adapter would break it silently. Treating the right side of every `->` as a target path was rejected because `map X -> Y` and `command x.md -> x.toml` would name files that do not exist. Snapshotting each whole runtime directory was rejected because the targets hold the operator's own files, and the backups would copy all of them on every run. Ignoring unplaced lines was the defect.

## consequences

A rollback backs up every file the render owns, not only those that change, so it copies more files than before for a run that rolls back or not. Rendering twice per runtime (once for the owned set, once to deploy) adds time to a rollback run only. Directories that a run creates (`mkdir hooks/...`) are not removed by the rollback, which restores and deletes files only, so an empty directory can remain. A file an adapter deletes outside its render and outside its dry-run lines is still not restored.
