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

## process-log

At session start, read [COMMENTS-PROCESS.md](COMMENTS-PROCESS.md). When you observe friction in the extraction process itself, append one dated line per observation under its `entries` section in the form `- YYYY-MM-DD: <one or two lines>`. Never edit or remove an existing entry.

## tests

Run the test suite before every commit:

```bash
python3 -m pytest
```

## runtime-assets

Templates, schemas, hooks and git hooks live under `agentrc/data/`. Code reaches them only through `importlib.resources.files("agentrc") / "data"`, never through a path built from `__file__` or relative to the repository root, so a checkout and an installed wheel behave the same.
