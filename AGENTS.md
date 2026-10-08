# AGENTS.md

Instructions for any coding agent working in this repository.

## public-repository

This repository is public under MIT. Everything committed here is visible to anyone, so treat every file, commit, pull request and comment as published.

## attribution

- Never add `models`, `providers` or `session-link` keys to the frontmatter of any Markdown file.
- Never add a session link anywhere: files, commit messages, pull request titles or bodies, or comments.
- Never credit an agent, model, model provider or agent runtime as author or contributor in files, commits, pull requests or comments. No co-author trailers, no "generated with" lines.
- Never post a session tokens comment on a pull request.

## secrets

Never commit a secret, credential, private key, `.env` file or `settings.local.json`. `.env.example` with placeholder values is allowed.

## process-observations

No process log is committed in this repository. When you observe friction in the work itself, record it in the commit that carries the work: end the commit message with a section headed `Process observations:` followed by a bulleted list of one or two line entries. The same entries may be aggregated in `.docs/`, which is gitignored and never published.

## tests

Run the test suite before every commit:

```bash
python3 -m pytest
```

## documentation

Documentation lives in three places. `docs/` is for people who use agentrc, `developer-docs/` is for people who change it, and `.docs/` is gitignored internal work product (prompts, questions, provenance, session notes, hook snapshots) that is never published. Inside `docs/` and `developer-docs/` every page is one Diátaxis kind and sits in the folder for that kind: `tutorials/`, `how-to-guides/`, `reference/` or `explanation/`; a page that would be two kinds is two pages. [docs/documentation-guide/README.md](docs/documentation-guide/README.md) has the full rule.

## runtime-assets

Templates, schemas, hooks and git hooks live under `agentrc/data/`. Code reaches them only through `importlib.resources.files("agentrc") / "data"`, never through a path built from `__file__` or relative to the repository root, so a checkout and an installed wheel behave the same.
