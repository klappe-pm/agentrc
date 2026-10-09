"""OpenCode runtime adapter -- syncs the shared config into ~/.config/opencode/.

Exposes:
    sync(source: Path, target: Path, dry_run: bool = False) -> list[str]

OpenCode formats:
  - opencode.json / opencode.jsonc: global config, validated strictly: an
    unknown top-level key makes OpenCode refuse the whole file (verified
    2026-09-23, OpenCode 1.4.2 `opencode debug config`; schema
    https://opencode.ai/config.json, Config additionalProperties false)
  - permission: an object keyed by tool, not the V2 "permissions" array
    this adapter used to write (verified 2026-09-23, same sources)
  - commands/<name>.md: YAML frontmatter (description, agent, model) + body
    https://opencode.ai/v2/docs/commands
  - agents/<name>.md: frontmatter with description, mode, model as
    provider/model-id, color as a hex or theme color, and permission
    (verified 2026-09-23, AgentConfig in https://opencode.ai/config.json and
    https://github.com/sst/opencode/blob/dev/packages/web/src/content/docs/agents.mdx)
  - skill/<name>/SKILL.md: same SKILL.md convention (name+description in
    frontmatter, body = instructions)
    https://opencode.ai/docs/skills/
  - plugins/: JS/TS plugin files -- NOT shell hooks; never touched
    https://opencode.ai/docs/plugins
  - instructions: config array for extra instruction files; AGENTS.md is
    the active surface
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Import shared helpers; fall back to inline copies if sibling agent hasn't
# finished writing _common.py yet.
# ---------------------------------------------------------------------------
from ._common import OwnedDir, mirror_skills, read_frontmatter, skill_excludes
from ._components import agent_aliases, alias_for, default_models, render as render_components, runtime_settings
from ._inventory import Item, list_dir, mcp_items, read_jsonc, unreadable
from ._text import strip_jsonc_comments
from agentrc.paths import engine_name
from agentrc.permissions import load as load_permissions, opencode_permission, same_in_order

# Rule tier selection comes from rules/tiers.json, the same manifest the
# source rule digest renders from, so the two never name different tiers.
from agentrc.rules_digest import load_tiers

# This runtime's entry in the runtime registry (_common.runtime_registry).
# OpenCode has no JSON hook registry (a local plugin instead), so the reverse
# pass reads none; UserPromptSubmit and SessionStart are dropped inside sync().
# The bridge (hooks/opencode-runtime-hooks.ts) carries the lifecycle events itself:
# SubagentStart on a child session's session.created, SubagentStop when that
# child goes idle or is deleted. PostToolUseFailure has no OpenCode event and
# is dropped; OpenCode keeps counting tool calls at tool.execute.before.
RUNTIME = {
    "name": "opencode",
    "target": ".config/opencode",
    "hook_registry": None,
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

# Claude tool names and the OpenCode permission key that gates each one
# (verified 2026-09-23, the permission key table in
# https://github.com/sst/opencode/blob/dev/packages/web/src/content/docs/agents.mdx).
_TOOL_PERMISSION_KEY: Dict[str, str] = {
    "read": "read",
    "grep": "grep",
    "glob": "glob",
    "bash": "bash",
    "write": "edit",
    "edit": "edit",
    "multiedit": "edit",
    "webfetch": "webfetch",
    "websearch": "websearch",
}
_RESTRICTABLE_KEYS = ("read", "edit", "grep", "glob", "bash", "webfetch", "websearch")
_THEME_COLORS = frozenset({"primary", "secondary", "accent", "success", "warning", "error", "info"})


def _translate_tools_to_permission(tools_value: object) -> Dict[str, str]:
    """Turn a Claude tools allowlist into an OpenCode agent permission object.

    Claude's `tools:` names the only tools an agent may use. OpenCode merges
    an agent's permission over the global one with the agent's rules taking
    precedence, so allowing the listed tools there would widen the global
    policy (a global bash deny would lose to an agent-level bash allow).
    The listed tools therefore get no entry and inherit the global rules,
    and each core tool left out is denied. No tools key means no restriction.
    """
    if isinstance(tools_value, str):
        names = [t.strip() for t in tools_value.split(",")]
    elif isinstance(tools_value, list):
        names = [str(t).strip() for t in tools_value]
    else:
        return {}
    allowed = {_TOOL_PERMISSION_KEY[n.lower()] for n in names if n.lower() in _TOOL_PERMISSION_KEY}
    return {key: "deny" for key in _RESTRICTABLE_KEYS if key not in allowed}


def _opencode_model(value: object) -> Optional[str]:
    """A model OpenCode can resolve: provider/model-id, never a Claude alias."""
    text = str(value or "").strip()
    return text if "/" in text else None


def _opencode_color(value: object) -> Optional[str]:
    """A hex color or an OpenCode theme color; Claude's color names are neither."""
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text) or text in _THEME_COLORS:
        return text
    return None


def _build_agent_frontmatter(fm: Dict[str, object], aliases: Optional[Dict[str, str]] = None) -> str:
    """Build OpenCode agent frontmatter, each value written as JSON (valid YAML).

    AgentConfig in the OpenCode schema (verified 2026-09-23,
    https://opencode.ai/config.json) takes description, mode, model as
    provider/model-id, color as "#rrggbb" or a theme color, and a permission
    object. A Claude model alias (opus, inherit) and a Claude color name
    (green) fit neither shape, so they are left out and the agent inherits,
    unless runtime_settings.opencode.aliases in components.json maps the
    model name.
    """
    lines: List[str] = ["---"]

    if "description" in fm:
        lines.append(f"description: {json.dumps(str(fm['description']))}")

    # mode: subagent for all synced agents (they are invocable helpers)
    lines.append(f"mode: {json.dumps('subagent')}")

    model = alias_for(fm.get("model"), aliases) or _opencode_model(fm.get("model"))
    if model:
        lines.append(f"model: {json.dumps(model)}")

    color = _opencode_color(fm.get("color"))
    if color:
        lines.append(f"color: {json.dumps(color)}")

    permission = _translate_tools_to_permission(fm.get("tools"))
    if permission:
        lines.append(f"permission: {json.dumps(permission)}")

    lines.append("---")
    return "\n".join(lines) + "\n"


def _translate_command_frontmatter(fm: Dict[str, object]) -> str:
    """Build OpenCode command YAML frontmatter.

    OpenCode command markdown frontmatter fields (V2):
      description, agent, model, subtask
    Ref: https://opencode.ai/v2/docs/commands

    Claude's allowed-tools has no equivalent in OpenCode commands and is dropped.
    argument-hint has no equivalent either; dropped.
    """
    lines: List[str] = ["---"]

    if "description" in fm:
        desc = str(fm["description"])
        if ":" in desc or "#" in desc:
            lines.append(f'description: "{desc}"')
        else:
            lines.append(f"description: {desc}")

    if "model" in fm:
        lines.append(f"model: {fm['model']}")

    # agent is kept if present (same key name in both runtimes)
    if "agent" in fm:
        lines.append(f"agent: {fm['agent']}")

    # allowed-tools: dropped (no OpenCode equivalent)
    # argument-hint: dropped (no OpenCode equivalent)

    lines.append("---")
    return "\n".join(lines) + "\n"


def merge_provider_entries(config_path: Path, updates: Dict[str, Optional[Dict[str, Any]]], dry_run: bool) -> bool:
    """Merge updates into config_path's "provider" key, one id at a time,
    preserving everything else in the file.

    updates maps a provider id to its desired block, or to None to remove
    that id. A provider id updates does not mention, whether written by hand
    or by another tool, is left alone. Returns whether anything changed.
    Shared by sync()'s runtime_settings.opencode.provider render and
    the project sync's per-project opt-in to a custom
    OpenAI-compatible provider, so a project's own checkout-root
    opencode.jsonc and the global one are never merged two different ways.
    """
    existing_config: Dict[str, Any] = {}
    if config_path.exists():
        existing_config = json.loads(strip_jsonc_comments(config_path.read_text()))
    existing_provider = existing_config.get("provider")
    existing_provider = existing_provider if isinstance(existing_provider, dict) else {}
    merged_provider = dict(existing_provider)
    for provider_id, entry in updates.items():
        if entry is None:
            merged_provider.pop(provider_id, None)
        else:
            merged_provider[provider_id] = entry
    if merged_provider == existing_provider:
        return False
    updated_config = dict(existing_config)
    if merged_provider:
        updated_config["provider"] = merged_provider
    else:
        updated_config.pop("provider", None)
    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(updated_config, indent=2) + "\n")
    return True


def read_provider_entry(config_path: Path, provider_id: str) -> Optional[Dict[str, Any]]:
    """The current provider.<provider_id> block in config_path, or None when
    the file does not exist or carries no such id.

    Shares merge_provider_entries's own JSONC parsing so a caller checking
    what is already there before deciding to remove it (the project sync's
    per-project opt-in to a custom OpenAI-compatible provider, which must
    never delete a block it did not itself deliver) never reads the file a
    second, different way. A malformed config raises the same json.loads
    error merge_provider_entries would, so a caller iterating many projects
    can catch it once and skip just that one (issue #315) rather than
    treating an unparseable file as "no entry".
    """
    if not config_path.exists():
        return None
    data = json.loads(strip_jsonc_comments(config_path.read_text()))
    provider = data.get("provider") if isinstance(data, dict) else None
    entry = provider.get(provider_id) if isinstance(provider, dict) else None
    return entry if isinstance(entry, dict) else None


def sync(source: Path, target: Path, dry_run: bool = False) -> list[str]:
    """Sync the shared config into an OpenCode target directory.

    Args:
        source: Path to the shared config repo (the canonical source)
        target: Path to the OpenCode runtime root (e.g. ~/.config/opencode)
        dry_run: If True, compute actions but do not write

    Returns:
        List of action descriptions
    """
    actions: list[str] = []

    # 0. Render the canonical policy into OpenCode's "permission" object,
    # replacing it whole, and remove the V2 "permissions" array an earlier
    # version of this adapter wrote: OpenCode 1.4.2 refuses a config carrying
    # that key (verified 2026-09-23, `opencode debug config`). Unrelated
    # config is preserved.
    config_path = target / "opencode.jsonc"
    policy = load_permissions(source)
    if policy:
        existing_settings: dict[str, Any] = {}
        if config_path.exists():
            existing_settings = json.loads(
                strip_jsonc_comments(config_path.read_text())
            )
        merged_settings = {
            key: value for key, value in existing_settings.items() if key != "permissions"
        }
        merged_settings["permission"] = opencode_permission(policy)
        if merged_settings != existing_settings or not same_in_order(
            existing_settings.get("permission"), merged_settings["permission"]
        ):
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                config_path.write_text(json.dumps(merged_settings, indent=2) + "\n")
            actions.append("render permissions.json into opencode.jsonc")

    # 0b. runtime_settings.opencode.model in components.json -> model.
    # OpenCode sets a subagent's model per agent only; notes() names one set.
    model = default_models(source, "opencode")["model"]
    if model:
        existing_config: dict[str, Any] = {}
        if config_path.exists():
            existing_config = json.loads(strip_jsonc_comments(config_path.read_text()))
        if existing_config.get("model") != model:
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                config_path.write_text(json.dumps(dict(existing_config, model=model), indent=2) + "\n")
            actions.append("render components.json runtime_settings.opencode model into opencode.jsonc")

    # 0c. runtime_settings.opencode.provider in components.json -> the
    # "provider" key:
    # a custom OpenAI-compatible provider such as a self-hosted gateway
    # (provider.<id>.npm/options/models, OpenCode's own schema).
    # merge_provider_entries is the same primitive the project sync
    # uses to render one project's opt-in provider entry into its own
    # checkout-root opencode.jsonc, so the two never drift.
    provider = runtime_settings(source, "opencode").get("provider")
    if isinstance(provider, dict) and provider and merge_provider_entries(config_path, provider, dry_run):
        actions.append("render components.json runtime_settings.opencode provider into opencode.jsonc")

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

    # 2. commands/*.md -> target/commands/<name>.md
    #    OpenCode V2 discovers .md files in commands/ (plural).
    #    Ref: https://opencode.ai/v2/docs/commands
    #    Translate frontmatter: keep description, model, agent;
    #    drop allowed-tools, argument-hint (no OC equivalent).
    commands_src = source / "commands"
    if commands_src.is_dir():
        commands_dst = target / "commands"
        for cmd_file in sorted(commands_src.glob("*.md")):
            text = cmd_file.read_text()
            fm, body = read_frontmatter(text)

            new_fm = _translate_command_frontmatter(fm)
            content = new_fm + body

            dst = commands_dst / cmd_file.name
            if dst.exists() and dst.read_text() == content:
                continue
            if not dry_run:
                commands_dst.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)
            actions.append(f"translate commands/{cmd_file.name}")

    # 3. agents/*.md -> target/agents/<name>.md
    #    Ref: https://opencode.ai/v2/docs/agents
    #    Translate frontmatter: name dropped (filename is ID),
    #    description kept, tools list -> permissions array,
    #    model kept, color kept (V2 supports color),
    #    mode set to subagent.
    #    Body: replace CLAUDE.md -> AGENTS.md.
    agents_src = source / "agents"
    if agents_src.is_dir():
        agents_dst = target / "agents"
        aliases = agent_aliases(source, "opencode")
        for md_file in sorted(agents_src.glob("*.md")):
            text = md_file.read_text()
            fm, body = read_frontmatter(text)

            new_fm = _build_agent_frontmatter(fm, aliases)
            converted_body = body.replace("CLAUDE.md", "AGENTS.md")
            content = new_fm + converted_body

            dst = agents_dst / md_file.name
            if dst.exists() and dst.read_text() == content:
                continue
            if not dry_run:
                agents_dst.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)
            actions.append(f"translate agents/{md_file.name}")

    # 4. skills/<name>/SKILL.md -> target/skill/<name>/SKILL.md
    #    OpenCode discovers skills from .opencode/skill/ (singular) and
    #    directories listed in the "skills" config array.
    #    Ref: https://opencode.ai/docs/skills/
    #    We mirror to target/skill/ to match the discovery convention.
    skills_src = source / "skills"
    if skills_src.is_dir():
        skills_dst = target / "skill"
        skill_actions = mirror_skills(skills_src, skills_dst, dry_run)
        if skill_actions:
            actions.extend(f"skill: {a}" for a in skill_actions)

    # 5. OpenCode hooks are a local plugin. Mirror the canonical shell helpers
    # and install the bridge that maps OpenCode tool events to their payloads.
    hooks_src = source / "hooks"
    if hooks_src.is_dir():
        hooks_dst = target / "hooks"
        for rel in sorted(_owned_hook_names(source)):
            hook_file = hooks_src / rel
            destination = hooks_dst / rel
            if (
                destination.exists()
                and destination.read_bytes() == hook_file.read_bytes()
            ):
                continue
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(hook_file, destination)
            actions.append(f"copy hooks/{rel}")
    plugin_src = hooks_src / "opencode-runtime-hooks.ts"
    plugin_name = own_plugin(source)
    plugin_dst = target / "plugins" / plugin_name
    if plugin_src.exists() and (
        not plugin_dst.exists() or plugin_dst.read_bytes() != plugin_src.read_bytes()
    ):
        if not dry_run:
            plugin_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plugin_src, plugin_dst)
        actions.append(f"install plugins/{plugin_name}")

    # 6. rules/*.md -> target/rules/
    #    OpenCode receives the generated rules digest through AGENTS.md.
    #    Keep these files available for inspection, but do not propose the
    #    inactive instructions setting as a manual runtime step.
    #    Legacy single-level layout support:
    #      source/rules/*.md (global rules are files not in the common tier of
    #      rules/tiers.json, except README.md)
    rules_root = source / "rules"
    for rule_name in sorted(_owned_rule_names(source)):
        rule_file = _owned_rule_source(source, rule_name)
        rules_dst = target / "rules"
        dst = rules_dst / rule_name
        src_bytes = rule_file.read_bytes()
        if dst.exists() and dst.read_bytes() == src_bytes:
            continue
        if not dry_run:
            rules_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rule_file, dst)
        rel = rule_file.relative_to(rules_root).as_posix()
        actions.append(f"copy rules/{rel} -> rules/{rule_name}")

    # 7. A target-only file in an owned directory (commands/, agents/,
    # skill/, hooks/, rules/) is an orphan pruned by sync.py --prune, per
    # owned_outputs below, not deleted by this loop.

    # 8. components.json MCP servers the global column selects -> the mcp key
    # of opencode.jsonc (adapters/_components.py).
    actions.extend(render_components("opencode", source, target, dry_run))

    return actions


def _owned_hook_names(source: Path) -> frozenset:
    """Relative hook file names the hooks copy loop mirrors into the target.

    Reused by owned_outputs so the reverse pass never drifts from what
    that loop actually produces.
    """
    hooks_src = source / "hooks"
    if not hooks_src.is_dir():
        return frozenset()
    names: set = set()
    for hook_file in hooks_src.rglob("*"):
        if (
            not hook_file.is_file()
            or hook_file.name.endswith(".test.sh")
            or hook_file.name in {"hook-test.sh", "hooks.json", "retired.json"}
            or hook_file.suffix not in {".sh", ".py", ".json"}
        ):
            continue
        names.add(str(hook_file.relative_to(hooks_src)))
    return frozenset(names)


def _owned_rule_names(source: Path) -> frozenset:
    """Basenames the rules copy loop mirrors into target/rules/.

    Reused by owned_outputs so the reverse pass never drifts from what
    that loop actually produces. Mirrors the loop's own legacy-layout
    fallback: source/rules/global/*.md when present, else the flat
    source/rules/*.md with README.md and any name in the common tier of
    rules/tiers.json excluded (those reach OpenCode through the AGENTS.md
    digest instead). Without a manifest nothing is excluded but README.md.
    """
    rules_root = source / "rules"
    rules_src = rules_root / "global"
    if not rules_src.is_dir():
        rules_src = rules_root
    if not rules_src.is_dir():
        return frozenset()
    common = frozenset(load_tiers(rules_root).get("common", ()))
    names: set = set()
    for rule_file in rules_src.glob("*.md"):
        if rule_file.name.lower() == "readme.md":
            continue
        if rules_src == rules_root and rule_file.name in common:
            continue
        names.add(rule_file.name)
    return frozenset(names)


def _owned_rule_source(source: Path, name: str) -> Path:
    """The source file _owned_rule_names(source) resolved *name* from."""
    rules_root = source / "rules"
    rules_src = rules_root / "global"
    if not rules_src.is_dir():
        rules_src = rules_root
    return rules_src / name


def owned_outputs(source: Path, target: Path) -> List[OwnedDir]:
    """Directories this adapter owns in an OpenCode target tree.

    Computed from source by the same name mapping sync() itself applies, so a
    file sync() would write is never reported as an orphan by the
    sync.py --check reverse pass. OpenCode hooks are a local plugin file
    (installed once, replaced whole), not a directory of owned names, so
    it is not covered here; opencode.jsonc's managed keys are likewise a
    single runtime-native file, not a directory.
    """
    owned = [
        OwnedDir(
            "command",
            target / "commands",
            frozenset(p.name for p in (source / "commands").glob("*.md")),
        ),
        OwnedDir(
            "agent",
            target / "agents",
            frozenset(p.name for p in (source / "agents").glob("*.md")),
        ),
        OwnedDir("hook", target / "hooks", _owned_hook_names(source)),
        OwnedDir("rule", target / "rules", _owned_rule_names(source)),
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
                target / "skill",
                names,
                per_item=True,
                exclude=frozenset(excluded),
            )
        )
    return owned


# The one plugin file sync() installs under plugins/; every other file there
# was put there by something else.
def own_plugin(root: Optional[Path] = None) -> str:
    """The plugin file name, `<engine name>-hooks.ts`."""
    return f"{engine_name(root)}-hooks.ts"


def external_inventory(target: Path, source: Optional[Path] = None) -> List[Item]:
    """Every external item OpenCode holds: the mcp and plugin keys of the
    global configuration (opencode.json and opencode.jsonc, both read) and
    each file under plugins/ other than <name>-hooks.ts, per the inventory
    record's table. An MCP server's environment and headers are carried for
    the literal-secret scan."""
    runtime = RUNTIME["name"]
    items: List[Item] = []
    for config in (target / "opencode.json", target / "opencode.jsonc"):
        data, error = read_jsonc(config)
        if error:
            items.append(unreadable(runtime, "mcp_server", config, error))
            continue
        if not isinstance(data, dict):
            continue
        items.extend(mcp_items(runtime, data.get("mcp"), config, "mcp"))
        plugins = data.get("plugin")
        if isinstance(plugins, list):
            items.extend(Item(runtime, "plugin", str(p), str(config)) for p in plugins if isinstance(p, str))
        elif plugins is not None:
            items.append(unreadable(runtime, "plugin", config, "plugin is not a list"))
    directory = target / "plugins"
    names, error = list_dir(directory, skip=(own_plugin(source),))
    if error:
        items.append(unreadable(runtime, "plugin", directory, error))
    items.extend(Item(runtime, "plugin", name, str(directory / name)) for name in names)
    return items
