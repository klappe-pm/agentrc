---
name: write-changelog-entry
description: "Draft a changelog entry from the current diff. Use when a change is ready to commit, when asked to update the changelog, or when asked what to say about a change under unreleased."
---

# write-changelog-entry

## steps

1. Run `git diff --stat` and `git diff` to see what changed. If nothing is staged, read the working tree diff.
2. Decide which heading the change belongs under: `added`, `changed`, `fixed` or `removed`.
3. Write one line in the past tense that a user of the tool would understand, without file names or internal function names.
4. Add the line under `unreleased` in `changelog.md`, creating that section and heading if they are missing.

## output

The exact line added to `changelog.md` and the heading it went under. If the diff holds several unrelated changes, write one line per change and say that the commit may want splitting.
