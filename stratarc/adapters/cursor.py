"""Cursor runtime adapter for canonical hooks and required user rules.

Cursor's hooks.json lists flat entries per event, {"command", "matcher"?,
"timeout"?}. A file carrying one Claude-shaped group ({"matcher", "hooks":
[...]}) fires nothing at all, its flat entries included (verified 2026-09-23,
cursor-agent 2026.07.23-e383d2b, a scratch project's .cursor/hooks.json
recording every payload), so every entry written here is flat, and a
grouped entry already in the file is flattened.

sessionStart and sessionEnd are Cursor hook events, and sessionStart's
additional_context reaches the model (verified 2026-09-23, same check).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ._common import OwnedDir, repoint_command
from ._foreign import render as render_foreign
from ._inventory import Item, mcp_from_json_file, unreadable


# This runtime's entry in the runtime registry (_common.runtime_registry).
# hook_events lists what sync() handles, a deliberate drop included: Stop,
# UserPromptSubmit, PostToolUseFailure, SubagentStart and SubagentStop have no
# entry in EVENTS below and are skipped. The three lifecycle events are dropped
# for now: cursor-agent 2026.07.23-e383d2b names postToolUseFailure,
# subagentStart and subagentStop in its bundle (verified 2026-09-24, the
# strings of index.js), but no live capture shows their payloads
# (a recorded cursor-agent hooks capture has sessionStart,
# preToolUse and sessionEnd only), so whether they carry a tool_use_id or an
# agent id is unknown. Cursor keeps counting tool calls at preToolUse
# (hooks/lib/tool-budget.py) and has no start signal for the subagent cap.
RUNTIME = {
    "name": "cursor",
    "target": ".cursor",
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

EVENTS = {
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "SessionStart": "sessionStart",
    "SessionEnd": "sessionEnd",
}
REQUIRED_USER_RULES = ("no-agent-attribution.md",)
_HOOKS_PREFIX = "$HOME/.cursor/hooks"

# Claude tool names in a matcher, as Cursor itself translates them when it
# imports Claude Code hooks (verified 2026-09-23, cursor-agent
# 2026.07.23-e383d2b index.js: Bash to Shell, Edit to Write, Glob not
# supported, mcp__<server>__<tool> to MCP:<tool>). A name not in the table is
# kept as written.
_CURSOR_TOOLS = {
    "Bash": "Shell",
    "Read": "Read",
    "Write": "Write",
    "Edit": "Write",
    "Glob": None,
    "Grep": "Grep",
    "WebFetch": "WebFetch",
    "WebSearch": "WebSearch",
    "Task": "Task",
}


def _cursor_matcher(matcher: object) -> str | None:
    """Translate a Claude matcher; None matches every tool."""
    if not isinstance(matcher, str) or matcher.strip() in ("", "*"):
        return None
    names: list[str] = []
    for part in matcher.split("|"):
        name = part.strip()
        if name.startswith("mcp__") and len(name.split("__")) >= 3:
            translated = "MCP:" + "__".join(name.split("__")[2:])
        elif name in _CURSOR_TOOLS:
            translated = _CURSOR_TOOLS[name]
            if translated is None:
                continue
        else:
            translated = name
        if translated not in names:
            names.append(translated)
    return "|".join(names) if names else None


def _flat_entries(group: object) -> list:
    """One flat Cursor entry per hook in a Claude-shaped group.

    An entry that is already flat, or not a group at all, is returned as is.
    """
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return [group]
    matcher = _cursor_matcher(group.get("matcher"))
    entries = []
    for hook in group["hooks"]:
        if not isinstance(hook, dict) or not hook.get("command"):
            continue
        entry: dict = {"command": hook["command"]}
        if matcher is not None:
            entry["matcher"] = matcher
        if "timeout" in hook:
            entry["timeout"] = hook["timeout"]
        entries.append(entry)
    return entries


def _source_names(source: Path) -> set[str]:
    return {
        path.name
        for path in (source / "hooks").glob("*.sh")
        if not path.name.endswith(".test.sh")
    }


def _is_ours(entry: object, names: set[str]) -> bool:
    if not isinstance(entry, dict):
        return False
    commands = [entry.get("command", "")] + [
        hook.get("command", "") for hook in entry.get("hooks", []) if isinstance(hook, dict)
    ]
    return any(
        f"/.cursor/hooks/{name}" in str(command) for command in commands for name in names
    )


def _repoint_foreign_group(group: object, *, source_names: set[str], hooks_dir: Path):
    """Repoint a preserved foreign entry's mislocated source scripts.

    A foreign entry may still name one of our shipped scripts from the
    wrong directory. That one command is rewritten onto our own hooks
    directory; everything else in the entry, including an existence
    guard, is left untouched. Entries are flat by the time they reach here.
    """
    if not isinstance(group, dict) or not isinstance(group.get("command"), str):
        return group
    return {
        **group,
        "command": repoint_command(
            group["command"],
            source_names=source_names,
            hooks_prefix=_HOOKS_PREFIX,
            hooks_dir=hooks_dir,
        ),
    }


def _owned_hook_names(source: Path) -> frozenset:
    """Relative hook file names _copy_hooks mirrors into the target.

    Reused by owned_outputs so the reverse pass never drifts from what
    this copy loop actually produces.
    """
    source_dir = source / "hooks"
    if not source_dir.is_dir():
        return frozenset()
    names: set[str] = set()
    for item in source_dir.rglob("*"):
        if (
            not item.is_file()
            or item.name.endswith(".test.sh")
            or item.name in {"hook-test.sh", "hooks.json", "retired.json"}
            or item.suffix not in {".sh", ".py", ".json"}
        ):
            continue
        names.add(str(item.relative_to(source_dir)))
    return frozenset(names)


def _copy_hooks(source: Path, target: Path, dry_run: bool) -> list[str]:
    actions: list[str] = []
    source_dir = source / "hooks"
    target_dir = target / "hooks"
    for rel in sorted(_owned_hook_names(source)):
        item = source_dir / rel
        destination = target_dir / rel
        if destination.exists() and destination.read_bytes() == item.read_bytes():
            continue
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
        actions.append(f"copy hooks/{rel}")
    return actions


def _copy_required_user_rules(
    source: Path, target: Path, dry_run: bool
) -> list[str]:
    actions: list[str] = []
    for name in REQUIRED_USER_RULES:
        item = source / "rules" / name
        if not item.is_file():
            continue
        destination = target / "rules" / name
        if destination.exists() and destination.read_bytes() == item.read_bytes():
            continue
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
        actions.append(f"copy rules/{name}")
    return actions


def sync(source: Path, target: Path, dry_run: bool = False) -> list[str]:
    """Sync canonical hooks and required user rules into Cursor."""
    actions = _copy_required_user_rules(source, target, dry_run)
    actions.extend(_copy_hooks(source, target, dry_run))
    # The declared foreign registrations go in before the merge below, so the
    # merge sees them as foreign entries and places them where it places every
    # other one, in this same pass.
    actions.extend(render_foreign("cursor", source, target, dry_run))
    source_config = source / "hooks" / "hooks.json"
    if not source_config.exists():
        return actions

    canonical = json.loads(source_config.read_text())
    source_names = _source_names(source)
    desired: dict[str, list[dict]] = {}
    for event, groups in canonical.items():
        cursor_event = EVENTS.get(event)
        if cursor_event is None:
            continue
        desired[cursor_event] = []
        for group in groups:
            for entry in _flat_entries(group):
                entry["command"] = str(entry.get("command", "")).replace(
                    "$HOME/.claude/hooks/", "$HOME/.cursor/hooks/"
                )
                desired[cursor_event].append(entry)

    config_path = target / "hooks.json"
    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            existing = {}
    prior = existing.get("hooks", {}) if isinstance(existing.get("hooks"), dict) else {}
    merged: dict[str, list[dict]] = {}
    for event in set(prior) | set(desired):
        foreign = [
            _repoint_foreign_group(
                entry, source_names=source_names, hooks_dir=target / "hooks"
            )
            for group in prior.get(event, [])
            if not _is_ours(group, source_names)
            for entry in _flat_entries(group)
        ]
        groups = desired.get(event, []) + foreign
        if groups:
            merged[event] = groups
    rendered = {**existing, "version": 1, "hooks": merged}
    if rendered != existing:
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(rendered, indent=2) + "\n")
        actions.append("merge hooks.json")
    return actions


def owned_outputs(source: Path, target: Path) -> list[OwnedDir]:
    """Directories this adapter owns in a Cursor target tree.

    Computed from source by the same name mapping sync() itself applies, so a
    file sync() would write is never reported as an orphan by the
    sync.py --check reverse pass.

    Unlike the record's own table, this adapter copies exactly the rules
    named in REQUIRED_USER_RULES, not the whole rules/ tree, so only that
    one name is owned here; owned_outputs follows the code, not the
    table. Cursor has no owned skills, agents or commands: sync() does
    not write any of those, so none is covered here either.
    """
    rule_names = frozenset(
        name for name in REQUIRED_USER_RULES if (source / "rules" / name).is_file()
    )
    return [
        OwnedDir("rule", target / "rules", rule_names),
        OwnedDir("hook", target / "hooks", _owned_hook_names(source)),
    ]


_PLUGINS_UNCONFIRMED = (
    "unconfirmed plugin registry; plugins/ holds cache and marketplaces but no "
    "confirmed list of installed plugins, and a live check confirms it"
)


def external_inventory(target: Path) -> list[Item]:
    """Every external item Cursor holds: mcpServers in mcp.json, and the
    plugins row, unreadable until its registry is confirmed live (the
    inventory record's table marks it unconfirmed)."""
    runtime = RUNTIME["name"]
    items = mcp_from_json_file(runtime, target / "mcp.json", "mcpServers")
    items.append(unreadable(runtime, "plugin", target / "plugins", _PLUGINS_UNCONFIRMED))
    return items
