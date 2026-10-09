"""Claude Code runtime adapter for the sync engine.

Target root: $HOME/.claude/
Claude Code is the native format, so this is mostly a structured copy
with hooks.json merged into settings.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from ._common import (
    OwnedDir,
    copy_preserving_mode,
    mirror_dir,
    mirror_skills,
    mirrored_names,
    repoint_command,
    skill_excludes,
)
from ._components import RenderRefused, default_models, enablement, runtime_settings, selected_plugins
from ._foreign import render as render_foreign
from ._inventory import Item, list_dir, mcp_from_json_file, mcp_items, read_json, unreadable
from stratarc.permissions import claude_auto_mode, claude_settings, load as load_permissions

# This runtime's entry in the runtime registry (_common.runtime_registry).
RUNTIME = {
    "name": "claude",
    "target": ".claude",
    "hook_registry": "settings.json",
    # Claude Code hosts every canonical event natively. PostToolUseFailure,
    # SubagentStart and SubagentStop carry the fields the event mapping relies on (verified
    # 2026-09-24, the Claude Code 2.1.273 binary).
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

_HOOKS_PREFIX = "$HOME/.claude/hooks"
_HOOK_MIRROR_EXCLUDES = [
    "*.test.sh",
    "hook-test.sh",
    "hooks.json",
    "claude-worktree-hooks.json",
    "claude-agent-graph-hooks.json",
    "opencode-runtime-hooks.ts",
    "retired.json",
]


def _remove_retired_rule_mirror(source: Path, target: Path, dry_run: bool) -> List[str]:
    """Delete the rule files earlier syncs mirrored into target/rules/.

    Claude Code loads every Markdown file under ~/.claude/rules/, so the full
    rule tree there sat in context beside CLAUDE.md's digest, which already
    carries each rule's binding section and a pointer to the full text. The
    two together pushed sessions past Claude Code's 150k-character
    instruction limit.
    Only names the source tree holds are removed; the directory itself goes
    once it is empty.
    """
    rules_dst = target / "rules"
    rules_src = source / "rules"
    if not rules_dst.is_dir() or not rules_src.is_dir():
        return []
    actions: List[str] = []
    for name in sorted(p.name for p in rules_src.glob("*.md")):
        deployed = rules_dst / name
        if deployed.is_file():
            actions.append(f"{'would remove' if dry_run else 'remove'} retired rule copy {deployed}")
            if not dry_run:
                deployed.unlink()
    if not dry_run and not any(rules_dst.iterdir()):
        rules_dst.rmdir()
    return actions


def _deep_merge(existing: object, desired: object) -> object:
    """Merge canonical settings without discarding runtime-native settings."""
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        return desired
    merged = dict(existing)
    for key, value in desired.items():
        merged[key] = _deep_merge(merged.get(key), value)
    return merged


def _load_runtime_settings(source: Path) -> dict:
    """The settings keys runtime_settings.claude in components.json carries.

    Some settings.json keys have no other source in this repo: statusLine is
    one, since it is neither a permission nor a hook. Runtime output is
    generated, so those keys are declared in the manifest and rendered by sync
    rather than edited into ~/.claude/settings.json by hand. The passthrough
    object is settings; model and subagent_model become Claude Code's own
    keys, a short name translated through aliases when it has one (the runtime
    settings file was folded into the component manifest).
    """
    passthrough = runtime_settings(source, "claude").get("settings")
    out: dict = dict(passthrough) if isinstance(passthrough, dict) else {}
    models = default_models(source, "claude")
    if models["model"]:
        out["model"] = models["model"]
    if models["subagent_model"]:
        out = _deep_merge(out, {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": models["subagent_model"]}})
    return out


def installed_plugins(target: Path) -> List[str]:
    """The plugin keys plugins/installed_plugins.json lists, or [] when it is absent.

    An unreadable registry raises RenderRefused: writing the enablement key
    from a partial list would switch on nothing and could leave an installed
    plugin enabled, so the run fails instead (sync.py reports it).
    """
    registry = target / "plugins" / "installed_plugins.json"
    if not registry.exists():
        return []
    data, error = read_json(registry)
    if error:
        raise RenderRefused(f"cannot write enabledPlugins: {registry.name} is unreadable ({error})")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        raise RenderRefused(f"cannot write enabledPlugins: plugins in {registry.name} is not an object")
    return sorted(str(key) for key in plugins)


def _desired_enabled_plugins(source: Path, target: Path, existing: object, environment: str | None = None) -> dict:
    """enabledPlugins as the adapter owns it: every installed, previously
    listed or selected plugin, true only when the global column selects it."""
    known = installed_plugins(target)
    if isinstance(existing, dict):
        known += [str(key) for key in existing]
    selected = list(selected_plugins(source, "claude"))
    environment = environment or os.environ.get("STRATARC_ENVIRONMENT")
    environments = runtime_settings(source, "claude").get("environment_plugins", {})
    if environment and isinstance(environments, dict):
        selected += environments.get(environment, [])
    return enablement(known, selected)


def sync(
    source: Path, target: Path, dry_run: bool = False, environment: str | None = None,
) -> List[str]:
    """Sync the shared config source tree into a Claude Code target directory.

    Args:
        source: path to the shared config repo root
        target: path to the Claude Code runtime root (e.g. ~/.claude)
        dry_run: if True, report what would happen without writing

    Returns:
        list of human-readable action descriptions
    """
    actions: List[str] = []

    # 0a. The declared foreign hook registrations go in before the hooks block
    # below, so that merge sees them as foreign groups and preserves them where
    # it preserves every other one, in this same pass.
    actions.extend(render_foreign("claude", source, target, dry_run))

    # 0. permissions.json is the single source of truth. Preserve unrelated
    # user and plugin settings while replacing the complete permissions block.
    settings_path = target / "settings.json"
    desired_settings: dict = {}
    sources: List[str] = []
    policy = load_permissions(source)
    if policy:
        desired_settings["permissions"] = claude_settings(policy)
        auto_mode = claude_auto_mode(policy)
        if auto_mode is not None:
            desired_settings["autoMode"] = auto_mode
        sources.append("permissions.json")

    # 0b. runtime_settings.claude in components.json carries settings keys
    # with no other source (statusLine today) and the default models. Merged
    # here so it shares the same preserving merge that keeps unrelated user
    # and plugin settings intact.
    claude_runtime_settings = _load_runtime_settings(source)
    if claude_runtime_settings:
        desired_settings = _deep_merge(desired_settings, claude_runtime_settings)
        sources.append("components.json runtime_settings.claude")

    existing_settings = (
        json.loads(settings_path.read_text(encoding="utf-8"))
        if settings_path.is_file()
        else {}
    )
    # 0c. enabledPlugins is owned as a whole: replaced, never merged,
    # so a hand-enabled plugin is switched back off and shows as drift.
    enabled_plugins = _desired_enabled_plugins(source, target, existing_settings.get("enabledPlugins"), environment)
    if desired_settings or enabled_plugins:
        merged_settings = _deep_merge(existing_settings, desired_settings)
        if enabled_plugins:
            merged_settings["enabledPlugins"] = enabled_plugins
            sources.append("components.json enabledPlugins")
        if merged_settings != existing_settings:
            actions.append(f"render {' and '.join(sources)} into {settings_path}")
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(
                    json.dumps(merged_settings, indent=2) + "\n", encoding="utf-8"
                )

    # 1. AGENTS.md -> AGENTS.md, CODEX.md and CLAUDE.md (byte copies). Its
    #    digest already carries each rule as binding plus a pointer.
    agents_src = source / "AGENTS.md"
    if agents_src.is_file():
        for name in ("AGENTS.md", "CODEX.md", "CLAUDE.md"):
            dst = target / name
            if not dst.is_file() or dst.read_bytes() != agents_src.read_bytes():
                actions.append(copy_preserving_mode(agents_src, dst, dry_run))

    # 2. The rule tree is no longer mirrored into target/rules/; remove the
    #    copies earlier syncs left there (see _remove_retired_rule_mirror).
    actions.extend(_remove_retired_rule_mirror(source, target, dry_run))

    # 3. hooks/*.sh, hooks/lib/** -> target/hooks/
    #    Mirror with exec bit, EXCLUDE *.test.sh and hooks.json
    actions.extend(
        mirror_dir(
            source / "hooks",
            target / "hooks",
            dry_run,
            delete_extra=True,
            exclude_patterns=_HOOK_MIRROR_EXCLUDES,
        )
    )

    # 4. hooks/hooks.json -> merge INTO target/settings.json under 'hooks' key.
    #    Merge rule: a hook group is "ours" if any command in it references a
    #    script that the shared config ships under hooks/. Ours are replaced by the
    #    source; runtime-native groups (plugins, local tooling) are preserved.
    hook_manifests = [
        source / "hooks" / "hooks.json",
        source / "hooks" / "claude-worktree-hooks.json",
        source / "hooks" / "claude-agent-graph-hooks.json",
    ]
    hooks_data: dict = {}
    for manifest_path in hook_manifests:
        if not manifest_path.is_file():
            continue
        for event, groups in json.loads(
            manifest_path.read_text(encoding="utf-8")
        ).items():
            hooks_data.setdefault(event, []).extend(groups)
    if hooks_data:
        if settings_path.is_file():
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        else:
            settings = {}

        shipped = {
            p.name
            for p in (source / "hooks").glob("*.sh")
            if not p.name.endswith(".test.sh")
        }
        legacy_worktree_scripts = {
            "worktree-create.sh",
            "worktree-hydrate.sh",
            "worktree-remove.sh",
            "worktree-validate.sh",
        }

        legacy_hook_scripts = {
            "autosave.sh",
        }

        def _ours(group) -> bool:
            if not isinstance(group, dict):
                return False
            for h in group.get("hooks", []) or []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if any(f"/hooks/{n}" in cmd for n in shipped | legacy_hook_scripts):
                    return True
                if any(
                    f"/scripts/{n}" in cmd or f"/hooks/{n}" in cmd
                    for n in legacy_worktree_scripts
                ):
                    return True
            return False

        # A preserved foreign group may still name one of our shipped
        # scripts from the wrong directory (the "mislocated" disposition).
        # Repoint that one command onto our own hooks directory before
        # keeping the rest of the group untouched.
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
                        source_names=shipped,
                        hooks_prefix=_HOOKS_PREFIX,
                        hooks_dir=target / "hooks",
                    )
                new_hooks.append(h)
            return {**group, "hooks": new_hooks}

        old_hooks = settings.get("hooks")
        if not isinstance(old_hooks, dict):
            old_hooks = {}
        merged: dict = {}
        events = list(dict.fromkeys(list(hooks_data) + list(old_hooks)))
        for ev in events:
            prev = old_hooks.get(ev, [])
            foreign = [
                _repoint_group(g)
                for g in (prev if isinstance(prev, list) else [])
                if not _ours(g)
            ]
            ours = hooks_data.get(ev, [])
            combined = ours + foreign
            if combined:
                merged[ev] = combined

        if old_hooks != merged:
            verb = "would merge" if dry_run else "merge"
            actions.append(f"{verb} hooks.json into {settings_path}")
            if not dry_run:
                settings["hooks"] = merged
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(
                    json.dumps(settings, indent=2) + "\n", encoding="utf-8"
                )

    # 5. skills/ -> target/skills/ (add/update only, never delete absent skills)
    actions.extend(mirror_skills(source / "skills", target / "skills", dry_run))

    # 6. commands/*.md -> target/commands/ (mirror)
    actions.extend(
        mirror_dir(source / "commands", target / "commands", dry_run, delete_extra=True)
    )

    # 7. agents/*.md -> target/agents/ (mirror)
    actions.extend(
        mirror_dir(source / "agents", target / "agents", dry_run, delete_extra=True)
    )

    return actions


def owned_outputs(source: Path, target: Path) -> List[OwnedDir]:
    """Directories this adapter owns in a Claude Code target tree.

    Computed from source by the same name mapping sync() itself applies,
    so a
    file sync() would write is never reported as an orphan by the
    sync.py --check reverse pass. target/rules/ stays owned with nothing
    expected in it: sync() no longer mirrors the rule tree there, so any
    file left in it is an orphan.
    """
    owned = [
        OwnedDir("rule", target / "rules", frozenset()),
        OwnedDir(
            "hook",
            target / "hooks",
            mirrored_names(source / "hooks", exclude_patterns=_HOOK_MIRROR_EXCLUDES),
        ),
        OwnedDir("command", target / "commands", mirrored_names(source / "commands")),
        OwnedDir("agent", target / "agents", mirrored_names(source / "agents")),
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
                # "synced" is never our source: Claude Code recreates it as
                # its own skill-sync bucket on every launch, so it is
                # excluded unconditionally rather than through
                # skills/sync-exclude.json, and the reverse pass must never
                # call it an orphan --prune would delete.
                exclude=frozenset(excluded) | {"synced"},
            )
        )
    return owned


# Confirmed on a live install (Claude Code 2.x): the plugin
# registry is plugins/installed_plugins.json ({"plugins": {"<name>@<market>":
# [...]}}) and the marketplace registry plugins/known_marketplaces.json
# ({"<market>": {...}}); enabledPlugins sits in settings.json.
_CONNECTORS_UNCONFIRMED = (
    "unconfirmed where the account connector list can be read; a live check "
    "confirms it"
)


def external_inventory(target: Path) -> List[Item]:
    """Every external item Claude Code holds, per the inventory record's table.

    Plugins from plugins/installed_plugins.json and the enabledPlugins key of
    settings.json, marketplaces from plugins/known_marketplaces.json, MCP
    servers from ~/.claude.json (user scope and each projects.<path> entry)
    and from .mcp.json at the root of each active project, and the account
    connectors row, which stays unreadable until a live check confirms its source.
    The home directory is target's parent, so a fixture home works unchanged.
    """
    runtime = RUNTIME["name"]
    home = target.parent
    items: List[Item] = []

    registry = target / "plugins" / "installed_plugins.json"
    data, error = read_json(registry)
    seen: set = set()
    if error:
        items.append(unreadable(runtime, "plugin", registry, error))
    elif isinstance(data, dict):
        plugins = data.get("plugins")
        if isinstance(plugins, dict):
            for key in sorted(plugins):
                seen.add(key)
                items.append(Item(runtime, "plugin", key, str(registry)))
        elif plugins is not None:
            items.append(unreadable(runtime, "plugin", registry, "plugins is not an object"))

    settings_path = target / "settings.json"
    settings, error = read_json(settings_path)
    if error:
        items.append(unreadable(runtime, "plugin", settings_path, error))
    elif isinstance(settings, dict) and isinstance(settings.get("enabledPlugins"), dict):
        for key, on in sorted(settings["enabledPlugins"].items()):
            if on is True and key not in seen:
                items.append(Item(runtime, "plugin", key, str(settings_path)))

    markets_path = target / "plugins" / "known_marketplaces.json"
    markets, error = read_json(markets_path)
    if error:
        items.append(unreadable(runtime, "marketplace", markets_path, error))
    elif isinstance(markets, dict):
        items.extend(Item(runtime, "marketplace", key, str(markets_path)) for key in sorted(markets))

    account = home / ".claude.json"
    data, error = read_json(account)
    if error:
        items.append(unreadable(runtime, "mcp_server", account, error))
    elif isinstance(data, dict):
        items.extend(mcp_items(runtime, data.get("mcpServers"), account, "mcpServers"))
        projects = data.get("projects")
        if isinstance(projects, dict):
            for path, project in sorted(projects.items()):
                if isinstance(project, dict):
                    items.extend(
                        mcp_items(runtime, project.get("mcpServers"), account, f"projects.{path}.mcpServers")
                    )

    active = home / "projects" / "active"
    names, error = list_dir(active, dirs_only=True)
    if error:
        items.append(unreadable(runtime, "mcp_server", active, error))
    for name in names:
        items.extend(mcp_from_json_file(runtime, active / name / ".mcp.json", "mcpServers"))

    items.append(unreadable(runtime, "connector", account, _CONNECTORS_UNCONFIRMED))
    return items
