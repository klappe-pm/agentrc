# 2026-10-09-textual-is-an-optional-extra-for-the-terminal-interface

## status

accepted, 2026-10-09. Supersedes [terminal UI library is open](2026-10-09-terminal-ui-library-is-open.md).

## context

The terminal interface needs a drawing library and the package has no runtime dependencies. The maintainer delegated the choice with the requirement that every decision be recorded. The interface must run in an 80 by 24 terminal, fall back to monochrome and be testable without a human.

## decision

The interface uses Textual and ships as the optional extra `stratarc[ui]`. The base install never imports it. Without the extra, `stratarc ui` exits 5 and names the extra to install. The data layer that builds the tree, the explain text, the log entries and the sync preview is plain Python with no Textual import, so the same data can feed other fronts. Every key in the interface calls the same library function as its command. App tests use Textual's headless test driver and skip when Textual is not installed.

## alternatives

Textual as a required dependency was rejected because most users run only the command line and the engine's guards and hooks must stay installable without a UI stack. The standard library `curses` was rejected because it has no portable Windows support and no test driver. A lighter widget library was rejected because none offered both a headless test driver and active maintenance. No interface was rejected because the maintainer asked for one.

## consequences

The base wheel stays dependency free and small. The interface is one install flag away and its tests need a second environment. A future Textual major version may need a port, which a new record would cover.
