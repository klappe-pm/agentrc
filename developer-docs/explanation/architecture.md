# architecture

This page explains how stratarc turns one source root into a configuration for each enabled runtime, for a contributor who needs the shape of the pipeline before changing a stage of it. It describes the stages in order: reading and validating the source, staging the output for each runtime through its adapter, comparing staged output with what is deployed, and writing only what changed. It will also cover the control plane, the reconciler and per-project overlays once the engine is extracted. The [source layout](../reference/source-layout.md) describes the input; the [runtimes](../../docs/reference/runtimes/README.md) pages describe the outputs.

## the-shape-today

The scaffold release ships the two ends of the pipeline and none of the middle. `stratarc init` writes the input: it copies the bundled template, resolved through `importlib.resources` as [package data](../reference/package-data.md) describes, into a new source root. The detectors and git hooks under `stratarc/data/` are the guards that the deployed output will carry. `sync`, `check`, `diff`, `prune` and `reconcile` are registered as subcommands and exit 2 until their stages land.

## the-stages

1. Read and validate. Load `stratarc.toml`, then each source kind, and validate the JSON files against the bundled schemas before anything is built.
2. Stage. For each enabled runtime, an adapter maps every source kind to the runtime's native files in a staging area, so the whole output exists before any of it is deployed.
3. Compare. Diff the staged output against what the runtime directory holds now; `diff` stops here and prints the result, `check` stops here and reports drift.
4. Write. `sync` copies only the files that changed into the runtime target, and `prune` removes files stratarc wrote earlier that no longer have a source.

The reconciler and the control plane sit beside the pipeline: `reconcile` refreshes `control-plane.md`, the generated inventory that records which source items are opted in for which project, and the per-project overlays under `projects-root/` are applied when a managed project is deployed.
