# security

## reporting-a-vulnerability

Report vulnerabilities privately through GitHub security advisories: open the repository's Security tab and choose "Report a vulnerability", or go to <https://github.com/klappe-pm/stratarc/security/advisories/new>. Do not open a public issue, pull request or discussion for a suspected vulnerability.

Include the affected version or commit, the steps to reproduce, and the impact you observed. You will receive an acknowledgement, and the fix and disclosure are coordinated with you through the advisory.

## scope

- The `stratarc` command and the Python package.
- The guard hooks and git hooks shipped under `stratarc/data/`, including any way to bypass a guard, leak a secret past one, or make one execute unintended commands.
- The scaffold template and the configuration files stratarc writes into runtime directories.

Vulnerabilities in the agent runtimes themselves are out of scope; report those to their maintainers.

## supported-versions

Until the first release, only the `main` branch receives security fixes.
