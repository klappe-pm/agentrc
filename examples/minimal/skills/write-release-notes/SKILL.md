---
name: write-release-notes
description: "Draft release notes from the commits since the last tag. Use when preparing a release, tagging a version, or when asked what changed since the previous release."
---

# write-release-notes

## steps

1. Find the most recent tag with `git describe --tags --abbrev=0`.
2. List the commits since that tag with `git log <tag>..HEAD --oneline`.
3. Group the changes under `added`, `changed` and `fixed`, one line per user-visible change, and drop internal refactors and chores.
4. Write the notes as a Markdown list under a heading naming the new version.

## output

A short Markdown section ready to paste into the changelog. Report any commit whose effect on users is unclear instead of guessing.
