# decisions

This folder holds stratarc's decision records: one page per choice that shaped the code, written when the choice was made, so a later contributor can see what was decided, what was considered instead and why. A record is never rewritten once accepted; a reversal is a new record that names the one it supersedes. Read the records that touch a part of the code before proposing to change it.

## format

Each record is a file named `YYYY-MM-DD-<short-kebab-title>.md` with these sections: `## context` (the situation and the forces at play), `## decision` (what was chosen, in one paragraph), `## alternatives` (what else was considered and why it lost), and `## consequences` (what follows, good and bad). Add the record to the list below when it is accepted.

## records

Every record below is dated 2026-10-09 and was backfilled in one pass after the choices were made, so each carries the alternatives that were weighed then. The alternatives written from the code and the brief rather than from the maintainer's own words are marked for review in their pages. The terminal interface library record was proposed and open; the Textual record supersedes it, and the record stays as written. The two open points in accepted records, the `_modes` syntax in explicit list modes and whether the XDG path should be the primary home, are settled by later records (list modes use a modes table, the home is `.stratarc`); the earlier records are not edited.

Naming and scope:

- [rename to stratarc](2026-10-09-rename-to-stratarc.md)
- [the stratarc name passed its checks with two caveats](2026-10-10-the-stratarc-name-passed-its-checks-with-two-caveats.md) (confirms the rename; the trademark registers it left open were checked where a search could be run)
- [public package ships code only and no private integrations](2026-10-09-public-package-ships-code-only-and-no-private-integrations.md)
- [one engine name](2026-10-09-one-engine-name.md)

Source root and deployment:

- [guards overlay package data](2026-10-09-guards-overlay-package-data.md)
- [private extensions live in the source root](2026-10-09-private-extensions-live-in-the-source-root.md)
- [no deploy branch guard unless environments declared](2026-10-09-no-deploy-branch-guard-unless-environments-declared.md)
- [projects root from config](2026-10-09-projects-root-from-config.md)
- [diff is a read-only full render](2026-10-09-diff-is-a-read-only-full-render.md)
- [verify defines drift as a dry run](2026-10-09-verify-defines-drift-as-a-dry-run.md)
- [sync verifies and rolls back on drift](2026-10-09-sync-verifies-and-rolls-back-on-drift.md)
- [sync records changes at its write points](2026-10-09-sync-records-changes-at-its-write-points.md)
- [private checks gate deploy only when the source root opts in](2026-10-09-private-checks-gate-deploy-only-when-the-source-root-opts-in.md)
- [the active source root is resolved in one place](2026-10-09-the-active-source-root-is-resolved-in-one-place.md)
- [the source root private hook files are staged from the source only](2026-10-09-the-source-root-private-hook-files-are-staged-from-the-source-only.md) (narrows the staging exclusion in the two records on guards and private extensions)
- [rendered workflows must run files the sync delivers](2026-10-09-rendered-workflows-must-run-files-the-sync-delivers.md)

Settings, registries and state:

- [layer order and provenance](2026-10-09-layer-order-and-provenance.md)
- [explicit list modes in layers](2026-10-09-explicit-list-modes-in-layers.md)
- [list modes use a modes table](2026-10-09-list-modes-use-a-modes-table.md), settles the open syntax point above
- [list overrides never default their mode](2026-10-10-list-overrides-never-default-their-mode.md), settles the open default point in the two list mode records above
- [home layout and safe writes](2026-10-09-home-layout-and-safe-writes.md)
- [source root files keep their mode and the home stays private](2026-10-10-source-root-files-keep-their-mode-and-the-home-stays-private.md), refines the file modes in the home layout record
- [the home is dot stratarc with an environment override](2026-10-09-the-home-is-dot-stratarc-with-an-environment-override.md), settles the open home point above
- [adapter registry and support ranges](2026-10-09-adapter-registry-and-support-ranges.md)
- [version detection runs in an isolated home](2026-10-09-version-detection-runs-in-an-isolated-home.md)
- [providers hold secret references only](2026-10-09-providers-hold-secret-references-only.md)
- [optional change log](2026-10-09-optional-change-log.md)

Command line and interfaces:

- [resource verb command grammar](2026-10-09-resource-verb-command-grammar.md)
- [exit code map](2026-10-09-exit-code-map.md)
- [message catalog with ids](2026-10-09-message-catalog-with-ids.md)
- [read-only local API](2026-10-09-read-only-local-api.md)
- [local API safeguards](2026-10-09-local-api-safeguards.md)
- [terminal UI library is open](2026-10-09-terminal-ui-library-is-open.md), superseded by the Textual record below
- [Textual is an optional extra for the terminal interface](2026-10-09-textual-is-an-optional-extra-for-the-terminal-interface.md)
- [starc is the short command alias](2026-10-09-starc-is-the-short-command-alias.md), superseded by the drop record below
- [drop the starc alias](2026-10-09-drop-the-starc-alias.md), supersedes the starc alias record: the command is `stratarc` only
- [resource verbs fill the grammar gaps](2026-10-09-resource-verbs-fill-the-grammar-gaps.md)
- [JSON accounts support set and unset](2026-10-10-json-accounts-support-set-and-unset.md), extends the account verbs in the gaps record
- [explain follows the dispatch relay and flags are a layer](2026-10-09-explain-follows-the-dispatch-relay-and-flags-are-a-layer.md)
- [the interface validates edits and keeps slow work off its thread](2026-10-09-the-interface-validates-edits-and-keeps-slow-work-off-its-thread.md)
- [CLI design is delivered in ordered slices](2026-10-09-cli-design-is-delivered-in-ordered-slices.md)
- [first run is a choice, not a scan](2026-10-09-first-run-is-a-choice-not-a-scan.md)

Docs and tests:

- [static docs site built from the repo](2026-10-09-static-docs-site-built-from-the-repo.md)
- [example output is captured, not written](2026-10-09-example-output-is-captured-not-written.md)
- [pytest is the only test runner](2026-10-09-pytest-is-the-only-test-runner.md)
- [shellcheck gates at warning severity](2026-10-10-shellcheck-gates-at-warning-severity.md)
- [new CLI tests carry mutation proofs](2026-10-09-new-cli-tests-carry-mutation-proofs.md)
