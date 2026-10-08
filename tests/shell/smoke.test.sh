#!/usr/bin/env bash
# Proves the conftest collector runs a shell test and reads its exit status.
set -eu
[ "$((1 + 1))" -eq 2 ]
