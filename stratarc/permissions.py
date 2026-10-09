"""Read and translate the single canonical permission policy.

The source format intentionally keeps Claude's established rule syntax. It is
the policy the user edits in permissions.json; adapters translate it into the
closest native enforcement surface for their runtime.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
from pathlib import Path
from typing import Iterable, Iterator

from stratarc.paths import home


_RULE = re.compile(r"^(Bash|Read|Edit)\((.*)\)$")
_REQUIRED_LISTS = ("allow", "deny", "ask")

# Directory names never entered when a walk looks for repositories or project
# config: vendored dependencies, virtualenvs, and caches.
PRUNE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".cache",
        ".obsidian",
        "site-packages",
        ".tox",
        ".mypy_cache",
        ".next",
        "target",
    }
)
MAX_WALK_DEPTH = 8


def load(source: Path) -> dict:
    """Load and validate the canonical permissions.json file."""
    path = source / "permissions.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise ValueError("permissions.json must be a schemaVersion 1 object")
    for key in _REQUIRED_LISTS:
        if not isinstance(data.get(key), list) or not all(
            isinstance(item, str) for item in data[key]
        ):
            raise ValueError(f"permissions.json {key!r} must be a list of strings")
    if not isinstance(data.get("additionalDirectories", []), list):
        raise ValueError("permissions.json additionalDirectories must be a list")
    if not isinstance(data.get("runtimeDirectories", []), list) or not all(
        isinstance(item, str) for item in data.get("runtimeDirectories", [])
    ):
        raise ValueError(
            "permissions.json runtimeDirectories must be a list of strings"
        )
    if "autoMode" in data and not isinstance(data["autoMode"], dict):
        raise ValueError("permissions.json autoMode must be an object")
    if "codexApprovalPolicy" in data and data["codexApprovalPolicy"] not in (
        "on-request",
        "never",
    ):
        raise ValueError(
            "permissions.json codexApprovalPolicy must be on-request or never"
        )
    if "codexNetwork" in data:
        network = data["codexNetwork"]
        if not isinstance(network, dict) or not isinstance(
            network.get("enabled"), bool
        ):
            raise ValueError("permissions.json codexNetwork.enabled must be a boolean")
        domains = network.get("domains", {})
        if not isinstance(domains, dict) or any(
            not isinstance(host, str) or not host or action not in ("allow", "deny")
            for host, action in domains.items()
        ):
            raise ValueError(
                "permissions.json codexNetwork.domains must map hosts to allow or deny"
            )
    if "policyVersion" in data and (
        not isinstance(data["policyVersion"], int)
        or isinstance(data["policyVersion"], bool)
        or data["policyVersion"] < 0
    ):
        raise ValueError("permissions.json policyVersion must be a non-negative integer")
    if "codexShellEnvironment" in data:
        shell_env = data["codexShellEnvironment"]
        if not isinstance(shell_env, dict):
            raise ValueError("permissions.json codexShellEnvironment must be an object")
        passthrough = shell_env.get("passthrough", [])
        if not isinstance(passthrough, list) or not all(
            isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            for name in passthrough
        ):
            raise ValueError(
                "permissions.json codexShellEnvironment.passthrough must be a list "
                "of SCREAMING_SNAKE_CASE variable names"
            )
    return data


def policy_version(policy: dict) -> int:
    """The policy's version number, 0 when it carries none.

    Bump it in permissions.json whenever the policy changes in a way an
    older checkout must not undo. The deploy guard refuses to write a lower
    version over a higher one.
    """
    version = policy.get("policyVersion", 0)
    return version if isinstance(version, int) and version >= 0 else 0


def claude_settings(policy: dict) -> dict:
    """Return Claude Code's native permissions object without source metadata."""
    keys = (
        "allow",
        "deny",
        "ask",
        "defaultMode",
        "blockReadsOutsideWorkingDirectories",
        "additionalDirectories",
    )
    rendered = {key: copy.deepcopy(policy[key]) for key in keys if key in policy}
    directories = list(rendered.get("additionalDirectories", [])) + runtime_directories(
        policy
    )
    if directories:
        rendered["additionalDirectories"] = list(dict.fromkeys(directories))
    return rendered


def runtime_directories(policy: dict) -> list[str]:
    """Runtime-owned configuration roots that must remain writable to agents."""
    return list(policy.get("runtimeDirectories", []))


def claude_auto_mode(policy: dict) -> dict | None:
    """Return Claude Code's auto-mode guidance block, or None when the policy carries none.

    The block names the trusted repository tree, so it is part of the permission
    policy: a stale path here makes auto mode prompt for routine work.
    """
    block = policy.get("autoMode")
    return copy.deepcopy(block) if isinstance(block, dict) else None


def noninteractive(policy: dict) -> bool:
    """Whether canonical mode suppresses approval prompts for unmatched work."""
    return policy.get("defaultMode") == "bypassPermissions"


def claude_project_permissions(policy: dict) -> dict:
    """Return the permissions object a project-scope .claude/settings.json may carry.

    Same rules as claude_settings, minus defaultMode. Claude Code ignores
    bypassPermissions and auto when a project-scope file sets them and starts
    the session in Manual mode instead, and any other value there would
    override the user-scope mode. Either way the result is a prompt on every
    action, so the mode is user-scope only and project files never state one.
    https://code.claude.com/docs/en/permission-modes
    """
    rendered = claude_settings(policy)
    rendered.pop("defaultMode", None)
    return rendered


def codex_approval_policy(policy: dict) -> str:
    """Honor explicit Codex review routing, retaining the legacy default."""
    return policy.get(
        "codexApprovalPolicy", "never" if noninteractive(policy) else "on-request"
    )


def gemini_approval_mode(policy: dict) -> str:
    """Gemini general.defaultApprovalMode value that matches the canonical mode."""
    return "auto_edit" if noninteractive(policy) else "default"


def parsed_rules(
    policy: dict, effect: str | None = None
) -> Iterator[tuple[str, str, str]]:
    """Yield (effect, tool, pattern) for portable Claude-style rules."""
    effects = (effect,) if effect else _REQUIRED_LISTS
    for current_effect in effects:
        for rule in policy.get(current_effect, []):
            match = _RULE.match(rule)
            if match:
                yield current_effect, match.group(1), match.group(2)


def shell_regex(pattern: str) -> str:
    """Translate a shell wildcard pattern into a whole-command regular expression."""
    return "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"


def shell_prefix(pattern: str) -> list[str] | None:
    """Return a safe Codex prefix for a shell rule, or None when it is ambiguous."""
    try:
        words = shlex.split(pattern)
    except ValueError:
        return None
    if not words:
        return None
    if words[-1] == "*":
        words = words[:-1]
    if not words or any("*" in word or "?" in word for word in words):
        return None
    return words


def filesystem_denies(
    policy: dict, tools: Iterable[str] = ("Read", "Edit")
) -> list[str]:
    """Return unique denied filesystem globs from the named tools' deny rules, in policy order."""
    wanted = set(tools)
    result: list[str] = []
    for _, tool, pattern in parsed_rules(policy, "deny"):
        if tool not in wanted:
            continue
        path = pattern.replace("//", "/")
        if path not in result:
            result.append(path)
    return result


def opencode_permission(policy: dict) -> dict:
    """Render OpenCode's "permission" object from the canonical policy.

    OpenCode 1.4.2 refuses a config carrying the V2 "permissions" array this
    function used to render ("Configuration is invalid ... Unrecognized key:
    "permissions"", verified 2026-09-23, `opencode debug config`). Its key is
    "permission": an object from tool (read, edit, bash, external_directory,
    and so on) to an action or to an object from pattern to action, where
    "the last matching rule wins" (verified 2026-09-23, schema at
    https://opencode.ai/config.json and
    https://github.com/sst/opencode/blob/dev/packages/web/src/content/docs/permissions.mdx).

    So "*" comes first as the catch-all, and inside each tool the rules are
    written allow, then ask, then deny, so that deny wins an overlap the way
    it does in Claude Code. A pattern repeated with a stronger effect moves to
    the end. Claude's "//" absolute prefix becomes "/". The policy's additional
    and runtime directories become external_directory allows, the OpenCode
    counterpart of Claude's additionalDirectories.
    """
    mode = "allow" if noninteractive(policy) else "ask"
    rendered: dict = {"*": mode}
    tool_map = {"Bash": "bash", "Read": "read", "Edit": "edit"}
    for effect in ("allow", "ask", "deny"):
        native_effect = effect
        if effect == "ask" and noninteractive(policy):
            native_effect = "deny"
        for _, tool, pattern in parsed_rules(policy, effect):
            if tool != "Bash" and pattern.startswith("//"):
                pattern = pattern[1:]
            rules = rendered.setdefault(tool_map[tool], {})
            rules.pop(pattern, None)
            rules[pattern] = native_effect
    directories = list(policy.get("additionalDirectories", [])) + runtime_directories(policy)
    if directories:
        external = rendered.setdefault("external_directory", {})
        for directory in dict.fromkeys(directories):
            external[directory.rstrip("/") + "/**"] = "allow"
    return rendered


def same_in_order(existing: object, desired: object) -> bool:
    """Equality that also compares key order, for policy objects where order matters.

    OpenCode applies the last matching rule, so a permission object holding
    the same entries in another order is a different policy, though Python's
    dict equality calls the two equal (found by the Codex review of PR 64).
    """
    return json.dumps(existing) == json.dumps(desired)


def gemini_policy_toml(policy: dict) -> str:
    """Render Gemini's rule file, using commandRegex for shell-level fidelity."""
    lines = ["# Generated from permissions.json. Do not edit this output.", ""]
    base = "allow" if noninteractive(policy) else "ask_user"
    lines.extend(
        (
            "[[rule]]",
            'toolName = "*"',
            f'decision = "{base}"',
            "priority = 1",
            "allowRedirection = true",
            "",
        )
    )
    tool_map = {"Bash": "run_shell_command", "Read": "read_file", "Edit": "write_file"}
    for effect in ("allow", "deny", "ask"):
        decision = {"allow": "allow", "deny": "deny", "ask": "ask_user"}[effect]
        if effect == "ask" and noninteractive(policy):
            decision = "deny"
        priority = {"allow": 500, "ask": 750, "deny": 999}[effect]
        for _, tool, pattern in parsed_rules(policy, effect):
            lines.extend(
                (
                    "[[rule]]",
                    f'toolName = "{tool_map[tool]}"',
                    f'decision = "{decision}"',
                    f"priority = {priority}",
                )
            )
            if tool == "Bash":
                lines.append(f"commandRegex = {json.dumps(shell_regex(pattern))}")
                lines.append("allowRedirection = true")
            else:
                lines.append(f"argsPattern = {json.dumps(shell_regex(pattern))}")
            lines.append("")
    return "\n".join(lines)


def repo_roots(root: Path, max_depth: int = MAX_WALK_DEPTH) -> list[Path]:
    """Every directory under root, root included, that holds a .git entry.

    Nested repositories, submodules, and linked worktrees all count, because
    Codex keys folder trust on whichever marker it finds nearest. Directories
    named in PRUNE_DIRS are never entered.
    """
    found: list[Path] = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, _ in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        current = Path(dirpath)
        if (current / ".git").exists():
            found.append(current)
        if len(current.relative_to(root).parts) >= max_depth:
            dirnames[:] = []
    return sorted(found)


def _expand_user(entry: str) -> Path:
    """Expand a leading `~` against `stratarc.paths.home()`, so `STRATARC_HOME` redirects it."""
    if entry == "~":
        return home()
    if entry.startswith("~/"):
        return home() / entry[2:]
    return Path(entry)


def trusted_repo_roots(policy: dict) -> list[Path]:
    """Every repository under the policy's additional directories, the tree the policy treats as the user's own."""
    roots: list[Path] = []
    for entry in policy.get("additionalDirectories", []):
        for root in repo_roots(_expand_user(entry)):
            if root not in roots:
                roots.append(root)
    return sorted(roots)
