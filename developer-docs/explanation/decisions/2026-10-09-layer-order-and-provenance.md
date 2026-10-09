# 2026-10-09-layer-order-and-provenance

## status

accepted, 2026-10-09

## context

The same setting can come from the shared source files, a runtime overlay, an account overlay, a project, an agent, an environment variable or a command-line flag. When a deployed value is wrong, the first question is where it came from and what it overrode. Without a fixed order and a record of each step, the answer means reading every file by hand.

## decision

Layers resolve from lowest to highest as base, runtime, account, project, agent, then environment and flags. Every resolved value reports the layer, file, line and operation (`set`, `merge`, `replace` or `extend`) of each step and the values it overrode. Resolution is read-only. A new kind of source file registers with one `register_file_kind` call that names its layer, location and key namespace. The implementation is [layers.py](../../../stratarc/layers.py) and the order is described in [cli-design.md](../cli-design.md).

## alternatives

- A flat merge with no provenance. Smaller, but every wrong value becomes an investigation.
- Provenance only on request through a separate tool. It would drift from the resolver and could disagree with it.
- A hard-coded list of file kinds. Simple at first, but each new source file would need an edit inside the resolver.

## consequences

- `explain` style commands and the API can answer where a value came from from one data structure.
- Because resolution never writes, it is safe to call from checks, the API and the terminal interface.
- The order is a public contract: moving a layer changes the meaning of existing source roots and needs a superseding record.
- Tracking line numbers requires the resolver to parse files itself instead of using a plain loader.
