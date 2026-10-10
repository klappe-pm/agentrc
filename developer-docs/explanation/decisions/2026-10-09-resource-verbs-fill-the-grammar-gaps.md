# 2026-10-09-resource-verbs-fill-the-grammar-gaps

## status

accepted, 2026-10-09

## context

The [resource verb grammar](2026-10-09-resource-verb-command-grammar.md) gave every resource the same verbs, but four gaps remained. `account edit` worked only through an editor, so a script could not change one value. An agent could be listed, shown and edited but not created or removed from the command line. `project enable` and `project disable` changed the opt-in cells and left the manifest `status` column, which the reconciler and the sync read as the project's state, unchanged, so the two tables disagreed until the next reconcile. And nothing pinned the file mode of a freshly scaffolded source root, which is written without the home's 0600 narrowing and is meant to be committed.

## decision

`account edit` takes `--set KEY=VALUE` and `--unset KEY`, applied by the existing comment-preserving line editor and accepted only when the parsed result equals the old document plus the requested changes. `agent add NAME` writes `agents/NAME.md` with the frontmatter the validator requires and refuses an existing name with exit 4; `agent remove NAME --yes` deletes the agent's files after backing each up. `project enable` and `project disable` also set the manifest status cell to `active` or `inactive` in the same write as the opt-in cells, and report `reconcile_needed` when the manifest has no status cell for the project. A regression test fixes that `source init` writes files and directories with the default mode.

## alternatives

- Rewrite the account file from a parsed document for `--set`. This is simpler, but it drops every comment, which the editor path already promises to keep.
- Leave `agent add` to a template file the user copies. This needs no code, but the stem, the `name` key and the tool names are easy to get wrong, and the validator only reports it afterwards.
- Make `--parent` copy the whole parent file. This would carry the parent's body and any later drift with it; inheriting only the three frontmatter defaults keeps the new agent independent.
- Leave the status column to the reconciler. This keeps one writer, but a user who disables a project sees it still listed as active until a separate command runs.
- Narrow scaffolded files to 0600 like the home. This is private by default, but a source root is committed and shared, and a restrictive mode would travel badly across checkouts.

## consequences

- A script can change one account value, create an agent and toggle a project without an editor, and each result is validated before it is saved.
- The manifest status cell now has a second writer. It is a small cell edit that is read back through `ControlPlane` before saving, and the reconciler stays the owner of adding rows and columns.
- `agent-exists` is a new string code and needs a catalog id. `unknown-key` is reused from the configuration layer with the same meaning, a key that is not set.
- A top-level `--set` key is written before the first table, which the line editor did not support before.
