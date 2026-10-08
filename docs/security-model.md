# security-model

This page will describe what agentrc protects against and how: the guard hooks that block secret exposure and unapproved actions, the git hooks that scan staged content for token-shaped values, how secrets are referenced rather than stored, and what agentrc trusts in a source root. It will be written once the guard library is extracted; until then, report vulnerabilities as described in [security.md](../security.md).
