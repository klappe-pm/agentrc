# adding-a-runtime

This page will explain how to add support for another agent runtime: writing an adapter under `agentrc/adapters/`, mapping each source kind to the runtime's native format, registering the runtime in `agentrc.toml`, and adding golden tests for its output. It will be written once the existing adapters are extracted and their shared interface is settled.
