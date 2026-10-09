"""Render the foreign hook registrations components.json declares wanted into
each runtime's own hook registry.

A foreign tool's own installer writes into ~/.claude/settings.json, ~/.codex/hooks.json,
~/.cursor/hooks.json and ~/.gemini/settings.json, which are exactly the files the sync
owns, so running it would fight the next sync. What it would write is captured once from
a fixture home, committed under the source root, named by the manifest entry's
registration map, and written here instead. The sync check then reports the registration
as declared rather than as an undeclared foreign hook group, and a prune leaves it alone.

The captured body is already in the runtime's own shape, because the dump is
per agent profile, and the four shapes genuinely differ: Claude Code groups
hooks under a matcher and measures its timeout in seconds; Codex adds
commandWindows and anchors its matcher (^Bash$); Cursor uses lowercase event
names, flat entries, the Shell tool name and failClosed on preToolUse; Gemini
CLI uses BeforeTool, AfterTool and AfterAgent, matches run_shell_command and
measures its timeout in milliseconds. Nothing here translates between them: a
body is written for the runtime it was captured for and no other.

Each adapter calls this before its own registry merge, never after, and this
module only ever appends a captured group the event does not already hold. The
adapter then reads the registration as an ordinary foreign group and puts it
where it puts every other one, in the same pass. Running after the merge
instead would work too, but the two disagree about placement (Codex writes its
own groups first and foreign ones after, Gemini CLI the other way round), so
each adapter would move the group on the following sync and report one more
stale run for a file whose content had already stopped changing.

Because a group is appended only when it is absent, and every one of the four
adapters preserves a foreign group unchanged, the second sync finds everything
present and writes nothing. tests/test_adapters_foreign.py syncs twice through the
real adapters and fails if any of them rewrites its registry.

Removal is the reverse pass's, not this module's: an entry turned wanted:
false is reported returned by the sync and removed by --prune, which
already knows the registry, event and command of every registration it
reports. A ledger here would duplicate that and could disagree with it.

OpenCode is absent throughout: it has no JSON hook registry to write one into.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._common import runtime_registry
from ._components import RenderRefused, staged


def registry_name(runtime: str) -> Optional[str]:
    """The hook registry file inside this runtime's target, or None when it
    has none (OpenCode keeps a local plugin instead)."""
    entry = runtime_registry().get(runtime)
    return entry.hook_registry if entry is not None else None


def declared(source: Path, runtime: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(name, registration body) for each wanted foreign hook this runtime renders.

    The body is the file the entry's registration map names, read from the
    stage at the same repository-relative path the staging step copied it to.
    A named file that is missing or unreadable is refused rather than skipped:
    silently registering nothing would leave the runtime without the hook and
    say so nowhere.
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for entry in staged(source).get("foreign_hooks") or []:
        if not isinstance(entry, dict) or entry.get("wanted") is not True:
            continue
        if runtime not in (entry.get("runtimes") or []):
            continue
        registration = entry.get("registration")
        relative = registration.get(runtime) if isinstance(registration, dict) else None
        if not isinstance(relative, str) or not relative:
            continue
        name = str(entry.get("name") or relative)
        path = source / relative
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RenderRefused(
                f"foreign hook {name} names {relative} for {runtime}, which cannot be read "
                f"({type(error).__name__}); restore it: save the registration body for {runtime} "
                f"to {relative}"
            ) from None
        hooks = body.get("hooks") if isinstance(body, dict) else None
        if not isinstance(hooks, dict):
            raise RenderRefused(f"foreign hook {name}: {relative} holds no hooks object to render")
        out.append((name, hooks))
    return out


def merge(existing: Any, desired: Dict[str, Any]) -> Dict[str, Any]:
    """existing with every desired group an event does not already hold appended.

    Equality is on the whole group, so a registration whose body changed (a new
    timeout, a new matcher) is added beside the old one rather than silently
    treated as the same hook; the reverse pass then reports the stale copy.

    Events come out sorted, which is the order the Codex and Gemini CLI
    adapters write their own registries in (both build the merged mapping over
    sorted(all_events)) and both compare the rendered text, not the parsed
    document, when deciding whether to write. Matching them costs nothing and
    keeps this module from handing either one a file it would rewrite only to
    reorder.
    """
    merged: Dict[str, Any] = {
        event: list(groups) if isinstance(groups, list) else groups
        for event, groups in (existing or {}).items()
    } if isinstance(existing, dict) else {}
    for event, groups in desired.items():
        if not isinstance(groups, list):
            continue
        current = merged.get(event)
        if not isinstance(current, list):
            current = []
            merged[event] = current
        for group in groups:
            if group not in current:
                current.append(group)
    return {event: merged[event] for event in sorted(merged)}


def render(runtime: str, source: Path, target: Path, dry_run: bool) -> List[str]:
    """The actions writing this runtime's declared registrations takes."""
    name = registry_name(runtime)
    if name is None:
        return []
    entries = declared(source, runtime)
    if not entries:
        return []
    path = target / name
    document: Dict[str, Any] = {}
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RenderRefused(
                f"{name} does not parse ({type(error).__name__}); fix it by hand before "
                f"{', '.join(entry for entry, _ in entries)} can be registered"
            ) from None
        if not isinstance(document, dict):
            raise RenderRefused(f"{name} does not hold a JSON object; fix it by hand before registering a foreign hook")
    existing = document.get("hooks")
    merged = dict(existing) if isinstance(existing, dict) else {}
    for _entry, hooks in entries:
        merged = merge(merged, hooks)
    if isinstance(existing, dict) and merged == existing:
        return []
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**document, "hooks": merged}, indent=2) + "\n", encoding="utf-8")
    return [f"register foreign hook {entry} in {name}" for entry, _hooks in entries]
