"""Tests for _foreign.py, the renderer for wanted foreign hook registrations.

A foreign tool's own installer can write into the runtime directories the sync owns,
so what it would write is captured from a fixture home, declared in
components.json, and written by each adapter instead. Every registration here
is a synthetic capture for a neutral tool, "reviewbot", in the runtime's own shape.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest


from agentrc.adapters import _foreign, claude, codex, cursor, gemini
from agentrc.adapters._components import RenderRefused


@pytest.fixture(autouse=True)
def _isolated_home(agentrc_home):
    """Every test reads the home through AGENTRC_HOME, never the real one."""
    return agentrc_home


# The four synthetic captured bodies, one per runtime shape.
CAPTURED = Path(__file__).resolve().parent / "fixtures" / "adapters" / "foreign"

ENTRY = {
    "name": "reviewbot-hook",
    "runtimes": ["claude", "codex", "cursor", "gemini"],
    "owner": "reviewbot",
    "wanted": True,
    "match": "reviewbot hook run",
    "registration": {
        "claude": "captures/claude.json",
        "codex": "captures/codex.json",
        "cursor": "captures/cursor.json",
        "gemini": "captures/gemini.json",
    },
}

# The registry file each runtime's registrations live in, as the runtime
# registry declares it.
REGISTRY = {"claude": "settings.json", "codex": "hooks.json", "cursor": "hooks.json", "gemini": "settings.json"}


def stage(directory: Path, entry=ENTRY) -> Path:
    """A stage holding the manifest selection and the captured bodies, as
    the staging step builds it."""
    source = directory / "stage"
    source.mkdir(parents=True, exist_ok=True)
    (source / "components.json").write_text(json.dumps({"version": 1, "foreign_hooks": [entry]}), encoding="utf-8")
    for relative in (entry.get("registration") or {}).values():
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((CAPTURED / Path(relative).name).read_text(encoding="utf-8"), encoding="utf-8")
    return source


def registry(target: Path, runtime: str) -> dict:
    path = target / REGISTRY[runtime]
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def commands(document: dict) -> list:
    """Every hook command the registry holds, flat shape and grouped shape alike."""
    found = []
    for groups in (document.get("hooks") or {}).values():
        for group in groups if isinstance(groups, list) else []:
            hooks = group.get("hooks") if isinstance(group.get("hooks"), list) else [group]
            found.extend(str(hook.get("command", "")) for hook in hooks if isinstance(hook, dict))
    return found


class TestRender(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = stage(self.directory)

    def target(self, runtime: str) -> Path:
        path = self.directory / runtime
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_every_runtime_gets_its_own_captured_body_verbatim(self):
        for runtime in REGISTRY:
            with self.subTest(runtime=runtime):
                target = self.target(runtime)
                actions = _foreign.render(runtime, self.source, target, dry_run=False)
                self.assertTrue(actions, "nothing was rendered")
                captured = json.loads((CAPTURED / f"{runtime}.json").read_text(encoding="utf-8"))["hooks"]
                self.assertEqual(registry(target, runtime)["hooks"], captured)

    def test_the_agent_profile_reaches_the_command(self):
        # Each profile runs its own `--agent <profile>`; rendering one runtime's
        # body into another would silently register the wrong agent.
        for runtime in REGISTRY:
            with self.subTest(runtime=runtime):
                target = self.target(runtime)
                _foreign.render(runtime, self.source, target, dry_run=False)
                for command in commands(registry(target, runtime)):
                    self.assertIn(f"--agent {runtime}", command)

    def test_captured_commands_resolve_the_tool_from_path(self):
        for runtime in REGISTRY:
            with self.subTest(runtime=runtime):
                body = json.loads((CAPTURED / f"{runtime}.json").read_text(encoding="utf-8"))
                for groups in body["hooks"].values():
                    for group in groups:
                        hooks = group.get("hooks", [group])
                        for hook in hooks:
                            self.assertTrue(hook["command"].startswith("reviewbot hook run "))
                            if "commandWindows" in hook:
                                self.assertTrue(hook["commandWindows"].startswith("reviewbot hook run "))

    def test_a_second_render_changes_nothing(self):
        for runtime in REGISTRY:
            with self.subTest(runtime=runtime):
                target = self.target(runtime)
                _foreign.render(runtime, self.source, target, dry_run=False)
                before = (target / REGISTRY[runtime]).read_text(encoding="utf-8")
                self.assertEqual(_foreign.render(runtime, self.source, target, dry_run=False), [])
                self.assertEqual((target / REGISTRY[runtime]).read_text(encoding="utf-8"), before)

    def test_dry_run_reports_the_action_and_writes_nothing(self):
        target = self.target("claude")
        self.assertTrue(_foreign.render("claude", self.source, target, dry_run=True))
        self.assertFalse((target / "settings.json").exists())

    def test_everything_else_in_the_registry_survives(self):
        target = self.target("claude")
        (target / "settings.json").write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {"Stop": [{"hooks": [{"command": "$HOME/.claude/hooks/ours.sh", "type": "command"}]}]},
                }
            ),
            encoding="utf-8",
        )
        _foreign.render("claude", self.source, target, dry_run=False)
        document = registry(target, "claude")
        self.assertEqual(document["model"], "opus")
        self.assertIn("$HOME/.claude/hooks/ours.sh", commands(document))
        self.assertIn(
            "reviewbot hook run --agent claude '--source=reviewbot-hook'",
            commands(document),
        )

    def test_an_unwanted_entry_renders_nothing(self):
        source = stage(self.directory / "unwanted", dict(ENTRY, wanted=False, registration=None))
        self.assertEqual(_foreign.render("claude", source, self.target("claude"), dry_run=False), [])

    def test_a_runtime_the_entry_does_not_name_renders_nothing(self):
        source = stage(self.directory / "narrow", dict(ENTRY, runtimes=["claude"], registration={"claude": "captures/claude.json"}))
        self.assertEqual(_foreign.render("codex", source, self.target("codex"), dry_run=False), [])

    def test_opencode_renders_nothing_because_it_has_no_registry(self):
        # The tool has no OpenCode agent profile, and OpenCode has no JSON hook
        # registry to write one into even if it did.
        self.assertEqual(_foreign.render("opencode", self.source, self.target("opencode"), dry_run=False), [])

    def test_a_registry_that_does_not_parse_is_refused_rather_than_overwritten(self):
        target = self.target("codex")
        (target / "hooks.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(RenderRefused) as refused:
            _foreign.render("codex", self.source, target, dry_run=False)
        self.assertIn("hooks.json", str(refused.exception))
        self.assertEqual((target / "hooks.json").read_text(encoding="utf-8"), "{not json")

    def test_a_missing_captured_file_is_refused_rather_than_skipped(self):
        source = stage(self.directory / "gone")
        (source / "captures" / "claude.json").unlink()
        with self.assertRaises(RenderRefused) as refused:
            _foreign.render("claude", source, self.target("claude"), dry_run=False)
        self.assertIn("save the registration body for claude", str(refused.exception))
        self.assertIn("captures/claude.json", str(refused.exception))


class TestAdaptersKeepIt(unittest.TestCase):
    """Each adapter renders the registration last and preserves it on the next
    sync. An adapter that normalizes a preserved foreign entry (dropping
    Codex's commandWindows, Cursor's failClosed, Gemini's millisecond timeout)
    would make the second sync report work every time, so the discriminating
    check is that it reports none."""

    ADAPTERS = {"claude": claude, "codex": codex, "cursor": cursor, "gemini": gemini}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = stage(self.directory)
        hooks = self.source / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "guard.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (hooks / "hooks.json").write_text(
            json.dumps(
                {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/guard.sh", "timeout": 3}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_one_sync_registers_it_and_the_next_is_current(self):
        for runtime, adapter in self.ADAPTERS.items():
            with self.subTest(runtime=runtime):
                target = self.directory / runtime
                target.mkdir(parents=True, exist_ok=True)
                adapter.sync(self.source, target)
                rendered = commands(registry(target, runtime))
                self.assertIn(
                    f"reviewbot hook run --agent {runtime} '--source=reviewbot-hook'",
                    rendered,
                )
                before = (target / REGISTRY[runtime]).read_text(encoding="utf-8")
                adapter.sync(self.source, target)
                self.assertEqual(
                    (target / REGISTRY[runtime]).read_text(encoding="utf-8"),
                    before,
                    f"{runtime} rewrote its registry on a second sync",
                )

    def test_the_adapter_keeps_its_own_hooks_too(self):
        for runtime, adapter in self.ADAPTERS.items():
            with self.subTest(runtime=runtime):
                target = self.directory / runtime
                target.mkdir(parents=True, exist_ok=True)
                adapter.sync(self.source, target)
                self.assertTrue(
                    any("guard.sh" in command for command in commands(registry(target, runtime))),
                    f"{runtime} lost its own hook",
                )
