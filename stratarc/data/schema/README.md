# schema

`hooks.schema.json` validates `hooks/hooks.json`, the canonical hook registry: each of the eight canonical events maps to a list of hook groups, each group carries an optional tool `matcher` and a non-empty list of command hooks with an optional timeout in seconds. No engine validator reads this file directly, so the schema encodes the shape the staging filter and the runtime adapters rely on.

`permissions.schema.json` validates `permissions.json`, the single permission policy every adapter translates: `schemaVersion` 1, the `allow`, `deny` and `ask` rule lists, the writable runtime directories, the Codex approval policy, network and shell environment settings, and the auto-mode guidance block. Unknown top-level keys are accepted, matching the engine's loader.

`components.schema.json` validates `components.json`, the manifest of external components: MCP servers, plugins, foreign hook groups, third-party skills and dependencies, plus budgets, autonomy limits, per-runtime settings, platform differences, environments and promotion. The `deploy` and `services` sections are accepted without being described. Checks that need code, such as unique entry names and the token-shaped value scan, are listed in the schema's `$comment`.
