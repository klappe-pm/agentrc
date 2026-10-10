# 2026-10-09-new-cli-tests-carry-mutation-proofs

## status

accepted, 2026-10-09

## context

The tests that shipped with the command line wiring (forwarding, exit-code mapping, the JSON envelope, global-flag scoping, the message catalog and the doctor permissions table) passed, but none had been shown to fail when the behavior they guard breaks. Twelve deliberate mutations of the source were applied one at a time. Six survived: dropping a catalog id that no code references, swapping the exit statuses of two catalog messages, mapping a module code to the wrong message id, not restoring an environment variable that held a value before a global flag overrode it, removing one permissions row, and letting a registered source win over `STRATARC_SOURCE` (only `tests/test_paths.py` noticed that one). A test that stays green under such a change documents nothing and guards nothing.

## decision

A test added with the command line wiring is accepted only with a revert proof: the behavior it covers is broken in a scratch checkout, the test is seen to fail, and the source is restored. Where a mutation survives, the missing test is written first. The message catalog is pinned as tables in `tests/test_messages.py` (the exact id set, the exit status of every id, the exact module code to id map), the permissions table is pinned as an ordered list of its command labels, and the environment tests assert both the value set during a command and the value restored after it.

## alternatives

- Rely on line coverage. The surviving mutations were all on covered lines, so coverage cannot show them.
- Run a mutation testing tool in CI. It would find more, but it is slow, noisy on string-heavy modules such as the catalog, and needs a baseline of accepted survivors to maintain.
- Pin only the highest-traffic messages. Cheaper, but the survivors were in the long tail, which is where a wrong id or status goes unnoticed.

## consequences

- Changing a message, its exit status or a code mapping now edits a literal table in a test as well as the catalog, which makes the change visible in review.
- Adding a permissions row or a message means updating the pinned list, an accepted cost.
- Pinning showed that `messages.from_code` raises `KeyError` for `backup-missing` and `settings-malformed`, because their messages use placeholders other than `{detail}`. No module raises those codes yet, so the pin skips the call for both. Whoever first raises one must supply the extra values.
