# 2026-10-09-terminal-ui-library-is-open

## status

proposed

## context

The design calls for a full-screen terminal interface, described under terminal-interface in [cli-design](../cli-design.md). Drawing one by hand with raw escape sequences is fragile across terminals. The package declares no runtime dependencies in [pyproject.toml](../../../pyproject.toml), and every command today works with the standard library alone. The choice of library is listed as an open question in [cli-backlog](../cli-backlog.md), and the interface also needs the `config` writers and the resource editors before it can act.

## decision

Textual is the working choice, to be offered as an optional extra so the base install keeps zero runtime dependencies. The interface follows fixed layout rules: it fits 80 columns by 24 rows, falls back to monochrome, and shows a one-line keyboard help row. Its behavior is tested in a pseudo-terminal. No dependency is added to the project until this record is accepted.

## alternatives

- Standard library curses. It adds no dependency, but layout, resizing and testing would be built by hand, and curses support is uneven on some platforms.
- A lighter prompt or widget library. It would be smaller than Textual, but it does not cover a tree and detail layout without extra work.
- No terminal interface. The command line already covers every action, so this stays possible if the cost of the dependency outweighs the benefit.
- Make Textual a required dependency. This simplifies imports but ends the zero-dependency property for everyone, including CI and servers.

## consequences

- Until acceptance, `stratarc ui` stays unbuilt and the work waits on the writers and editors in the [backlog order](../cli-backlog.md).
- If accepted, the interface code must import the library lazily so a base install never fails on a missing extra.
- The extra has to be tested in CI against a pseudo-terminal, which adds a job.
- A rejection would be a new record that supersedes this one and removes `ui` from the design.
