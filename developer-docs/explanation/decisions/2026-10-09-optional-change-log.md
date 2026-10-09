# 2026-10-09-optional-change-log

## status

accepted, 2026-10-09

## context

Operators want to ask what changed, when and why, and to verify a change later. Recording that costs disk, can capture sensitive text and can fail (a locked or newer database). The tool must never become unusable because its log is. See [changelog.py](../../../stratarc/changelog.py) and the change-log section of [cli-design.md](../cli-design.md).

## decision

A human-readable log is always on. The SQLite database and its JSONL mirror are off by default and turn on with `STRATARC_LOG`, `logging = true` in `config.toml`, or the `enable` command. The log stores digests, never content, and every text value passes the packaged token-shaped detector before it is written; if the detector cannot run, the value is replaced and nothing unredacted is written (it fails closed). A locked or newer database degrades to the human log with one message and never fails the command. Recording is a no-op while disabled and never fails a sync.

## alternatives

- Always-on database. Richer history for everyone, but a write path, disk growth and a failure mode for users who never asked for them.
- A separate logging service. It would add a process to install, secure and keep running for what is a local file concern.
- Store the before and after content. It makes explanations easy but turns the log into a copy of the operator's files and a leak risk.

## consequences

- A fresh install writes only a plain text line per event, so the cost of the feature is opt in.
- Explaining a change relies on digests and the verify report, not on stored content, so it can say that something differed but not show the old bytes.
- A failing log can never block a deploy, at the price that an event may exist only in the human log and the mirror.
