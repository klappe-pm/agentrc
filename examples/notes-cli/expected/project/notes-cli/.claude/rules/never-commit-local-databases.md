# never-commit-local-databases

## binding

Never stage or commit a local database or the files SQLite keeps beside it: `*.db`, `*.sqlite`, `*.sqlite3` and their `-journal`, `-wal` and `-shm` companions. If one is already tracked, stop and report it instead of rewriting history.

Full rule: `$HOME/agentrc-source/rules/never-commit-local-databases.md`.
