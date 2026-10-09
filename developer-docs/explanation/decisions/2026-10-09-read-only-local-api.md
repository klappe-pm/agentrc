# 2026-10-09-read-only-local-api

## status

accepted, 2026-10-09

## context

Dashboards, graph views and editors want the same data as the `list`, `show`, `explain` and `log` commands without parsing text or reading source files. An interface that can change deployed state would need authentication, authorization and a threat model that a local tool should not carry in its first release. See [api.py](../../../stratarc/api.py) and the local API section of [cli-design.md](../cli-design.md).

## decision

The local API uses the standard library only and answers GET only; every other method gets 405. It listens on a Unix socket with mode 0600 or on a loopback port, never on 0.0.0.0. Routes are versioned under `/v1`, the schema is published by `api schema`, and secrets never appear in responses. Writes through the API are out of scope for the first release.

## alternatives

- A web framework. It adds dependencies for a handful of read routes.
- Write endpoints from the start. Convenient, but they require authentication and make a local process a remote control for deployment.
- Bind to all interfaces for remote dashboards. Exposes configuration to the network with no authentication.
- No API, only JSON from the commands. Works, but every consumer then spawns a process per query.

## consequences

- Access control is the socket file mode, so it is as strong as the account it runs under.
- The API reuses the library functions behind the commands, so the two cannot disagree.
- Clients that need to change state must call the command line.
- The `/v1` prefix and published schema let the shape evolve without breaking existing clients.
