# COMMENTS-PROCESS.md

This is the append-only process log for extracting components into this repository. Each entry records friction in the extraction flow itself, in the form `- YYYY-MM-DD: <one or two lines>`. It continues the log kept in the operator's private source repository.

## entries

- 2026-10-08: Runtime assets moved under agentrc/data/ instead of the top-level hooks/, git-hooks/, schema/ and templates/ the design record lists, so a checkout and an installed wheel resolve them through importlib.resources the same way, with no copy kept in sync by a test.
- 2026-10-08: An end-to-end check of the commit-msg backstop cannot run through an agent's shell, because the runtime guard denies the test commit's trailer before the hook sees it; the backstop is checked by running the strip script on a message file, and the hook's wiring by a clean commit.
