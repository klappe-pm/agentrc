# architecture

This page will describe how agentrc turns one source root into five runtime configurations: reading and validating the source, staging the output for each runtime through its adapter, comparing staged output with what is deployed, and writing only what changed. It will also cover the control plane, the reconciler and per-project overlays once the engine is extracted.
