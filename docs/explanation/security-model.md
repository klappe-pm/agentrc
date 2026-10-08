# security-model

This page explains what agentrc protects against and how, for a reader deciding whether to trust it with their runtime configuration. It covers the guard hooks that block secret exposure and unapproved actions, the git hooks that scan staged content for token-shaped values, how secrets are referenced rather than stored, and what agentrc trusts in a source root. It is explanation, not procedure: it says why the pieces exist and what they do not cover. It will be completed once the guard library is extracted; until then, report vulnerabilities as described in [security.md](../../security.md).

## what-ships-today

- A token-shaped value detector, `agentrc/data/hooks/lib/guard-utils.sh`, which matches private key blocks, AWS access keys, GitHub and Slack tokens, `sk`-prefixed API keys, Google API keys, JWTs, bearer tokens, and a generic assignment of a long value to a name containing key, token or secret. It is the single definition of "token-shaped" for every check in the repository, and it reports the kind of match, never the value.
- An attribution detector, `agentrc/data/hooks/lib/attribution-detect.py`, and a `commit-msg` git hook that strips agent attribution trailers from a commit message before it lands.
- A frontmatter strip, `python -m agentrc.strip_provenance`, which removes session provenance keys from Markdown frontmatter.

## trust-boundary

agentrc treats the source root as trusted input and the runtime directories as output it owns. It does not read secrets; configuration refers to them by name, and whatever resolves the name at runtime is outside agentrc. The runtimes themselves are outside its scope too; see the [scope](../../security.md#scope) section of the security policy.
