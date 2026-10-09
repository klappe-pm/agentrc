# 2026-10-09-message-catalog-with-ids

## status

accepted, 2026-10-09

## context

Error wording scattered through the commands drifts: the same failure is described three ways, recovery advice is missing, and a document that quotes a message goes stale when the code changes. Users filing an issue also need a short stable handle for the message they saw.

## decision

Every user-facing error has an id of the form `msg-NNNN`, one sentence that states the problem and one sentence that states the recovery, and the exit status it ends the command with. The catalog lives in [messages.py](../../../stratarc/messages.py), so wording is edited in one place and the documentation links to a message by its id instead of copying its text. A caller raises a message by id and fills its placeholders. Tracebacks appear only with `--debug`.

## alternatives

- Inline strings at each raise site. Cheapest to write, but wording cannot be reviewed as a set and ids cannot exist.
- Message text in a data file. It would separate wording from code, but placeholders and exit statuses would then need their own validation and the catalog loses type checking.
- Always print tracebacks. Useful to maintainers, hostile to users who only need to know what to do next.

## consequences

- Wording is reviewed and changed in one file, and a test can assert that every id has both sentences.
- Documentation survives a rewording because it cites the id.
- Adding an error means adding a catalog entry first, which is a small tax on every new failure path.
- Without `--debug` a maintainer sees less detail on an unexpected crash and must ask the reporter to rerun with it.
