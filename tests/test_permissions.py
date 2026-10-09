"""Keep the permission set from denying commands a session is required to run.

The policy on GitHub CLI is one sentence: every `gh` action is allowed except deleting a repository or a project. Deleting the container is the only unrecoverable act; a deny rule over the other writes only produces sessions caught between two rules they cannot both satisfy. These tests check a policy against the commands rather than leaving the two to drift, over the fixture policy in tests/fixtures/permissions.

A `gh api` call that issues a DELETE against a repository is not expressible as a wildcard rule: `*` matches a slash in every adapter's regex, so a rule narrow enough to catch repository deletion also catches deleting a comment. That path is governed by the autoMode prohibition, which `test_auto_mode_states_the_deletion_prohibition` holds in place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stratarc import permissions

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "permissions"

# Commands a handoff-on-pull-request rule tells a session to run, followed by the ordinary GitHub writes the policy opens. None may be denied, and none may sit in `ask`: under bypassPermissions the Gemini and OpenCode adapters render `ask` as deny.
ALLOWED = (
    "gh pr view --json comments --jq '[.comments[].body] | last'",
    "gh pr create --title t --body-file /tmp/body.md",
    "gh pr comment 12 --body-file /tmp/handoff.md",
    "gh api repos/owner/repo/commits/abc123/comments -F body=@/tmp/handoff.md",
    "gh api -X POST repos/owner/repo/issues/1/comments -f body=x",
    "gh pr review 12 --approve",
    "gh pr edit 12 --title x",
    "gh pr reopen 12",
    "gh pr merge 12 --squash",
    "gh pr merge --admin 12",
    "gh issue create --title x --body y",
    "gh issue comment 5 --body y",
    "gh issue edit 5 --title x",
    "gh issue delete 5 --yes",
    "gh repo fork owner/repo",
    "gh release create v1.0.0 --notes x",
    "gh release delete v1.0.0 --yes",
)

# The two deletions the policy keeps closed: the repository and the project are the containers, and losing either loses everything inside it.
DENIED = (
    "gh repo delete owner/repo",
    "gh project delete 1 --owner example-org",
)


@pytest.fixture
def policy() -> dict:
    return permissions.load(FIXTURES)


def matches(policy: dict, command: str, effect: str) -> list[str]:
    """The patterns of one effect that match a whole command."""
    return [
        pattern
        for _, tool, pattern in permissions.parsed_rules(policy, effect)
        if tool == "Bash" and re.match(permissions.shell_regex(pattern), command)
    ]


@pytest.mark.parametrize("command", ALLOWED)
def test_github_commands_are_not_denied(policy, command):
    assert matches(policy, command, "deny") == []


@pytest.mark.parametrize("command", ALLOWED)
def test_github_commands_are_not_routed_to_ask(policy, command):
    assert matches(policy, command, "ask") == []


@pytest.mark.parametrize("command", DENIED)
def test_container_deletion_stays_denied(policy, command):
    assert matches(policy, command, "deny") != []


def test_auto_mode_states_the_deletion_prohibition(policy):
    """The `gh api` DELETE path has no wildcard rule, so the prose carries it."""
    allow = permissions.claude_auto_mode(policy).get("allow", [])
    assert any("gh api" in line and "prohibited" in line for line in allow)


def test_load_returns_empty_for_a_source_without_a_policy(tmp_path):
    assert permissions.load(tmp_path) == {}


def test_load_rejects_a_wrong_schema_version(tmp_path):
    (tmp_path / "permissions.json").write_text('{"schemaVersion": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="schemaVersion 1"):
        permissions.load(tmp_path)


def test_trusted_repo_roots_expand_the_tilde_against_the_engine_home(policy, stratarc_home):
    """`~` in additionalDirectories resolves through stratarc.paths.home(), so STRATARC_HOME redirects it."""
    repo = stratarc_home / "projects" / "demo"
    (repo / ".git").mkdir(parents=True)
    assert permissions.trusted_repo_roots(policy) == [repo]


def test_trusted_repo_roots_ignore_a_missing_directory(policy, stratarc_home):
    assert permissions.trusted_repo_roots(policy) == []
