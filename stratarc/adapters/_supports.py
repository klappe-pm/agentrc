"""The runtime version ranges each bundled adapter was built and tested against, and how to detect an installed runtime's version.

One table, read by `stratarc.registry` to derive the bundled manifests. See the decision record adapter-registry-and-support-ranges.

Columns of SUPPORTS:

- `supports`: the permissive range. The lower bound is the oldest runtime version whose format the adapter's output still works with, taken from the evidence cited per row; where the repository holds no such evidence it is the tested version's major.minor. The upper bound is open up to the next major.
- `tested`: the exact runtime version the adapter was verified against, from the runtime's own `--version`.
- `tested_on`: the date that version was recorded.
- `exercised_by`: the tests and the expected output that exercise the adapter.
- `evidence`: why the lower bound is what it is.

This module is not an adapter: the leading underscore keeps it out of the runtime registry.
"""

from __future__ import annotations

SUPPORTS: dict[str, dict[str, str]] = {
    "claude": {
        "supports": ">=2.1,<3",
        "tested": "2.1.296",
        "tested_on": "2026-10-09",
        "exercised_by": "tests/test_adapters_claude.py; examples/notes-cli/expected/claude",
        # No repository evidence of an older format: the lower bound is the tested major.minor.
        "evidence": "none in the repository; lower bound is the tested major.minor",
    },
    "codex": {
        "supports": ">=0.156,<1",
        "tested": "0.162.0",
        "tested_on": "2026-10-09",
        "exercised_by": "tests/test_adapters_codex.py; examples/notes-cli/expected/codex",
        # stratarc/adapters/codex.py, _agent_md_to_toml: the agent role file keys were verified against Codex 0.156.1 on 2026-09-23.
        "evidence": "adapter docstring in codex.py records the role file schema verified on 0.156.1",
    },
    "cursor": {
        "supports": ">=2026.7,<2027",
        "tested": "2026.7.23",
        "tested_on": "2026-10-09",
        "exercised_by": "tests/test_adapters_cursor.py; examples/notes-cli/expected/cursor",
        # The version is that of cursor-agent (calendar versioned, 2026.07.23), the command detection runs; the editor is versioned separately (3.18.25).
        "evidence": "none in the repository; lower bound is the tested major.minor of cursor-agent",
    },
    "gemini": {
        "supports": ">=0.62,<1",
        "tested": "0.62.0",
        "tested_on": "2026-10-09",
        "exercised_by": "tests/test_adapters_gemini.py; examples/notes-cli/expected/gemini",
        "evidence": "none in the repository; lower bound is the tested major.minor",
    },
    "opencode": {
        "supports": ">=1.4,<2",
        "tested": "1.4.2",
        "tested_on": "2026-10-09",
        "exercised_by": "tests/test_adapters_opencode.py; examples/notes-cli/expected/opencode",
        # stratarc/adapters/opencode.py: OpenCode 1.4.2 refuses a config carrying the V2 `permissions` array (verified 2026-09-23).
        "evidence": "opencode.py records the permission config format verified on 1.4.2; lower bound is 1.4",
    },
}

# Per runtime: the command, its arguments and the regex whose first group is the version. Output of stdout and stderr is searched together, so a credentials notice printed first (codex) is skipped.
DETECT: dict[str, tuple[str, tuple[str, ...], str]] = {
    "claude": ("claude", ("--version",), r"(\d+\.\d+\.\d+)"),
    "codex": ("codex", ("--version",), r"codex-cli\s+(\d+\.\d+\.\d+)"),
    "cursor": ("cursor-agent", ("--version",), r"(\d{4}\.\d{1,2}\.\d{1,2})"),
    "gemini": ("gemini", ("--version",), r"(\d+\.\d+\.\d+)"),
    "opencode": ("opencode", ("--version",), r"(\d+\.\d+\.\d+)"),
}
