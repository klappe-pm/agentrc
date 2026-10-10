# 2026-10-09-the-source-root-private-hook-files-are-staged-from-the-source-only

## status

Accepted, 2026-10-09, by the maintainer.

## context

[guards-overlay-package-data](2026-10-09-guards-overlay-package-data.md) states that a `private/` directory under either `hooks/lib/` is never staged, and [private-extensions-live-in-the-source-root](2026-10-09-private-extensions-live-in-the-source-root.md) places the environment-dump patterns at `hooks/lib/private/env-dump-patterns.json`. Together they left the user-level guards deployed to each runtime unable to read those patterns: the guard that runs in the runtime directory looks for `lib/private/env-dump-patterns.json` beside itself, and staging never delivered it. A comparison of the earlier private engine with this one over redirected homes showed the deployed guard deciding without the operator's patterns.

## decision

This record narrows the staging exclusion stated in the two records above, which are left as written. Staging copies the source root's own `hooks/lib/private/` into the stage's `hooks/lib/private/`, and only when the stage is built for the global column, the column the user-level runtimes are mirrored from. Package data still never ships a `private/` directory and staging still never reads one from it. A project column does not receive the files through staging: project checkouts get the pattern file only through the carried-guard logic in [projects.py](../../../stratarc/projects.py), which already carries it when the source root holds it. A stage with no enabled hooks has no `hooks/` directory, so a runtime that carries no hooks receives nothing. The adapters mirror the stage's hooks directory and delete what the stage no longer holds, so removing a private source file removes it from every runtime on the next sync, and a stage test pins this.

## alternatives

- Keep the exclusion. Rejected: the deployed guards run with generic patterns only, which defeats the extension point.
- Stage `private/` for every column. Rejected: a project column's hooks land in a checkout that may be tracked or public.
- Stage `private/` from package data as well. Rejected: package data must never hold or ship private files.
- Copy the files from a separate delivery step. Rejected: the mirror already lists and prunes everything the stage holds, so a second step would duplicate the manifest.

## consequences

- The deployed user-level guards read the operator's private patterns, and a source root without `hooks/lib/private/` behaves as before.
- Private files sit in the runtime directories of the machine that syncs, which are generated, untracked output.
- The exclusion in the two earlier records is now accurate only for package data and for project columns.
