---
description: Summarize the uncommitted changes in the current repository in a few plain sentences.
argument-hint: "[path, e.g. \"src/\"]"
allowed-tools: Bash, Read
---

# summarize-diff

Summarize the working tree changes so the user can review them quickly.

## steps

1. Run `git status --short` and `git diff` (limited to the path argument when one is given).
2. Describe what changed and why it appears to have changed, grouped by file or feature.
3. Flag anything that looks unintended, such as debug output or unrelated edits.

## output

- One short paragraph per logical change.
- A list of files that look unintended, or "none".
