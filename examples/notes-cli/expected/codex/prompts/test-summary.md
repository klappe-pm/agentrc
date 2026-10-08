---
description: Run the test suite and summarize any failures in a few plain sentences.
argument-hint: "[test path or -k expression]"
allowed-tools: Bash, Read
---

# test-summary

Run the project's tests and report the result so the user can decide what to do next.

## steps

1. Run `python3 -m pytest -q` with the path or expression argument when one is given.
2. If everything passes, say so in one sentence with the count of passed tests.
3. If anything fails, name each failing test, give the assertion or error in one line, and say whether the cause looks like the test or the code under test.

## output

- A one-sentence verdict.
- A list of failing tests with one line each, or "none".
