"""Codex runtime adapter -- syncs the shared config into ~/.codex/.

Exposes:
    sync(source: Path, target: Path, dry_run: bool = False) -> list[str]

Codex formats (learned by reading the real install):
  - hooks.json: {"hooks": {<Event>: [...]}} with same inner shape as Claude;
    SessionStart, UserPromptSubmit and Stop fire with no feature flag set
    (verified 2026-09-23, Codex 0.156.1 `codex exec`, `hooks` in the default
    feature list of `codex doctor`)
  - agents/*.toml: role files found in $CODEX_HOME/agents with no [agents]
    registration, keys name, description, developer_instructions, model,
    model_reasoning_effort (verified 2026-09-23, Codex 0.156.1 `codex exec`)
  - skills/*/SKILL.md: same SKILL.md format as Claude (YAML frontmatter + body)
  - prompts/*.md: same format as Claude commands (YAML frontmatter + body)
  - rules/default.rules: prefix_rule() DSL -- DO NOT overwrite
  - config.toml: merge canonical Codex-only settings without changing runtime-native keys
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

from ._common import (
    OwnedDir,
    home,
    is_claude_model,
    mirror_skills,
    mirrored_names,
    read_frontmatter,
    repoint_command,
    skill_excludes,
)
from ._components import agent_aliases, alias_for, default_models, render as render_components
from ._foreign import render as render_foreign
from ._inventory import Item, mcp_items, read_toml, unreadable
from ._text import set_top_level, split_top_level, top_level_entries
from agentrc.paths import engine_name
from agentrc.permissions import (
    codex_approval_policy,
    filesystem_denies,
    load as load_permissions,
    noninteractive,
    parsed_rules,
    runtime_directories,
    shell_prefix,
    trusted_repo_roots,
)


# This runtime's entry in the runtime registry (_common.runtime_registry).
RUNTIME = {
    "name": "codex",
    "target": ".codex",
    "hook_registry": "hooks.json",
    "hook_events": [
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "UserPromptSubmit",
        "SessionStart",
        "SubagentStart",
        "SubagentStop",
    ],
}

# Canonical events Codex has no hook event for, dropped from our groups. Codex 0.156.1's
# HookEventsToml names PreToolUse, PermissionRequest, PostToolUse, PreCompact,
# PostCompact, SessionStart, SessionEnd, UserPromptSubmit, SubagentStart,
# SubagentStop, Stop and Interrupt, and no PostToolUseFailure (verified
# 2026-09-24, the strings of /opt/homebrew/Caskroom/codex/0.156.1/bin/codex).
# Every other event keeps its canonical name.
_UNSUPPORTED_EVENTS = frozenset({"PostToolUseFailure"})

_HOOKS_PREFIX = "$HOME/.codex/hooks"
_HOOK_MIRROR_EXCLUDES = [
    "*.test.sh",
    "hook-test.sh",
    "hooks.json",
    "claude-worktree-hooks.json",
    "claude-agent-graph-hooks.json",
    "opencode-runtime-hooks.ts",
    "retired.json",
]
# Skill directories Codex itself writes under target/skills/; see owned_outputs.
_RUNTIME_SKILL_DIRS = frozenset({".system"})


def _rewrite_hook_paths(command: str) -> str:
    """Rewrite $HOME/.claude/hooks/ -> $HOME/.codex/hooks/ in a hook command string."""
    home_dir = str(home())
    # Handle both literal home path and $HOME
    result = command.replace(f"{home_dir}/.claude/hooks/", f"{home_dir}/.codex/hooks/")
    result = result.replace("$HOME/.claude/hooks/", "$HOME/.codex/hooks/")
    return result


def _source_hook_names(source: Path) -> set[str]:
    """Collect basenames of hook scripts in source hooks/ dir."""
    hooks_dir = source / "hooks"
    if not hooks_dir.is_dir():
        return set()
    names = set()
    for f in hooks_dir.iterdir():
        if f.is_file() and f.suffix == ".sh" and not f.name.endswith(".test.sh"):
            names.add(f.name)
    return names


def _command_references_source_hook(command: str, source_names: set[str]) -> bool:
    """Check if a hook command references one of our source hook scripts."""
    home_dir = str(home())
    for name in source_names:
        if (
            f"{home_dir}/.codex/hooks/{name}" in command
            or f"$HOME/.codex/hooks/{name}" in command
        ):
            return True
    return False


def _group_is_ours(group: dict, source_names: set[str]) -> bool:
    """Return True if a hook group's commands reference source hook scripts."""
    for hook in group.get("hooks", []):
        cmd = hook.get("command", "")
        if _command_references_source_hook(cmd, source_names):
            return True
    return False


def _agent_md_to_toml(fm: dict, body: str, aliases: dict | None = None) -> str:
    """Convert agent frontmatter+body to a Codex agent role file.

    Codex role keys: name, description, developer_instructions, and the
    optional model and model_reasoning_effort (verified 2026-09-23, Codex
    0.156.1: `codex exec` listed a role file's model; the AgentRoleToml
    schema at https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/config.schema.json).

    A key Codex does not know makes it ignore the whole role file with
    "Ignoring malformed agent role definition: ... unknown field `color`"
    (verified 2026-09-23, Codex 0.156.1 `codex exec`), so the Claude-only
    color and tools fields are left out. A Claude model name is left out too:
    Codex answers "The 'opus' model is not supported when using Codex with a
    ChatGPT account" (verified 2026-09-23, `codex exec -m opus`), and a role
    with no model inherits the session's, unless runtime_settings.codex.aliases
    in components.json maps the name to a Codex model.
    """
    lines: list[str] = []

    name = fm.get("name", "unknown")
    lines.append(f'name = "{name}"')

    desc = str(fm.get("description", ""))
    lines.append(f"description = {json.dumps(desc)}")

    # Replace CLAUDE.md -> AGENTS.md in body
    converted_body = body.replace("CLAUDE.md", "AGENTS.md")
    lines.append(f"developer_instructions = {json.dumps(converted_body)}")

    model = fm.get("model")
    aliased = alias_for(model, aliases)
    if aliased:
        lines.append(f"model = {json.dumps(aliased)}")
    elif not is_claude_model(model):
        lines.append(f"model = {json.dumps(str(model).strip())}")
    effort = str(fm.get("model_reasoning_effort", "")).strip()
    if effort:
        lines.append(f"model_reasoning_effort = {json.dumps(effort)}")

    return "\n".join(lines) + "\n"


def _codex_permissions_block(policy: dict, name: str | None = None) -> str:
    """Render the managed Codex profile from canonical filesystem rules.

    A Codex filesystem "deny" blocks reads and writes together, so an
    absolute path is denied only when the policy denies reading it. An
    Edit-only rule outside the workspace roots needs no entry at all: the
    :workspace preset already grants writes only inside those roots and the
    temp directories. Inside a writable root only "deny" can restrict, so an
    Edit-only "**/" rule is rendered as a deny and annotated.
    """
    roots = list(
        dict.fromkeys(
            list(policy.get("additionalDirectories", [])) + runtime_directories(policy)
        )
    )
    read_denied = set(filesystem_denies(policy, tools=("Read",)))
    workspace_rules: list[tuple[str, bool]] = []
    absolute_rules: list[str] = []
    for path in filesystem_denies(policy):
        if path.startswith("**/"):
            workspace_rules.append((path, path in read_denied))
        elif path in read_denied:
            absolute_rules.append(path.removesuffix("/**"))

    name = name or engine_name()
    lines = [
        f"# BEGIN {name} permissions, generated from permissions.json",
        f"[permissions.{name}]",
        'extends = ":workspace"',
        "",
    ]
    if roots:
        lines.append(f"[permissions.{name}.workspace_roots]")
        lines.extend(f"{json.dumps(root)} = true" for root in roots)
        lines.append("")
    if workspace_rules:
        lines.append(f'[permissions.{name}.filesystem.":workspace_roots"]')
        for path, reads_denied in workspace_rules:
            lines.append(f'{json.dumps(path)} = "deny"')
            if not reads_denied:
                lines.append(
                    "# Edit-only in permissions.json; Codex cannot deny writes alone inside a writable root"
                )
        lines.append("")
    if absolute_rules:
        lines.append(f"[permissions.{name}.filesystem]")
        lines.extend(f'{json.dumps(path)} = "deny"' for path in absolute_rules)
        lines.append("")
    network = policy.get("codexNetwork")
    if network is not None:
        lines.extend(
            [
                f"[permissions.{name}.network]",
                f"enabled = {json.dumps(network['enabled'])}",
                "",
            ]
        )
        if network.get("domains"):
            lines.append(f"[permissions.{name}.network.domains]")
            lines.extend(
                f"{json.dumps(host)} = {json.dumps(action)}"
                for host, action in network["domains"].items()
            )
            lines.append("")
    lines.append(f"# END {name} permissions")
    return "\n".join(lines) + "\n"


def _managed_permission_table(name: str) -> re.Pattern[str]:
    escaped = re.escape(name)
    return re.compile(
        rf"(?ms)^\[permissions\.(?:{escaped}|\"{escaped}\"|'{escaped}')(?:\.[^\]]*)?\]\n"
        r".*?(?=^\[|\Z)"
    )


def _strip_managed_permissions(toml: str, name: str | None = None) -> str:
    """Remove every prior copy of the managed profile, marked or not.

    Codex re-serializes config.toml whenever a setting changes in the TUI, and
    that rewrite sorts the tables and drops comments. A block that loses its
    BEGIN/END markers that way is invisible to the marker regex, so the next
    sync appends a second [permissions.<name>] and Codex refuses to load the
    file at all ("Cannot declare (permissions, <name>) twice"). Strip the
    marked region first so its comments go with it, then any surviving
    permissions.<name> table wherever the runtime moved it.
    """
    name = name or engine_name()
    escaped = re.escape(name)
    without_markers = re.sub(
        rf"(?ms)^# BEGIN {escaped} permissions, generated from permissions\.json\n.*?^# END {escaped} permissions\n?",
        "",
        toml,
    )
    return _managed_permission_table(name).sub("", without_markers)


def _codex_rules(policy: dict) -> str:
    """Render Codex command rules without touching the user's default.rules."""
    lines = ["# Generated from permissions.json. Do not edit this output."]
    skipped: list[str] = []
    for effect in ("allow", "deny", "ask"):
        decision = {"allow": "allow", "deny": "forbidden", "ask": "prompt"}[effect]
        if effect == "ask" and noninteractive(policy):
            decision = "forbidden"
        for _, tool, pattern in parsed_rules(policy, effect):
            if tool != "Bash":
                continue
            prefix = shell_prefix(pattern)
            if prefix is None:
                skipped.append(f"# No exact Codex prefix equivalent: {pattern}")
                continue
            lines.append(
                f"prefix_rule(pattern={json.dumps(prefix)}, decision={json.dumps(decision)})"
            )
    if skipped:
        lines.extend(["", *skipped])
    return "\n".join(lines) + "\n"


def _enable_network_proxy(toml: str) -> str:
    """Enable filtering while preserving existing proxy options and domain rules."""
    table = (
        "features.network_proxy"
        if re.search(r"(?m)^\[features\.network_proxy\]", toml)
        else "features"
    )
    key = "enabled" if table == "features.network_proxy" else "network_proxy"
    pattern = rf"(?ms)^(\[{re.escape(table)}\]\n)(.*?)(?=^\[|\Z)"
    match = re.search(pattern, toml)
    if match:
        body = set_top_level(match.group(2), key, "true")
        return toml[: match.start()] + match.group(1) + body + toml[match.end() :]
    return toml.rstrip() + f"\n\n[{table}]\n{key} = true\n"


def _set_table_scalar(toml: str, table: str, key: str, value: str) -> str:
    """Set a scalar in one TOML table without changing neighboring tables."""
    pattern = rf"(?ms)^(\[{re.escape(table)}\]\n)(.*?)(?=^\[|\Z)"
    match = re.search(pattern, toml)
    if match:
        body = set_top_level(match.group(2), key, value)
        return toml[: match.start()] + match.group(1) + body + toml[match.end() :]
    return toml.rstrip() + f"\n\n[{table}]\n{key} = {value}\n"


def _list_top_level(toml: str, key: str) -> list[str]:
    """Read the strings of a top-level array, e.g. `include_only = ["A", "B"]`.

    Only the section before the first table header is read, matching
    `set_top_level`. The value may span several lines and carry comments,
    literal strings, and a trailing comma; it is parsed with `tomllib`.
    Returns an empty list if the key is absent or its value is not an array.
    """
    head, _ = split_top_level(toml)
    entries = top_level_entries(head, key)
    if not entries:
        return []
    _, value_start, end = entries[0]
    try:
        value = tomllib.loads(f"value = {head[value_start:end]}")["value"]
    except tomllib.TOMLDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


_TOML_KEY = re.compile(r'(?m)^(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_-]+))\s*=')


def _existing_table_keys(toml: str, table: str) -> list[str]:
    """Top-level keys of a named table, e.g. `shell_environment_policy.set`."""
    pattern = rf"(?ms)^\[{re.escape(table)}\]\n(.*?)(?=^\[|\Z)"
    match = re.search(pattern, toml)
    if not match:
        return []
    return [next(g for g in groups if g) for groups in _TOML_KEY.findall(match.group(1))]


# Codex's own "core" shell_environment_policy inherit mode, as observed on
# this install (Codex 0.155.1): the sandboxed child's default environment
# without any include_only filtering. `include_only` is documented as an
# allowlist filtered against whatever `inherit` would have produced, not
# against the full parent environment, so getting a var through it under
# `inherit = "all"` requires this adapter to name every var `inherit =
# "core"` would otherwise have supplied -- confirmed empirically, not from
# public documentation. If a future Codex release changes what "core"
# provides, this list needs updating alongside it; a missing name here
# reads as a working sandbox missing an env var, not a permissions.json
# validation failure, so it fails quiet rather than loud.
_CODEX_CORE_ENV_VARS = (
    "ALL_PROXY", "all_proxy",
    "BUNDLE_HTTP_PROXY", "BUNDLE_HTTPS_PROXY", "BUNDLE_NO_PROXY",
    "CODEX_NETWORK_ALLOW_LOCAL_BINDING", "CODEX_NETWORK_PROXY_ACTIVE", "CODEX_SANDBOX",
    "DOCKER_HTTP_PROXY", "DOCKER_HTTPS_PROXY",
    "ELECTRON_GET_USE_PROXY",
    "FTP_PROXY", "ftp_proxy",
    "GIT_SSH_COMMAND",
    "HOME",
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "LANG", "LOGNAME",
    "NO_PROXY", "no_proxy",
    "NODE_USE_ENV_PROXY",
    "NPM_CONFIG_HTTP_PROXY", "npm_config_http_proxy",
    "NPM_CONFIG_HTTPS_PROXY", "npm_config_https_proxy",
    "NPM_CONFIG_NOPROXY", "npm_config_noproxy",
    "NPM_CONFIG_PROXY", "npm_config_proxy",
    "PATH", "PIP_PROXY", "SHELL", "TMPDIR", "USER",
    "WS_PROXY", "ws_proxy", "WSS_PROXY", "wss_proxy",
    "YARN_HTTP_PROXY", "YARN_HTTPS_PROXY",
)


def _merge_shell_environment_passthrough(toml: str, names: list[str]) -> str:
    """Ensure `[shell_environment_policy]` passes the named variables through.

    `include_only` replaces whatever `inherit` would have provided rather
    than adding to it, so reaching a var declared here requires `inherit =
    "all"` plus every name Codex's own "core" mode would otherwise have
    supplied (`_CODEX_CORE_ENV_VARS`) alongside the requested passthrough
    names. Existing `.set` keys and any names already in `include_only` are
    preserved, since `include_only` filters those too. A bare `include` key
    is not a real Codex passthrough mechanism (confirmed empirically: it has
    no effect once `include_only` is present) and is removed if found, so a
    stale one left by an earlier, since-corrected version of this function
    does not linger.
    """
    table = "shell_environment_policy"
    pattern = rf"(?ms)^(\[{re.escape(table)}\]\n)(.*?)(?=^\[|\Z)"
    match = re.search(pattern, toml)
    body = match.group(2) if match else ""
    merged = list(
        dict.fromkeys(
            list(_CODEX_CORE_ENV_VARS)
            + _existing_table_keys(toml, f"{table}.set")
            + _list_top_level(body, "include_only")
            + list(names)
        )
    )
    body = set_top_level(body, "inherit", json.dumps("all"))
    body = set_top_level(body, "include_only", json.dumps(merged))
    body = set_top_level(body, "include", None)
    if match:
        return toml[: match.start()] + match.group(1) + body + toml[match.end() :]
    header = "" if not toml or toml.endswith("\n") else "\n"
    return toml + header + f"\n[{table}]\n" + body


def _merge_canonical_config(source: Path, target: Path, dry_run: bool) -> list[str]:
    """Render Codex's native non-prompt profile from permissions.json."""
    policy = load_permissions(source)
    if not policy:
        return []
    config_path = target / "config.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    updated = existing
    updated = set_top_level(
        updated, "approval_policy", json.dumps(codex_approval_policy(policy))
    )
    updated = set_top_level(updated, "approvals_reviewer", json.dumps("auto_review"))
    updated = set_top_level(updated, "web_search", json.dumps("live"))
    name = engine_name(source)
    updated = set_top_level(updated, "default_permissions", json.dumps(name))
    # Profiles supersede sandbox_mode. Keeping the old setting disables the
    # generated deny rules, so remove only this adapter-owned compatibility key.
    updated = set_top_level(updated, "sandbox_mode", None)
    updated = re.sub(r"(?m)^web_search_request\s*=.*\n?", "", updated)
    updated = _strip_managed_permissions(updated, name)
    if policy.get("codexNetwork", {}).get("enabled"):
        updated = _enable_network_proxy(updated)
    passthrough = policy.get("codexShellEnvironment", {}).get("passthrough")
    if passthrough:
        updated = _merge_shell_environment_passthrough(updated, passthrough)
    # [history].persistence is the one Codex config key adjacent to rollout
    # retention (verified 2026-09-25, Codex 0.156.1: codex doctor reports a
    # rollout file count and disk size as an informational note only, and
    # neither codex --help nor the binary's embedded default config names any
    # age or count based pruning of ~/.codex/sessions; session logs are kept).
    # "save-all" is already the shipped default,
    # so this is a defensive pin, not a behavior change: it corrects a
    # hand-set persistence = "none" back to keeping every rollout.
    for table, key, value in (
        ("features", "memories", "false"),
        ("memories", "use_memories", "false"),
        ("memories", "generate_memories", "false"),
        ("desktop", "external-agent-import-sync-enabled", "false"),
        ("history", "persistence", '"save-all"'),
    ):
        updated = _set_table_scalar(updated, table, key, value)
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += _codex_permissions_block(policy, name)

    if updated == existing:
        return []
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        config_path.write_text(updated)
    return ["render permissions.json into config.toml"]


_PROJECT_TABLE = re.compile(
    r"(?m)^\s*\[projects\.(?:\"((?:[^\"\\\\]|\\\\.)*)\"|'([^']*)'|([^\]\s]+))\]"
)


def _project_entries(toml: str) -> set[str]:
    """Paths that already have a [projects.<path>] table, whatever their trust level."""
    found: set[str] = set()
    for match in _PROJECT_TABLE.finditer(toml):
        if match.group(1) is not None:
            found.add(match.group(1).replace('\\"', '"').replace("\\\\", "\\"))
        elif match.group(2) is not None:
            found.add(match.group(2))
        else:
            found.add(match.group(3))
    return found


def _trust_repositories(source: Path, target: Path, dry_run: bool) -> list[str]:
    """Append a trusted [projects.<path>] table for every repository under the policy's additional directories.

    Codex keys folder trust on the exact repository root, with no ancestor
    walk, and shows a trust dialog on the first launch in a root that has no
    entry. Existing entries are never rewritten, so a root the user marked
    untrusted stays untrusted, and the append is idempotent. This runs before
    the managed permissions block is regenerated so that block stays last.
    """
    policy = load_permissions(source)
    if not policy:
        return []
    config_path = target / "config.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    present = _project_entries(existing)
    missing = [
        str(root) for root in trusted_repo_roots(policy) if str(root) not in present
    ]
    if not missing:
        return []
    updated = existing if not existing or existing.endswith("\n") else existing + "\n"
    updated += "".join(
        f'\n[projects.{json.dumps(path)}]\ntrust_level = "trusted"\n'
        for path in missing
    )
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        config_path.write_text(updated)
    return [f"trust {len(missing)} repositories in config.toml"]


def _render_codex_rules(source: Path, target: Path, dry_run: bool) -> list[str]:
    """Write the managed rules file alongside untouched user default.rules."""
    policy = load_permissions(source)
    if not policy:
        return []
    rules_name = f"{engine_name(source)}.rules"
    destination = target / "rules" / rules_name
    rendered = _codex_rules(policy)
    if destination.exists() and destination.read_text() == rendered:
        return []
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered)
    return [f"render permissions.json -> rules/{rules_name}"]


def _render_default_models(source: Path, target: Path, dry_run: bool) -> list[str]:
    """runtime_settings.codex model and subagent_model -> config.toml.

    The top-level model key and [agents] default_subagent_model.
    Only a key the manifest sets is written; an unset one leaves the
    operator's own value alone.
    """
    models = default_models(source, "codex")
    if not models["model"] and not models["subagent_model"]:
        return []
    config_path = target / "config.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    updated = existing
    if models["model"]:
        updated = set_top_level(updated, "model", json.dumps(models["model"]))
    if models["subagent_model"]:
        updated = _set_table_scalar(updated, "agents", "default_subagent_model", json.dumps(models["subagent_model"]))
    if updated == existing:
        return []
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        config_path.write_text(updated)
    return ["render components.json runtime_settings.codex models into config.toml"]


def sync(source: Path, target: Path, dry_run: bool = False) -> list[str]:
    """Sync the shared config into a Codex target directory.

    Args:
        source: Path to the shared config repo (the canonical source)
        target: Path to the Codex runtime root (e.g. ~/.codex)
        dry_run: If True, compute actions but do not write

    Returns:
        List of action descriptions
    """
    actions: list[str] = []

    actions.extend(_trust_repositories(source, target, dry_run))
    # The declared foreign registrations go in before the hooks.json merge
    # below, so that merge sees them as foreign groups and places them where it
    # places every other one, in this same pass.
    actions.extend(render_foreign("codex", source, target, dry_run))
    # Before the permissions block is regenerated, so that block stays last
    # (the same reason _trust_repositories runs first).
    actions.extend(render_components("codex", source, target, dry_run))
    actions.extend(_render_default_models(source, target, dry_run))
    actions.extend(_merge_canonical_config(source, target, dry_run))
    actions.extend(_render_codex_rules(source, target, dry_run))

    # 1. AGENTS.md -> target/AGENTS.md (byte copy)
    agents_md = source / "AGENTS.md"
    if agents_md.exists():
        dst = target / "AGENTS.md"
        src_bytes = agents_md.read_bytes()
        needs_copy = not dst.exists() or dst.read_bytes() != src_bytes
        if needs_copy:
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(agents_md, dst)
            actions.append("copy AGENTS.md")

    # 2. hooks/*.sh + hooks/lib/** -> target/hooks/
    #    Exclude *.test.sh and hooks.json. Add and update here; deletion of
    #    a target-only file belongs to sync.py --prune, driven by
    #    owned_outputs below.
    hooks_src = source / "hooks"
    if hooks_src.is_dir():
        hooks_dst = target / "hooks"
        for item in sorted(hooks_src.rglob("*")):
            rel = item.relative_to(hooks_src)
            # Skip hooks.json (handled separately), test files
            if item.name in {
                "hooks.json",
                "claude-worktree-hooks.json",
                "claude-agent-graph-hooks.json",
                "hook-test.sh",
                "opencode-runtime-hooks.ts",
                "retired.json",
            }:
                continue
            if item.name.endswith(".test.sh"):
                continue
            dest = hooks_dst / rel
            if item.is_dir():
                if not dest.is_dir():
                    if not dry_run:
                        dest.mkdir(parents=True, exist_ok=True)
                    actions.append(f"mkdir hooks/{rel}")
            elif item.is_file():
                src_bytes = item.read_bytes()
                needs_copy = True
                if dest.exists():
                    if dest.read_bytes() == src_bytes:
                        # Also check exec bit
                        src_exec = bool(item.stat().st_mode & 0o111)
                        dst_exec = bool(dest.stat().st_mode & 0o111)
                        if src_exec == dst_exec:
                            needs_copy = False
                if needs_copy:
                    if not dry_run:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest)
                        # Preserve exec bit explicitly
                        src_mode = item.stat().st_mode
                        if src_mode & 0o111:
                            dest.chmod(dest.stat().st_mode | (src_mode & 0o111))
                    actions.append(f"copy hooks/{rel}")

    # 3. hooks/hooks.json -> target/hooks.json
    #    Rewrite paths, wrap in {"hooks": {...}}, and MERGE with existing.
    #
    #    Merge rule: For each event key in source, rewrite our hook groups
    #    (those whose commands reference $HOME/.codex/hooks/<source script>)
    #    and replace them in target. Keep any target-native hook groups whose
    #    commands do NOT reference any of our source hook scripts. This
    #    preserves Codex-native hooks (graft/, orca, etc.) while
    #    ensuring our hooks are up to date.
    hooks_json_src = source / "hooks" / "hooks.json"
    if hooks_json_src.exists():
        src_data = json.loads(hooks_json_src.read_text())
        source_names = _source_hook_names(source)

        # Rewrite all command paths in source data
        rewritten: dict = {}
        for event, groups in src_data.items():
            if event in _UNSUPPORTED_EVENTS:
                # A deliberate drop, reported nowhere: an action here would
                # make every sync.py --check read Codex as stale.
                continue
            new_groups = []
            for group in groups:
                new_group = dict(group)
                if "hooks" in new_group:
                    new_hooks = []
                    for hook in new_group["hooks"]:
                        new_hook = dict(hook)
                        if "command" in new_hook:
                            new_hook["command"] = _rewrite_hook_paths(
                                new_hook["command"]
                            )
                        new_hooks.append(new_hook)
                    new_group["hooks"] = new_hooks
                new_groups.append(new_group)
            rewritten[event] = new_groups

        # Load existing target hooks.json if present
        target_hooks_path = target / "hooks.json"
        existing: dict = {}
        if target_hooks_path.exists():
            try:
                raw = json.loads(target_hooks_path.read_text())
                # Codex wraps in {"hooks": {...}}
                existing = raw.get("hooks", raw)
            except (json.JSONDecodeError, AttributeError):
                existing = {}

        # A preserved foreign group may still name one of our shipped
        # scripts from the wrong directory (the "mislocated" disposition).
        # Repoint that one command onto our own hooks directory; the rest
        # of the group, including any existence guard, is left untouched.
        #
        # A live timeout also needed fixing: the foreign
        # graft-hooks.cjs entry carries 10000 beside neighbours in
        # seconds. The Codex hook contract uses seconds throughout
        # (confirmed against the published hook reference,
        # https://learn.chatgpt.com/docs/hooks: "timeout is in seconds"),
        # so 10000 is a mistaken milliseconds figure for that one entry.
        # Only this named command and value are touched; no other
        # timeout, ours or foreign, is converted.
        _GRAFT_TIMEOUT_MARKER = "graft-hooks.cjs"
        _GRAFT_TIMEOUT_WRONG = 10000
        _GRAFT_TIMEOUT_RIGHT = 10

        def _repoint_group(group):
            if not isinstance(group, dict):
                return group
            hooks_list = group.get("hooks")
            if not isinstance(hooks_list, list):
                return group
            new_hooks = []
            for h in hooks_list:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    h = dict(h)
                    h["command"] = repoint_command(
                        h["command"],
                        source_names=source_names,
                        hooks_prefix=_HOOKS_PREFIX,
                        hooks_dir=target / "hooks",
                    )
                    if (
                        _GRAFT_TIMEOUT_MARKER in h["command"]
                        and h.get("timeout") == _GRAFT_TIMEOUT_WRONG
                    ):
                        h["timeout"] = _GRAFT_TIMEOUT_RIGHT
                new_hooks.append(h)
            return {**group, "hooks": new_hooks}

        # Merge: for each event, keep foreign groups + replace ours
        merged: dict = {}
        all_events = set(list(rewritten.keys()) + list(existing.keys()))
        for event in sorted(all_events):
            existing_groups = existing.get(event, [])
            source_groups = rewritten.get(event, [])

            # Keep foreign groups (those not referencing our source hooks)
            foreign = [
                _repoint_group(g)
                for g in existing_groups
                if not _group_is_ours(g, source_names)
            ]
            # Our groups come from source (already rewritten)
            merged[event] = source_groups + foreign

        new_content = json.dumps({"hooks": merged}, indent=2) + "\n"
        old_content = (
            target_hooks_path.read_text() if target_hooks_path.exists() else None
        )
        if old_content != new_content:
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                target_hooks_path.write_text(new_content)
            actions.append("merge hooks.json (rewrite paths, wrap, merge)")

    # 4. skills/ -> target/skills/ (add/update here; a target-only skill
    #    directory is an orphan pruned per the ownership record, not this
    #    loop). Shared helper honors skills/sync-exclude.json and skips
    #    unchanged files.
    skills_src = source / "skills"
    if skills_src.is_dir():
        actions.extend(mirror_skills(skills_src, target / "skills", dry_run))

    # 5. agents/*.md -> target/agents/<name>.toml
    #    Translate frontmatter+body to TOML. Replace CLAUDE.md -> AGENTS.md.
    #    A target-only agent, including one whose .md source is gone, is
    #    an orphan pruned by sync.py --prune, not deleted by this loop.
    agents_src = source / "agents"
    if agents_src.is_dir():
        agents_dst = target / "agents"
        aliases = agent_aliases(source, "codex")
        for md_file in sorted(agents_src.glob("*.md")):
            text = md_file.read_text()
            fm, body = read_frontmatter(text)
            if not fm.get("name"):
                fm["name"] = md_file.stem
            toml_content = _agent_md_to_toml(fm, body, aliases)
            toml_path = agents_dst / f"{md_file.stem}.toml"
            if toml_path.exists() and toml_path.read_text() == toml_content:
                continue
            if not dry_run:
                agents_dst.mkdir(parents=True, exist_ok=True)
                toml_path.write_text(toml_content)
            actions.append(
                f"translate agents/{md_file.stem}.md -> agents/{md_file.stem}.toml"
            )

    # 6. commands/*.md -> target/prompts/<name>.md
    #    Codex prompts/ uses the same markdown format with YAML frontmatter.
    #    Both use: description, argument-hint in frontmatter; body is the prompt.
    #    Byte-compatible, so we copy directly. A target-only prompt is an
    #    orphan pruned by sync.py --prune, not deleted by this loop.
    commands_src = source / "commands"
    if commands_src.is_dir():
        prompts_dst = target / "prompts"
        for cmd_file in sorted(commands_src.glob("*.md")):
            dst = prompts_dst / cmd_file.name
            src_bytes = cmd_file.read_bytes()
            if dst.exists() and dst.read_bytes() == src_bytes:
                continue
            if not dry_run:
                prompts_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cmd_file, dst)
            actions.append(f"copy commands/{cmd_file.name} -> prompts/{cmd_file.name}")

    # 7. Never write rules/default.rules or other runtime state.

    return actions


def owned_outputs(source: Path, target: Path) -> list:
    """Directories this adapter owns in a Codex target tree.

    Computed from source by the same name mapping sync() itself applies, so a
    file sync() would write is never reported as an orphan by the
    sync.py --check reverse pass.

    target/rules/ is deliberately not covered: sync() only ever replaces
    the one generated rules/<name>.rules file there and never touches
    the runtime-native rules/default.rules beside it, so scanning that
    directory for orphans would misclassify the user's own command rules
    as prunable content. config.toml's managed sections are likewise a
    single runtime-native file with owned keys, not a directory of owned
    names, so it is not covered here either.

    target/skills/.system/ is Codex's own: the runtime installs its bundled
    system skills there and reinstalls them itself. It is reported as
    excluded, never as an orphan, so --prune never deletes it.
    """
    owned = [
        OwnedDir(
            "hook",
            target / "hooks",
            mirrored_names(source / "hooks", exclude_patterns=_HOOK_MIRROR_EXCLUDES),
        ),
        OwnedDir(
            "agent",
            target / "agents",
            frozenset(f"{p.stem}.toml" for p in (source / "agents").glob("*.md")),
        ),
        OwnedDir(
            "prompt",
            target / "prompts",
            frozenset(p.name for p in (source / "commands").glob("*.md")),
        ),
    ]
    skills_src = source / "skills"
    if skills_src.is_dir():
        excluded = skill_excludes(skills_src)
        names = frozenset(
            p.name for p in skills_src.iterdir() if p.is_dir() and p.name not in excluded
        )
        owned.append(
            OwnedDir(
                "skill",
                target / "skills",
                names,
                per_item=True,
                exclude=frozenset(excluded) | _RUNTIME_SKILL_DIRS,
            )
        )
    return owned


def external_inventory(target: Path) -> list[Item]:
    """Every external item Codex holds: the [mcp_servers.*] and [plugins.*]
    tables of config.toml, per the inventory record's table, plus every
    skill directory under ~/.agents/skills.

    Codex also lists skills from ~/.agents/skills, a second root
    no adapter here writes. A name found only there is a foreign copy of whatever this
    repository already delivers to ~/.codex/skills, reported undeclared
    unless a third_party_skills entry names it for codex; nothing here
    deletes it. An MCP server's env and http_headers values are carried for
    the literal-secret scan.
    """
    runtime = RUNTIME["name"]
    config = target / "config.toml"
    data, error = read_toml(config)
    if error:
        items = [unreadable(runtime, "mcp_server", config, error)]
    elif not isinstance(data, dict):
        items = []
    else:
        items = mcp_items(runtime, data.get("mcp_servers"), config, "mcp_servers")
        plugins = data.get("plugins")
        if isinstance(plugins, dict):
            items.extend(Item(runtime, "plugin", str(key), str(config)) for key in sorted(plugins))
        elif plugins is not None:
            items.append(unreadable(runtime, "plugin", config, "plugins is not a table"))
    agents_skills = target.parent / ".agents" / "skills"
    if agents_skills.is_dir():
        try:
            found = sorted(p.name for p in agents_skills.iterdir() if p.is_dir())
        except OSError as skill_error:
            items.append(unreadable(runtime, "skill", agents_skills, f"cannot be listed: {type(skill_error).__name__}"))
        else:
            items.extend(Item(runtime, "skill", name, str(agents_skills / name)) for name in found)
    return items
