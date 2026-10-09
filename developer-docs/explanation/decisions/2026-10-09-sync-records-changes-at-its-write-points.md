# 2026-10-09-sync-records-changes-at-its-write-points

## status

accepted, 2026-10-09

## context

The [change log](2026-10-09-optional-change-log.md) needs events to hold, and the sync engine is where nearly every write happens. The question was where to call the log, and what a logging failure may do to a sync.

## decision

Sync records an event at each point where it changes something: the reconcile and project steps (one event per changed project), the plugin ingest declare and opt-in, the derived-file regeneration, each runtime after its adapter ran (status propagated, with a digest of the result and the shared cause id of the run), the permission sweep, and the deploy record. A refusal at any of those points records a failed event. Recording does nothing when logging is off, including the human log, and a failure to record or a locked database never stops a sync; the call degrades to the human log and prints one message.

## alternatives

Recording from each adapter was rejected because adapters render and do not decide what counts as a change. Recording once at the end of a run was rejected because it loses which layer and which project each change reached. Failing the sync on a logging error was rejected because a diagnostic aid must not become a way to break a deploy.

## consequences

The log reads as a causal chain from one source change to each runtime and project it reached. Any new write step in sync must add its own recording call, which a test over the call sites guards. The known cost is a few more lines at every write point.
