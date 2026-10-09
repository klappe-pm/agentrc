"""Gemini CLI runtime adapter.

Syncs the shared config into $HOME/.gemini/ format.

Verified against:
  - https://geminicli.com/docs/hooks (hook events reference)
  - https://geminicli.com/docs/hooks/reference (settings.json schema)
  - https://geminicli.com/docs/cli/custom-commands/ (TOML command format)
  - Gemini CLI 0.56.0's own bundle for agents: user agents load from
    ~/.gemini/agents/*.md with a strict frontmatter schema (verified
    2026-09-23, bundle/chunk-LZUWGCRJ.js Storage.getUserAgentsDir and
    localAgentSchema, run through its loadAgentsFromDirectory)

Event mapping (Claude Code -> Gemini CLI):
  PreToolUse       -> BeforeTool
  PostToolUse      -> AfterTool
  SessionStart     -> SessionStart
  SessionEnd       -> SessionEnd
  Stop             -> AfterAgent
  Notification     -> Notification
  UserPromptSubmit -> BeforeAgent
  PreCompact       -> PreCompress

Dropped (no Gemini equivalent):
  SubagentStart, SubagentStop, PostToolUseFailure,
  WorktreeCreate, WorktreeRemove
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import shared helpers; fall back to private copies if _common.py is not yet
# written by the sibling agent.
# ---------------------------------------------------------------------------
from ._common import (
    OwnedDir,
    is_claude_model,
    mirror_dir,
    mirror_skills,
    mirrored_names,
    read_frontmatter,
    repoint_command,
    skill_excludes,
    source_hook_names,
)
from ._components import agent_aliases, alias_for, default_models, render as render_components
from ._foreign import render as render_foreign
from ._inventory import Item, list_dir, mcp_from_json_file, unreadable
from stratarc.paths import engine_name
from stratarc.permissions import (
    gemini_approval_mode,
    gemini_policy_toml,
    load as load_permissions,
)

# This runtime's entry in the runtime registry (_common.runtime_registry).
RUNTIME = {
    "name": "gemini",
    "target": ".gemini",
    "hook_registry": "settings.json",
    # PostToolUseFailure, SubagentStart and SubagentStop are declared as
    # deliberate drops: UNMAPPABLE_EVENTS below skips them.
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

EVENT_MAP: dict[str, str] = {
    "PreToolUse": "BeforeTool",
    "PostToolUse": "AfterTool",
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
    "Stop": "AfterAgent",
    "Notification": "Notification",
    "UserPromptSubmit": "BeforeAgent",
    "PreCompact": "PreCompress",
}

# Events with no Gemini equivalent -- silently dropped with a note.
UNMAPPABLE_EVENTS: set[str] = {
    "SubagentStart",
    "SubagentStop",
    "PostToolUseFailure",
    "WorktreeCreate",
    "WorktreeRemove",
}

_CLAUDE_HOOK_PREFIX = "$HOME/.claude/hooks/"
_GEMINI_HOOK_PREFIX = "$HOME/.gemini/hooks/"
_HOOK_MIRROR_EXCLUDES = [
    "*.test.sh",
    "hook-test.sh",
    "hooks.json",
    "claude-worktree-hooks.json",
    "claude-agent-graph-hooks.json",
    "opencode-runtime-hooks.ts",
    "retired.json",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _rewrite_command(cmd: str) -> str:
    """Rewrite $HOME/.claude/hooks/ references to $HOME/.gemini/hooks/."""
    return cmd.replace(_CLAUDE_HOOK_PREFIX, _GEMINI_HOOK_PREFIX)


def _convert_hook_group(group: dict[str, Any]) -> dict[str, Any]:
    """Convert a single hook group: rewrite paths, convert timeout s->ms."""
    out: dict[str, Any] = {}
    if "matcher" in group:
        out["matcher"] = group["matcher"]
    hooks_list = []
    for h in group.get("hooks", []):
        new_h: dict[str, Any] = {}
        for k, v in h.items():
            if k == "command":
                new_h[k] = _rewrite_command(v)
            elif k == "timeout":
                # Claude uses seconds, Gemini uses milliseconds
                new_h[k] = v * 1000
            elif k == "statusMessage":
                # Gemini hooks don't have statusMessage; drop it
                continue
            else:
                new_h[k] = v
        hooks_list.append(new_h)
    out["hooks"] = hooks_list
    return out


def _is_source_managed_group(group: dict[str, Any]) -> bool:
    """True if any command in the group references our hook dir."""
    for h in group.get("hooks", []):
        cmd = h.get("command", "")
        if _GEMINI_HOOK_PREFIX in cmd:
            return True
    return False


def _deep_merge(existing: object, desired: object) -> object:
    """Merge canonical settings without discarding runtime-native settings."""
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        return desired
    merged = dict(existing)
    for key, value in desired.items():
        merged[key] = _deep_merge(merged.get(key), value)
    return merged


def _repoint_foreign_group(
    group: dict[str, Any], *, source_names: set[str], hooks_dst: Path
) -> dict[str, Any]:
    """Repoint a preserved foreign group's mislocated source scripts.

    A foreign group may still name one of our shipped scripts from the
    wrong directory. That one command is rewritten onto our own hooks
    directory; everything else in the group, including an existence
    guard, is left untouched.
    """
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
                hooks_prefix=_GEMINI_HOOK_PREFIX.rstrip("/"),
                hooks_dir=hooks_dst,
            )
        new_hooks.append(h)
    return {**group, "hooks": new_hooks}


def _merge_hooks(
    source_hooks: dict[str, list[dict[str, Any]]],
    existing_hooks: dict[str, list[dict[str, Any]]],
    *,
    source_names: set[str] | None = None,
    hooks_dst: Path | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Merge converted source hooks into existing Gemini hooks.

    Preserves existing hook groups whose commands do NOT reference
    $HOME/.gemini/hooks/<source hook name>.  Returns (merged, actions).
    """
    merged: dict[str, list[dict[str, Any]]] = {}
    actions: list[str] = []

    # Collect all Gemini event names that have source-managed content
    gemini_events_from_source: set[str] = set()

    # Convert source hooks
    converted: dict[str, list[dict[str, Any]]] = {}
    for claude_event, groups in source_hooks.items():
        if claude_event in UNMAPPABLE_EVENTS:
            actions.append(f"drop event {claude_event} (no Gemini equivalent)")
            continue
        gemini_event = EVENT_MAP.get(claude_event)
        if gemini_event is None:
            actions.append(f"drop event {claude_event} (unknown)")
            continue
        gemini_events_from_source.add(gemini_event)
        converted_groups = [_convert_hook_group(g) for g in groups]
        if gemini_event in converted:
            converted[gemini_event].extend(converted_groups)
        else:
            converted[gemini_event] = converted_groups
        actions.append(f"map {claude_event} -> {gemini_event}")

    # For each Gemini event, build final list: source-managed groups from
    # converted data, plus foreign groups from existing config.
    all_events = set(converted.keys()) | set(existing_hooks.keys())
    for event in sorted(all_events):
        groups: list[dict[str, Any]] = []
        # Foreign groups from existing config (not source-managed)
        for g in existing_hooks.get(event, []):
            if not _is_source_managed_group(g):
                if source_names is not None and hooks_dst is not None:
                    g = _repoint_foreign_group(
                        g, source_names=source_names, hooks_dst=hooks_dst
                    )
                groups.append(g)
        # Source-managed groups
        if event in converted:
            groups.extend(converted[event])
        if groups:
            merged[event] = groups

    return merged, actions


def _escape_toml_multiline(s: str) -> str:
    """Escape a string for use in a TOML multi-line basic string (triple-quote).

    TOML triple-quoted strings allow most content verbatim.  We only need to
    escape sequences of 2+ consecutive quotes that might close the delimiter
    early, and backslashes that could form escape sequences.
    """
    # Backslashes need escaping in basic strings
    s = s.replace("\\", "\\\\")
    # Three or more consecutive quotes would close the string
    # Replace runs of 3+ quotes by inserting a backslash-newline break
    # Actually in TOML basic multi-line strings, we can use \" to escape
    # Just ensure no triple-quote sequence appears
    while '"""' in s:
        s = s.replace('"""', '""\\"')
    return s


# Claude Code tool names and the Gemini CLI built-in tool each one maps to,
# from the tool name constants in Gemini CLI 0.56.0 (verified 2026-09-23,
# bundle/chunk-LZUWGCRJ.js READ_FILE_TOOL_NAME, GREP_TOOL_NAME, GLOB_TOOL_NAME,
# SHELL_TOOL_NAME, EDIT_TOOL_NAME, WRITE_FILE_TOOL_NAME, WEB_FETCH_TOOL_NAME,
# WEB_SEARCH_TOOL_NAME, LS_TOOL_NAME). A Claude tool with no Gemini
# counterpart (NotebookEdit, Task) is left out, since the loader refuses an
# unknown tool name.
_GEMINI_TOOLS: dict[str, str] = {
    "read": "read_file",
    "grep": "grep_search",
    "glob": "glob",
    "bash": "run_shell_command",
    "edit": "replace",
    "multiedit": "replace",
    "write": "write_file",
    "webfetch": "web_fetch",
    "websearch": "google_web_search",
    "ls": "list_directory",
}


def _gemini_tools(value: object) -> list[str]:
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        names = [str(part).strip() for part in value]
    else:
        return []
    return list(dict.fromkeys(_GEMINI_TOOLS[n.lower()] for n in names if n.lower() in _GEMINI_TOOLS))


def _agent_md(stem: str, md_text: str, aliases: dict | None = None) -> str:
    """Translate a source agent into the frontmatter Gemini CLI validates.

    Gemini's localAgentSchema is strict: name (a slug), description
    (non-empty), tools (an array of Gemini tool names), model, temperature,
    max_turns, timeout_mins, mcp_servers, display_name and kind, nothing
    else. A byte copy of a source agent fails to load with "tools: Expected
    array, received string" and "Unrecognized key(s) in object: 'color'"
    (verified 2026-09-23, Gemini CLI 0.56.0 loadAgentsFromDirectory). So
    color is dropped, tools are mapped, and a Claude model name is dropped so
    the agent inherits the session model, unless runtime_settings.gemini.aliases
    in components.json maps it to a Gemini model. Values are written as
    JSON, which is valid YAML.
    """
    meta, body = read_frontmatter(md_text)
    name = str(meta.get("name") or stem).strip().lower()
    description = str(meta.get("description") or name).replace("CLAUDE.md", "GEMINI.md")
    lines = ["---", f"name: {json.dumps(name)}", f"description: {json.dumps(description)}"]
    tools = _gemini_tools(meta.get("tools"))
    if tools:
        lines.append(f"tools: {json.dumps(tools)}")
    model = meta.get("model")
    aliased = alias_for(model, aliases)
    if aliased:
        lines.append(f"model: {json.dumps(aliased)}")
    elif not is_claude_model(model):
        lines.append(f"model: {json.dumps(str(model).strip())}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.replace("CLAUDE.md", "GEMINI.md")


def _md_to_toml(name: str, md_text: str) -> str:
    """Convert a Claude command .md file to Gemini .toml format."""
    meta, body = read_frontmatter(md_text)
    description = meta.get("description", name)
    prompt = body.strip()
    escaped_prompt = _escape_toml_multiline(prompt)

    lines = []
    lines.append(f'description = "{description}"')
    lines.append(f'prompt = """\n{escaped_prompt}\n"""')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync(source: Path, target: Path, dry_run: bool = False) -> list[str]:
    """Sync the shared config at *source* into Gemini CLI root at *target*.

    Returns a list of human-readable action descriptions.
    """
    actions: list[str] = []
    settings_dst = target / "settings.json"

    # 0. permissions.json renders both Gemini permission surfaces. The settings
    # file controls the default UI mode and the policy file controls rules.
    policy = load_permissions(source)
    if policy:
        desired_settings = {
            "general": {"defaultApprovalMode": gemini_approval_mode(policy)}
        }
        existing_settings = (
            json.loads(settings_dst.read_text()) if settings_dst.exists() else {}
        )
        merged_settings = _deep_merge(existing_settings, desired_settings)
        if merged_settings != existing_settings:
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                settings_dst.write_text(json.dumps(merged_settings, indent=2) + "\n")
            actions.append("render permissions.json into settings.json")

    # 0b. runtime_settings.gemini.model in components.json -> model.name
    # (model selection for this runtime).
    # Gemini CLI has no runtime-wide subagent model key; notes() names one set.
    model = default_models(source, "gemini")["model"]
    if model:
        existing_settings = json.loads(settings_dst.read_text()) if settings_dst.exists() else {}
        merged_settings = _deep_merge(existing_settings, {"model": {"name": model}})
        if merged_settings != existing_settings:
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                settings_dst.write_text(json.dumps(merged_settings, indent=2) + "\n")
            actions.append("render components.json runtime_settings.gemini model into settings.json")

    if policy:
        policy_name = f"{engine_name(source)}-permissions.toml"
        policy_dst = target / "policies" / policy_name
        rendered_policy = gemini_policy_toml(policy).encode()
        needs_copy = (
            not policy_dst.exists() or policy_dst.read_bytes() != rendered_policy
        )
        if needs_copy:
            if not dry_run:
                policy_dst.parent.mkdir(parents=True, exist_ok=True)
                policy_dst.write_bytes(rendered_policy)
            actions.append(f"render permissions.json -> policies/{policy_name}")

    # 1. AGENTS.md -> target/GEMINI.md (byte copy)
    agents_src = source / "AGENTS.md"
    gemini_dst = target / "GEMINI.md"
    if agents_src.exists():
        src_bytes = agents_src.read_bytes()
        needs_copy = not gemini_dst.exists() or gemini_dst.read_bytes() != src_bytes
        if needs_copy:
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(agents_src, gemini_dst)
            actions.append("copy AGENTS.md -> GEMINI.md")

    # 2. hooks/*.sh + hooks/lib/** -> target/hooks/
    hooks_src = source / "hooks"
    hooks_dst = target / "hooks"
    if hooks_src.is_dir():
        if not dry_run:
            hooks_dst.mkdir(parents=True, exist_ok=True)
        acts = mirror_dir(
            hooks_src,
            hooks_dst,
            dry_run=dry_run,
            delete_extra=False,
            exclude_patterns=_HOOK_MIRROR_EXCLUDES,
        )
        for a in acts:
            actions.append(f"hooks: {a}")

    # 3. hooks/hooks.json -> merge into target/settings.json. The declared
    # foreign registrations go in first, so the merge below sees them as
    # foreign groups and places them where it places every other one, in this
    # same pass.
    actions.extend(render_foreign("gemini", source, target, dry_run))
    hooks_json_src = source / "hooks" / "hooks.json"
    if hooks_json_src.exists():
        source_hooks = json.loads(hooks_json_src.read_text())
        existing_settings: dict[str, Any] = {}
        if settings_dst.exists():
            existing_settings = json.loads(settings_dst.read_text())
        existing_hooks = existing_settings.get("hooks", {})
        merged_hooks, hook_actions = _merge_hooks(
            source_hooks,
            existing_hooks,
            source_names=source_hook_names(source),
            hooks_dst=hooks_dst,
        )

        new_settings = dict(existing_settings)
        new_settings["hooks"] = merged_hooks
        new_content = json.dumps(new_settings, indent=2) + "\n"
        old_content = settings_dst.read_text() if settings_dst.exists() else None
        if old_content != new_content:
            actions.extend(hook_actions)
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                settings_dst.write_text(new_content)
            actions.append("write settings.json")

    # 4. skills/ -> target/skills/ (add/update, never delete)
    skills_src = source / "skills"
    skills_dst = target / "skills"
    if skills_src.is_dir():
        acts = mirror_skills(skills_src, skills_dst, dry_run=dry_run)
        for a in acts:
            actions.append(f"skills: {a}")

    # 5. commands/*.md -> target/commands/<name>.toml
    cmds_src = source / "commands"
    cmds_dst = target / "commands"
    if cmds_src.is_dir():
        for md_file in sorted(cmds_src.glob("*.md")):
            name = md_file.stem
            toml_path = cmds_dst / f"{name}.toml"
            md_text = md_file.read_text()
            toml_text = _md_to_toml(name, md_text)
            if toml_path.exists() and toml_path.read_text() == toml_text:
                continue
            if not dry_run:
                cmds_dst.mkdir(parents=True, exist_ok=True)
                toml_path.write_text(toml_text)
            actions.append(f"command {name}.md -> {name}.toml")

    # 6. agents/*.md -> target/agents/<name>.md in Gemini's own shape.
    #    Gemini CLI loads user agents from ~/.gemini/agents/ with no
    #    acknowledgment step, while experimental.enableAgents is true, its
    #    default (verified 2026-09-23, Gemini CLI 0.56.0 AgentRegistry.loadAgents).
    agents_src = source / "agents"
    agents_dst = target / "agents"
    if agents_src.is_dir():
        aliases = agent_aliases(source, "gemini")
        for md_file in sorted(agents_src.glob("*.md")):
            content = _agent_md(md_file.stem, md_file.read_text(), aliases)
            dst = agents_dst / md_file.name
            if dst.exists() and dst.read_text() == content:
                continue
            if not dry_run:
                agents_dst.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)
            actions.append(f"agent {md_file.name} -> agents/{md_file.name}")

    # 7. components.json MCP servers the global column selects -> mcpServers
    # in settings.json (adapters/_components.py). Last, after every
    # other settings.json write, so it reads the file those steps left.
    actions.extend(render_components("gemini", source, target, dry_run))

    return actions


def owned_outputs(source: Path, target: Path) -> list[OwnedDir]:
    """Directories this adapter owns in a Gemini CLI target tree.

    Computed from source by the same name mapping sync() itself applies, so a
    file sync() would write is never reported as an orphan by the
    sync.py --check reverse pass. Gemini CLI has no owned rules directory
    (the rules digest reaches it through GEMINI.md instead), so none is
    covered here.
    """
    owned = [
        OwnedDir(
            "hook",
            target / "hooks",
            mirrored_names(source / "hooks", exclude_patterns=_HOOK_MIRROR_EXCLUDES),
        ),
        OwnedDir(
            "command",
            target / "commands",
            frozenset(f"{p.stem}.toml" for p in (source / "commands").glob("*.md")),
        ),
        OwnedDir(
            "agent",
            target / "agents",
            frozenset(p.name for p in (source / "agents").glob("*.md")),
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
                exclude=frozenset(excluded),
            )
        )
    return owned


def external_inventory(target: Path) -> list[Item]:
    """Every external item Gemini CLI holds: mcpServers in settings.json and
    each extension directory under extensions/, per the inventory record's
    table. Files beside the extension directories (extension-enablement.json)
    are Gemini CLI's own state, not extensions."""
    runtime = RUNTIME["name"]
    items = mcp_from_json_file(runtime, target / "settings.json", "mcpServers")
    extensions = target / "extensions"
    names, error = list_dir(extensions, dirs_only=True)
    if error:
        items.append(unreadable(runtime, "extension", extensions, error))
    items.extend(Item(runtime, "extension", name, str(extensions / name)) for name in names)
    return items
