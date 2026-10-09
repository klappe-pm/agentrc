# 2026-10-09-adapter-registry-and-support-ranges

## status

accepted, 2026-10-09

## context

Each adapter translates the source into one runtime's native files, and those runtimes change their formats. Deploying through an adapter that does not understand the installed runtime can write files the runtime rejects or misreads. The tool needs a way to say how far an adapter can be trusted and to retire one gracefully. See [registry.py](../../../stratarc/registry.py).

## decision

Every adapter carries a manifest with a `supports` version range. Its status is `ok`, `outdated`, `unsupported` or `unknown`. An `unsupported` adapter blocks a deploy with exit 5, and an `outdated` one (newer than the version it was tested against) warns once per run. Deprecating an adapter prompts once and records the answer; a noninteractive run prints the notice and continues without recording. Bundled ranges are permissive until test fixtures record the runtime versions each adapter was built against.

## alternatives

- No version checks. Simplest, but failures show up as broken runtime configuration.
- Block on `outdated` too. Safer, but it stops a deploy for a runtime that merely moved ahead of the tested version.
- Warn on every use. Noise teaches users to ignore it, so it is once per run.
- Narrow bundled ranges now. It would be a guess without recorded evidence.

## consequences

- A hard mismatch stops before any file is written, with a recovery sentence.
- Permissive bundled ranges mean the gate protects registered third-party manifests more than bundled ones until fixtures land.
- A deprecation answer recorded interactively spares scripts a prompt, and a script never hangs waiting for one.
