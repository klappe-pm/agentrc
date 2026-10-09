# 2026-10-09-pytest-is-the-only-test-runner

## status

accepted, 2026-10-09

## context

The engine inherited shell tests and bun tests alongside Python tests, plus a legacy runner and tests that read live operator data. Contributors would need several commands and a particular machine to get a result, and a result could change with the operator's own files.

## decision

pytest is the only test runner. The shell and bun tests are collected by [conftest.py](../../../tests/conftest.py), which runs a shell test with bash and a bun test with bun and skips the latter when bun is missing. Live reads of operator data are replaced by synthetic fixtures, and no legacy runner exists in this repository. [CI](../../../.github/workflows/ci.yml) installs with the test extra, runs pytest, validates the example and the template, and rejects machine paths in tests.

## alternatives

- Keep a separate runner per language. Each has its own flags and reports, and a contributor must know which to run.
- Keep tests that read real operator data. They pass on one machine and fail on another, and they can leak private content into output.
- Port every shell and bun test to Python. A large rewrite for no behavior gain.

## consequences

- One command runs everything, locally and in CI, with one report.
- A test that touches a path under a real home fails the machine-path check, which keeps tests portable.
- Shell and bun tests still need those tools installed to run; bun tests skip when it is absent.
- New test kinds need a collector in the same file, not a new runner.
