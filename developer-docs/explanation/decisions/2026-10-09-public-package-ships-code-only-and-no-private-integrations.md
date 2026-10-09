# 2026-10-09-public-package-ships-code-only-and-no-private-integrations

## status

accepted, 2026-10-09

## context

The engine began inside the maintainer's own configuration repository, alongside that repository's skills, agents, commands and bindings to specific tools. A public package that carried all of it would ship third-party content it has no right or reason to redistribute, and integrations that almost no user can run.

## decision

The public package ships code, not third-party skills, agents or commands. Integrations with specific third-party tools that most users will not have (a work ledger tool binding, a review engine, a canvas tool) are excluded and remain private extensions in the maintainer's own source root. Credentials handling is withheld from the first release. A project permissions special case keyed on one private project name was generalised to a `projectScope` key in the project's permissions file (see [project_permissions.py](../../../stratarc/project_permissions.py)). Code and install steps for the review engine were removed.

## alternatives

- Ship everything and mark integrations optional. Users would carry dead code and unsupported install steps.
- Keep the private project name as a special case. It leaks a private name and cannot serve other projects.
- Include credentials handling now. It widens the security surface before the provider and secret-reference model has settled.

## consequences

- The package is smaller, has no hidden dependencies and can be audited as a whole.
- The maintainer keeps private integrations by registering them from a source root, through the same extension points any user has.
- Users who want credential handling must wait for a later release or use their own resolver with `secret://` references.
- Any new integration must justify why most users would have the tool before it enters the package.
