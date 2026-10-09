# notes-cli

A worked example of stratarc from the first command to the last. It is small enough to read in ten minutes and complete enough that every directory the engine reads holds a real entry.

## the-scenario

You maintain `notes-cli`, a command-line notes tool written in Python. You use Claude Code, Codex, Gemini CLI, Cursor and OpenCode, and you were tired of keeping five configurations in step by hand. So you keep one source root, `~/stratarc-source`, and let stratarc render it into all five.

Three things matter to you, and they are what the example configures:

- No agent ever commits your notes database. A global rule says so and a hook enforces it on shell commands.
- Commit messages follow one style everywhere, and you have one skill, one command and one agent you use in every project.
- `notes-cli` itself has stricter needs: its own instructions, a rule about the on-disk format, a rule that tests use a temporary directory, and a tighter permission policy.

The example has three parts. `source/` is the source root you would write, `expected/` is the output a sync of it produces, and this page connects the two.

## the-source-root

Everything under `source/`. A file marked "no row" is read directly by the engine as configuration and has no line in `control-plane.md`; every other file has one.

| file | why it exists | control-plane row |
| --- | --- | --- |
| `stratarc.toml` | Enables all five runtimes, names their target directories, and names the owner and projects root that decide which checkouts are managed. | linked from the runtime rows |
| `AGENTS.md` | The instructions every agent reads. It carries a marker pair where the rules digest is written. | `instruction-file` |
| `permissions.json` | The single permission policy, in Claude Code rule syntax, that each runtime adapter translates into its own enforcement. | no row |
| `components.json` | The manifest of external components and session budgets. This example declares budgets only and no servers or plugins. | no row |
| `control-plane.md` | The inventory that decides which resource reaches which scope. | not applicable |
| `rules/tiers.json` | Assigns each rule file to the `global`, `common` or `project` tier. | no row |
| `rules/never-commit-local-databases.md` | A `global` rule: applies everywhere and cannot be opted out. | `rules` |
| `rules/conventional-commit-messages.md` | A `common` rule: applies to every scope by default. | `rules` |
| `rules/tests-use-a-temporary-directory.md` | A `project` rule: applies only where a project opts in, here `notes-cli`. | `rules` |
| `hooks/hooks.json` | Registers the one hook on `PreToolUse` for shell commands. | no row |
| `hooks/block-database-commits.sh` | The hook script. It refuses a `git add` or `git commit` that names a database file. | `hooks-pre-tool-use` |
| `skills/write-changelog-entry/SKILL.md` | A skill that drafts a changelog line from the current diff. | `skills` |
| `commands/test-summary.md` | The `/test-summary` command: run the tests and summarize failures. | `commands` |
| `agents/diff-reviewer.md` | A read-only subagent that reviews a diff. | `agents` |
| `projects-root/notes-cli/AGENTS.md` | Instructions for the `notes-cli` checkout only. | `project-local-configuration` |
| `projects-root/notes-cli/rules/storage-format-is-versioned.md` | A rule that exists only for `notes-cli`. | `project-local-configuration` |
| `projects-root/notes-cli/permissions.json` | The `notes-cli` permission policy. | no row |

Each JSON file validates against the schema shipped under `stratarc/data/schema/`, and `tests/test_examples.py` checks that along with the shape of every rule and skill.

## the-managed-project

A checkout under `projects_root` whose origin belongs to `owner` is a managed project. Here that is `~/projects/notes-cli` with an origin under `example-owner`. It appears as a column in every table of `control-plane.md`, and `projects-root/notes-cli/` holds what is true of that project alone.

| concern | top level | `projects-root/notes-cli/` |
| --- | --- | --- |
| Instructions | `AGENTS.md` is deployed to each runtime directory. | `AGENTS.md` is deployed into the checkout, as `AGENTS.md`, `CLAUDE.md`, `CODEX.md` and `GEMINI.md`. |
| Rules | The digest holds the `global` and `common` rules. | The digest also holds the `project` rule the column opts into, and `.claude/rules/` receives the shared rules plus the project-local one. |
| Shared rules in the checkout | Not applicable. | A rule the global scope already ships arrives as its binding section only, so a session does not read it twice. A rule only this project uses, and the project-local rule, arrive whole. |
| Skills | Deployed to each runtime's skills directory. | The shared skill the column opts into lands in `.claude/skills/` and `.agents/skills/`, which the runtimes read per project. |
| Permissions | `permissions.json` is rendered into each runtime. | `permissions.json` narrows the policy to the checkout and denies the notes database. |
| Hooks, commands, agents | Deployed at user scope. | Not overridden, so the user scope versions apply. |

The result for a session in `~/projects/notes-cli` is the shared baseline plus the three project additions, with nothing to copy by hand.

## walkthrough

`stratarc init` works today. `stratarc sync`, `check`, `diff`, `prune` and `reconcile` are placeholders that print `stratarc <name>: not yet extracted` and exit 2 until the engine lands, so each step below describes what the command will do and the page stays true on either side of that change. Run every command from the directory shown.

### 1-stratarc-init

```bash
cd ~ && stratarc init ~/stratarc-source
```

```text
stratarc init: wrote 11 files to /home/you/stratarc-source
```

That scaffold is a bare source root: the instruction file, the configuration, an empty control plane and a README in each directory. Fill it in, or take the finished example. This copies it over the scaffold, from the root of your stratarc checkout:

```bash
cd <your stratarc checkout> && cp -R examples/notes-cli/source/. ~/stratarc-source/
```

### 2-edit-a-rule

You decide `perf` deserves its own commit type. Edit `~/stratarc-source/rules/conventional-commit-messages.md`, in the source root and nowhere else:

```diff
-Write each commit subject as `type(scope): summary`, using `feat`, `fix`, `docs`, `refactor`, `test` or `chore` as the type.
+Write each commit subject as `type(scope): summary`, using `feat`, `fix`, `docs`, `refactor`, `perf`, `test` or `chore` as the type.
```

Nothing deployed has changed yet. Runtime directories are generated output and are only rewritten by `sync`.

### 3-stratarc-check

```bash
cd ~/stratarc-source && stratarc check
```

`check` compares what each runtime directory holds with what the source would render, changes nothing, and lists every file that is out of date. After step 2 that is every file that carries the digest, because the changed rule is `common` and reaches every scope:

```text
claude/AGENTS.md  claude/CLAUDE.md  claude/CODEX.md
codex/AGENTS.md   gemini/GEMINI.md  opencode/AGENTS.md
notes-cli/AGENTS.md  notes-cli/CLAUDE.md  notes-cli/CODEX.md  notes-cli/GEMINI.md
notes-cli/.claude/rules/conventional-commit-messages.md
```

That listing is illustrative; the engine fixes the real format.

### 4-stratarc-diff

```bash
cd ~/stratarc-source && stratarc diff
```

`diff` shows the change itself, file by file, without writing it. For `claude/AGENTS.md` it is the one line you edited, rendered into the digest:

```diff
-Write each commit subject as `type(scope): summary`, using `feat`, `fix`, `docs`, `refactor`, `test` or `chore` as the type. Keep the summary lowercase, imperative and under 72 characters. Put the reason for the change in the body, not the subject.
+Write each commit subject as `type(scope): summary`, using `feat`, `fix`, `docs`, `refactor`, `perf`, `test` or `chore` as the type. Keep the summary lowercase, imperative and under 72 characters. Put the reason for the change in the body, not the subject.
```

That diff is illustrative too; the engine fixes the real format.

### 5-stratarc-sync

```bash
cd ~/stratarc-source && stratarc sync
```

`sync` renders the source into each enabled runtime and into the managed project. It writes only the files it owns and leaves everything else in a runtime directory alone, so settings you keep by hand survive. Its output names each file it writes; the exact wording is not fixed until the engine lands. The result is what `expected/` holds. The hook registration for Claude Code, for example, lands in `~/.claude/settings.json`:

```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Bash",
      "hooks": [
        {
          "type": "command",
          "command": "$HOME/.claude/hooks/block-database-commits.sh",
          "timeout": 3
        }
      ]
    }
  ]
}
```

### 6-what-appears-under-each-runtime

The same source becomes five different layouts, because each runtime reads its own formats.

```text
~/.claude/                       Claude Code
  AGENTS.md CLAUDE.md CODEX.md   AGENTS.md plus the rules digest, three identical copies
  settings.json                  permissions.json and hooks/hooks.json
  hooks/block-database-commits.sh
  skills/write-changelog-entry/SKILL.md
  commands/test-summary.md
  agents/diff-reviewer.md

~/.codex/                        Codex
  AGENTS.md
  config.toml                    permission profile from permissions.json
  rules/stratarc.rules            shell rules from the Bash entries
  hooks.json                     hook paths rewritten to ~/.codex/hooks
  hooks/block-database-commits.sh
  skills/write-changelog-entry/SKILL.md
  prompts/test-summary.md        the command, as a prompt
  agents/diff-reviewer.toml      the agent, as a role file

~/.gemini/                       Gemini CLI
  GEMINI.md
  settings.json                  approval mode and hooks, event renamed to BeforeTool, timeout in milliseconds
  policies/stratarc-permissions.toml
  hooks/block-database-commits.sh
  skills/write-changelog-entry/SKILL.md
  commands/test-summary.toml     the command, as TOML
  agents/diff-reviewer.md        the agent, with Gemini tool names

~/.cursor/                       Cursor
  hooks.json                     flat entries, Bash matcher renamed to Shell
  hooks/block-database-commits.sh

~/.config/opencode/              OpenCode
  AGENTS.md
  opencode.jsonc                 permission object from permissions.json
  rules/never-commit-local-databases.md
  hooks/block-database-commits.sh
  skill/write-changelog-entry/SKILL.md
  commands/test-summary.md       argument-hint and allowed-tools dropped
  agents/diff-reviewer.md        mode subagent, tools not listed are denied

~/projects/notes-cli/            the managed project
  AGENTS.md CLAUDE.md CODEX.md GEMINI.md
  .claude/rules/                 two shared rules as binding sections, one shared and one local rule whole
  .claude/skills/write-changelog-entry/SKILL.md
  .agents/skills/write-changelog-entry/SKILL.md
  .claude/settings.json          the project permission policy
```

Cursor receives only hooks here. Where a runtime reads a different format, the file is translated rather than copied, which is why the Codex agent is a TOML file and the Gemini command is a TOML prompt.

### 7-stratarc-reconcile

You add a command. Create `~/stratarc-source/commands/release-checklist.md` with a `description` in its frontmatter and a short prompt as the body, then run:

```bash
cd ~/stratarc-source && stratarc reconcile
```

`reconcile` notices the new source file, adds a row for it to `control-plane.md`, and leaves every existing cell as you set it. A new command opts in globally and stays off for the project until you choose it:

```text
| [command:release-checklist](commands/release-checklist.md) | x |  |
```

Run `stratarc sync` and the command appears in `~/.claude/commands/`, `~/.codex/prompts/`, `~/.gemini/commands/` and `~/.config/opencode/commands/`.

### 8-stratarc-prune

You decide the command was a mistake and delete `~/stratarc-source/commands/release-checklist.md`. Reconcile removes its row:

```bash
cd ~/stratarc-source && stratarc reconcile
```

A sync adds and updates but does not delete, so the four deployed copies are still there. `prune` removes the files in a runtime directory that the source no longer produces:

```bash
cd ~/stratarc-source && stratarc prune
```

It touches only paths stratarc owns, such as `commands/` and `prompts/`, so a command you installed into a runtime by hand stays.

## expected-output

`expected/` holds the files a sync of `source/` should produce, laid out one directory per runtime: `claude/`, `codex/`, `gemini/`, `cursor/` and `opencode/` are the contents of `~/.claude`, `~/.codex`, `~/.gemini`, `~/.cursor` and `~/.config/opencode`, and `project/notes-cli/` is the contents of `~/projects/notes-cli`.

To compare a real sync, give it a throwaway home so nothing of yours is touched. This assumes the engine expands `~` from `HOME`. Scaffold a checkout whose origin belongs to the example owner:

```bash
mkdir -p /tmp/stratarc-home/projects/notes-cli && git -C /tmp/stratarc-home/projects/notes-cli init -q && git -C /tmp/stratarc-home/projects/notes-cli remote add origin https://github.com/example-owner/notes-cli.git
```

Copy the example source to where the rules digest expects it, then sync and compare:

```bash
cd <your stratarc checkout> && mkdir -p /tmp/stratarc-home/stratarc-source && cp -R examples/notes-cli/source/. /tmp/stratarc-home/stratarc-source/
```

```bash
cd /tmp/stratarc-home/stratarc-source && HOME=/tmp/stratarc-home stratarc sync
```

```bash
cd <your stratarc checkout> && diff -r /tmp/stratarc-home/.claude examples/notes-cli/expected/claude
```

Repeat the last command for `.codex`, `.gemini`, `.cursor`, `.config/opencode` and `projects/notes-cli`, with the matching `expected/` directory. A few files differ until the engine lands because they are structural approximations; `expected/README.md` lists them, and each should be replaced with the real output at that point. Until then the commands above stop at `sync`, which exits 2.

`tests/test_examples.py` guards the example itself. Run it from the root of your checkout:

```bash
cd <your stratarc checkout> && python3 -m pytest -q tests/test_examples.py
```

## what-this-example-does-not-show

- External components. `components.json` declares session budgets and no MCP servers, plugins, foreign hooks, third-party skills or dependencies, so nothing is rendered from it.
- More than one managed project, or the lifecycle states of a project. One active checkout is enough to show the column.
- Project-local hooks, commands, agents and skills. `projects-root/notes-cli/` holds an instruction file, a rule and a permission policy, and those directories follow the same formats as the top level.
- How a project `permissions.json` is delivered. The file is valid and the expected project settings show it rendered whole, but the delivery path is not derived from the engine yet.
- The OpenCode hook bridge and Cursor's required user rules. Both come from the engine, not from the source root.
- The engine itself. Everything under `expected/` is derived from how the existing runtime adapters translate each file, and `expected/README.md` marks what is still approximate.
