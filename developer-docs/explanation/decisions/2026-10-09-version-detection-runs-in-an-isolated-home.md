# 2026-10-09-version-detection-runs-in-an-isolated-home

## status

Accepted, 2026-10-09, by the maintainer.

## context

The adapter registry learns each installed runtime's version by running its command with `--version`. The commands ran with the caller's full environment and working directory. Under a redirected home (the test suite, a container, or a user syncing against another home) the runtimes created their own directories and files inside it, such as `~/.codex/tmp/arg0`, `~/.cursor/cli-config.json`, `~/.gemini/projects.json.*.tmp` and `~/.config/opencode`. The next sync then saw those runtime targets as live and failed with `missing runtimeDirectories entry for ~/.<runtime>`. The same inheritance let the runtimes read the user's credentials and print credential notices, and a hung command could stall a sync for the old 10 second limit.

## decision

`detect_versions` in [registry.py](../../../stratarc/registry.py) runs every detection command with these limits:

- `HOME`, the `XDG_*` base directories and the runtimes' own home variables point at a fresh temporary directory that is removed afterwards.
- The environment is an allowlist: `PATH`, `LANG`, `LC_*`, `TERM=dumb`, `NO_COLOR=1`, `CI=1` and the temporary homes. Tokens and every other variable are dropped.
- The working directory is the temporary directory, stdin is closed, and each command has a hard 5 second timeout.
- Any failure of one detection (missing command, timeout, non-zero exit, unparsable output, operating system error) gives an unknown version for that runtime and never raises.
- Results from the real runner are cached for the life of the process, keyed by runtime and resolved command path. An injected runner is never cached.

## alternatives

- Keep the caller's environment and clean up afterwards. Rejected: it cannot undo a read of credentials, and a cleanup would delete files the user owned.
- Skip detection when the home is redirected. Rejected: the version is what the support range check needs, and a redirected home is a normal way to run.
- Parse the version from package metadata instead of running the command. Rejected: the runtimes ship in different forms, and only the command reports the version that will run.

## consequences

- Detection cannot create files in, or read credentials from, the caller's home.
- A runtime that needs its configuration to print a version reports unknown instead. That is an allowed state of the registry and never blocks a sync.
- A repeated call in one process costs nothing, and a hung runtime costs at most 5 seconds.
