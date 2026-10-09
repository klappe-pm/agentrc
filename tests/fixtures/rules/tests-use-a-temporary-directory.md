# tests-use-a-temporary-directory

## binding

A test that reads or writes notes runs against a temporary directory and never against the real notes directory. Point `NOTES_HOME` at the test's temporary path before the code under test runs.

## rationale

The notes directory is the user's data. A test that touches it can lose or corrupt real notes, and it makes the suite depend on whatever happens to be on the machine.
