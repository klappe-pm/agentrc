"""A sixth runtime adapter, used only by the runtime registry tests.

The one-runtime-registry design requires that a fixture adapter directory holding a sixth adapter is accepted by
the components, launcher and sync code without other edits. The tests
copy the real adapter files and this one into a temporary directory; nothing
imports this module.
"""

RUNTIME = {
    "name": "sixth",
    "target": ".sixth",
    "hook_registry": "hooks.json",
    "hook_events": ["PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "SessionStart"],
}


def sync(source, target, dry_run=False):
    return []


def owned_outputs(source, target):
    return []
