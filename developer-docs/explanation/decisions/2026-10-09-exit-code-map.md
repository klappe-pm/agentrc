# 2026-10-09-exit-code-map

## status

accepted, 2026-10-09

## context

Scripts and CI jobs that call stratarc need to tell a bad argument from a refused write, a conflict, a missing prerequisite and a deployed file that no longer matches its source. Two sibling tools already use exit codes for part of this, and they do not agree with each other. A script that wraps all three should not need a separate table for each. The command line and the local API also need to describe a failure the same way, so a caller can reuse one parser.

## decision

stratarc exits 0 for ok, 1 for failure, 2 for invalid input, 3 for denied, 4 for conflict, 5 for unavailable, 6 for drift or a failed verification and 130 for an interrupted run. The map is a superset of the conventions of the two sibling tools, so a script sees one table. The command line (with `--json`) and the API share one JSON envelope, `{ok, data, error}`, where `error` carries `code`, `message`, `param` and `hint`. The codes live in [messages.py](../../../stratarc/messages.py) and are mapped to names in [cli.py](../../../stratarc/cli.py).

## alternatives

- Exit 1 for every failure. Simple, but a script cannot tell a typo from a drifted deployment without parsing text.
- Adopt one sibling tool's table unchanged. It lacks a code for drift and for a newer-schema refusal, so those would collapse into a generic failure.
- A different envelope for the API than for `--json`. It would force every consumer to maintain two parsers for the same facts.

## consequences

- A script can branch on the status alone: 6 means rerun a sync, 5 means upgrade or wait, 2 means fix the call.
- A new failure class needs a new code only if callers must react differently to it; otherwise it reuses the nearest existing code.
- The numbers are part of the public contract and cannot be reassigned without a superseding record.
