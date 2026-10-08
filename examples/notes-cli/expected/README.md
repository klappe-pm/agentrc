# expected

The rendered runtime output a sync of `../source` produces, one directory per runtime plus `project/notes-cli/` for the managed project, assuming the source root lives at `~/agentrc-source`; replace a file with golden output once the engine lands.

Structural approximations, to be replaced by golden output when the engine lands:

- Every instruction file (`AGENTS.md`, `CLAUDE.md`, `CODEX.md`, `GEMINI.md`, and the project copies): the rule binding text and the `Full rule:` pointers are exact, but the digest heading, its introductory sentence and the marker comment wording are approximated.
- `codex/config.toml`: carries only the keys derived from `permissions.json`; the profile name `agentrc`, its marker comments and the per-repository trust tables are left to the engine.
- `codex/rules/agentrc.rules` and `gemini/policies/agentrc-permissions.toml`: the contents follow the permission translation, but the file names are approximated.
- `gemini/settings.json`: the hook matcher is written as the source spells it, and whether the engine translates it to a Gemini tool name is not derived.
- `opencode/`: the plugin that bridges OpenCode events to `hooks/block-database-commits.sh` ships with the engine and is not shown, so the copied script is not yet registered.
- `cursor/`: no `rules/` directory, because Cursor receives rules only from a fixed list of required user rules that this example does not define.
- `project/notes-cli/.claude/settings.json`: how a project `permissions.json` is delivered is not derived; the file shows the policy rendered whole, without `defaultMode`.
