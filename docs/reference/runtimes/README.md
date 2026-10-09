# runtimes

The agent runtimes stratarc targets, with the default directory it deploys each one to and a page per runtime describing the files stratarc writes for it. Each page is a reference: the file names, their formats and which part of the source root feeds each one. The pages fill in as the adapters are extracted.

| runtime | default target | page |
| --- | --- | --- |
| Claude Code | `~/.claude/` | [claude](claude.md) |
| Codex | `~/.codex/` | [codex](codex.md) |
| Cursor | `~/.cursor/` | [cursor](cursor.md) |
| Gemini CLI | `~/.gemini/` | [gemini](gemini.md) |
| OpenCode | `~/.config/opencode/` | [opencode](opencode.md) |

The target for each runtime is set in `stratarc.toml`; see [configuration](../configuration.md).
