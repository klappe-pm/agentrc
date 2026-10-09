# projects-root

Project-local configuration lives here, one directory per project named exactly like the project's checkout under `projects_root` (for example `projects-root/my-app/`). A project directory mirrors the source root's own layout for the pieces that apply only to that project: an `AGENTS.md` with project instructions, and optionally `rules/`, `skills/`, `commands/`, `agents/` and `hooks/hooks.json` in the same formats used at the top level; `stratarc sync` deploys them into that project's checkout.
