# never-commit-local-databases

## binding

Never stage or commit a local database or the files SQLite keeps beside it: `*.db`, `*.sqlite`, `*.sqlite3` and their `-journal`, `-wal` and `-shm` companions. If one is already tracked, stop and report it instead of rewriting history.

## rationale

A database holds whatever the user typed into it, including text no one meant to publish. It also changes on every run, so it never produces a useful diff. Keeping databases out of version control is cheap; removing one from history later is not.

## exceptions

A small, synthetic database that a test fixture needs may be committed once the user says so for that file in the current session.
