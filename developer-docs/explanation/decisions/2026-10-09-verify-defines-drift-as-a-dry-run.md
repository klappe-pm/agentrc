# 2026-10-09-verify-defines-drift-as-a-dry-run

## status

accepted, 2026-10-09

## context

After a sync, an operator needs to know whether what is deployed still matches what the source says. "Drift" can mean many things: a hand edit, a missing file, an extra file, a changed merge target. A vague definition produces false alarms, and a checker that writes to deployed files cannot be run safely from a script. See [verify.py](../../../stratarc/verify.py).

## decision

`verify` re-renders the affected runtimes into a temporary stage through the same staging and adapters a sync uses, and compares the result with the deployed files byte for byte. It records `verified` or `drift` per file, never writes a deployed file and exits 6 on drift. Drift means exactly what an adapter's dry run would change. A stray file in a directory the tool does not manage is a matter for pruning, not drift.

## alternatives

- Compare checksums recorded at the last sync. Cheap, but it misses a source change that was never deployed and trusts stale stamps.
- Treat any unexpected file as drift. Noisy: users keep their own files beside generated ones.
- Have verify repair what it finds. It would turn a read-only check into a write path with its own failure modes.

## consequences

- Verify and sync agree by construction, because both use the adapters; a file reported as drift is one a sync would change.
- It is safe in CI and in the background, since it writes only its report and temporary files.
- Unmanaged extra files go unreported by verify and need the prune path.
- For a file an adapter merges into, the recorded digest of the render is not what a sync would leave; the report notes this.
