"""Tests for the Claude Code runtime adapter."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import pytest


from stratarc.adapters.claude import owned_outputs, sync
from stratarc.adapters._common import home, owned_dir_entries, read_frontmatter


@pytest.fixture(autouse=True)
def _isolated_home(stratarc_home):
    """Every test reads the home through STRATARC_HOME, never the real one."""
    return stratarc_home



class TestClaudeAdapter(unittest.TestCase):
    def setUp(self):
        # Source and target share one private parent, so the check that the
        # adapter writes nothing beside its target lists a directory no other
        # process writes into, not the shared TMPDIR (G-05).
        self.parent_tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.parent_tmp.name)
        self.source = self.parent / "source"
        self.target = self.parent / "target"
        self.source.mkdir()
        self.target.mkdir()

    def tearDown(self):
        self.parent_tmp.cleanup()

    def _populate_source(self):
        """Build a realistic llm-root source tree."""
        # AGENTS.md
        (self.source / "AGENTS.md").write_text("# Global instructions\n")

        # rules/
        (self.source / "rules" / "global").mkdir(parents=True)
        (self.source / "rules" / "global" / "decisions.md").write_text(
            "---\nname: decisions\n---\nDecision rule.\n"
        )
        (self.source / "rules" / "common").mkdir(parents=True)
        (self.source / "rules" / "common" / "naming.md").write_text(
            "---\nname: naming\n---\nNaming convention.\n"
        )

        # hooks/
        (self.source / "hooks").mkdir()
        (self.source / "hooks" / "lib").mkdir()
        hook_script = self.source / "hooks" / "no-emdash-guard.sh"
        hook_script.write_text("#!/bin/bash\nexit 0\n")
        hook_script.chmod(hook_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        (self.source / "hooks" / "lib" / "log.sh").write_text("# logging\n")
        # Test file that should be excluded
        (self.source / "hooks" / "no-emdash-guard.test.sh").write_text("# test\n")
        # hooks.json
        hooks_data = {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "$HOME/.claude/hooks/no-emdash-guard.sh",
                            "timeout": 3,
                        }
                    ],
                }
            ]
        }
        (self.source / "hooks" / "hooks.json").write_text(
            json.dumps(hooks_data, indent=2)
        )

        # skills/
        (self.source / "skills" / "my-skill").mkdir(parents=True)
        (self.source / "skills" / "my-skill" / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: A test skill\n---\nSkill body.\n"
        )

        # commands/
        (self.source / "commands").mkdir()
        (self.source / "commands" / "commit.md").write_text(
            "---\ndescription: Commit helper\n---\nCommit prompt.\n"
        )

        # agents/
        (self.source / "agents").mkdir()
        (self.source / "agents" / "reviewer.md").write_text(
            "---\nname: reviewer\n---\nReviewer system prompt.\n"
        )

    def test_permissions_policy_replaces_permissions_and_preserves_unmanaged_settings(
        self,
    ):
        (self.source / "permissions.json").write_text(
            '{"schemaVersion":1,"allow":["Bash(git *)"],"deny":["Bash(rm -r *)"],"ask":[],"defaultMode":"bypassPermissions"}\n'
        )
        (self.target / "settings.json").write_text(
            '{"permissions":{"allow":["Bash(git *)"]},"native":true}\n'
        )

        sync(self.source, self.target)

        settings = json.loads((self.target / "settings.json").read_text())
        self.assertEqual(settings["permissions"]["defaultMode"], "bypassPermissions")
        self.assertEqual(settings["permissions"]["allow"], ["Bash(git *)"])
        self.assertEqual(settings["permissions"]["deny"], ["Bash(rm -r *)"])
        self.assertTrue(settings["native"])

    def test_auto_mode_block_renders_from_policy_and_replaces_stale_lists(self):
        (self.source / "permissions.json").write_text(
            '{"schemaVersion":1,"allow":[],"deny":[],"ask":[],"defaultMode":"bypassPermissions",'
            '"autoMode":{"allow":["$defaults","Inside a repository under ~/projects/, running tests is routine."],'
            '"environment":["$defaults","Trusted repo: every git repository under ~/projects/."]}}\n'
        )
        (self.target / "settings.json").write_text(
            '{"autoMode":{"allow":["$defaults","Inside a repository under ~/coding/, running tests is routine."],'
            '"soft_deny":["$defaults"]},"native":true}\n'
        )

        sync(self.source, self.target)

        settings = json.loads((self.target / "settings.json").read_text())
        self.assertEqual(
            settings["autoMode"]["allow"][1],
            "Inside a repository under ~/projects/, running tests is routine.",
        )
        self.assertEqual(
            settings["autoMode"]["environment"][1],
            "Trusted repo: every git repository under ~/projects/.",
        )
        self.assertEqual(settings["autoMode"]["soft_deny"], ["$defaults"])
        self.assertNotIn("~/coding", json.dumps(settings))
        self.assertTrue(settings["native"])

    def test_policy_without_auto_mode_leaves_runtime_auto_mode_alone(self):
        (self.source / "permissions.json").write_text(
            '{"schemaVersion":1,"allow":[],"deny":[],"ask":[],"defaultMode":"bypassPermissions"}\n'
        )
        (self.target / "settings.json").write_text(
            '{"autoMode":{"allow":["$defaults"]}}\n'
        )

        sync(self.source, self.target)

        settings = json.loads((self.target / "settings.json").read_text())
        self.assertEqual(settings["autoMode"], {"allow": ["$defaults"]})

    # ---- Rule 1: AGENTS.md copies ----

    def test_agents_md_copies(self):
        self._populate_source()
        sync(self.source, self.target)

        for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
            dst = self.target / name
            self.assertTrue(dst.exists(), f"{name} should exist")
            self.assertEqual(
                dst.read_bytes(),
                (self.source / "AGENTS.md").read_bytes(),
                f"{name} should be a byte copy of AGENTS.md",
            )

    # ---- Rule 2: the rule tree is not mirrored; earlier copies are removed ----

    def test_rules_are_not_mirrored(self):
        self._populate_source()
        sync(self.source, self.target)
        self.assertFalse((self.target / "rules").exists())

    def test_a_previously_mirrored_rule_tree_is_removed(self):
        """Claude Code loads every file under ~/.claude/rules/; the full rule
        tree there beside CLAUDE.md's digest pushed sessions past the
        150k-character instruction limit."""
        self._populate_source()
        (self.source / "rules" / "flat-rule.md").write_text("# flat-rule\n")
        (self.source / "rules" / "README.md").write_text("# README.MD\n")
        (self.target / "rules").mkdir()
        (self.target / "rules" / "flat-rule.md").write_text("old copy\n")
        (self.target / "rules" / "README.md").write_text("old copy\n")

        actions = sync(self.source, self.target)

        self.assertFalse((self.target / "rules").exists())
        self.assertIn(f"remove retired rule copy {self.target / 'rules' / 'flat-rule.md'}", actions)

    def test_a_file_the_source_never_shipped_survives_removal(self):
        self._populate_source()
        (self.source / "rules" / "flat-rule.md").write_text("# flat-rule\n")
        (self.target / "rules").mkdir()
        (self.target / "rules" / "flat-rule.md").write_text("old copy\n")
        (self.target / "rules" / "mine.md").write_text("operator file\n")

        sync(self.source, self.target)

        self.assertFalse((self.target / "rules" / "flat-rule.md").exists())
        self.assertEqual((self.target / "rules" / "mine.md").read_text(), "operator file\n")

    def test_dry_run_reports_without_removing(self):
        self._populate_source()
        (self.source / "rules" / "flat-rule.md").write_text("# flat-rule\n")
        (self.target / "rules").mkdir()
        (self.target / "rules" / "flat-rule.md").write_text("old copy\n")

        actions = sync(self.source, self.target, dry_run=True)

        self.assertTrue((self.target / "rules" / "flat-rule.md").exists())
        self.assertTrue(any(a.startswith("would remove retired rule copy") for a in actions))

    # ---- Rule 3: hooks mirror, exclude *.test.sh and hooks.json, preserve exec ----

    def test_hooks_mirror(self):
        self._populate_source()
        sync(self.source, self.target)

        hook_dst = self.target / "hooks" / "no-emdash-guard.sh"
        self.assertTrue(hook_dst.exists())
        # Exec bit preserved
        self.assertTrue(hook_dst.stat().st_mode & stat.S_IXUSR)

        # lib/ copied
        self.assertTrue((self.target / "hooks" / "lib" / "log.sh").exists())

        # *.test.sh excluded
        self.assertFalse((self.target / "hooks" / "no-emdash-guard.test.sh").exists())
        # hooks.json itself not copied as a file to hooks/
        self.assertFalse((self.target / "hooks" / "hooks.json").exists())

    # ---- Rule 4: hooks.json merges into settings.json ----

    def test_hooks_merge_into_settings(self):
        self._populate_source()
        # Pre-existing settings.json with other keys
        settings_path = self.target / "settings.json"
        settings_path.write_text(
            json.dumps({"theme": "dark", "hooks": {"old": "data"}}, indent=2)
        )

        sync(self.source, self.target)

        settings = json.loads(settings_path.read_text())
        # Other keys preserved
        self.assertEqual(settings["theme"], "dark")
        # Hooks key replaced entirely
        self.assertIn("PreToolUse", settings["hooks"])
        self.assertNotIn("old", settings["hooks"])

    def test_hooks_creates_settings_if_missing(self):
        self._populate_source()
        settings_path = self.target / "settings.json"
        self.assertFalse(settings_path.exists())

        sync(self.source, self.target)

        self.assertTrue(settings_path.exists())
        settings = json.loads(settings_path.read_text())
        self.assertIn("hooks", settings)
        self.assertIn("PreToolUse", settings["hooks"])

    def test_claude_worktree_lifecycle_hooks_merge_idempotently(self):
        self._populate_source()
        manifest = {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "$HOME/.claude/hooks/worktree-validate.sh",
                            "timeout": 3,
                        }
                    ],
                }
            ],
            "WorktreeCreate": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "$HOME/.claude/hooks/worktree-create.sh",
                            "timeout": 30,
                        }
                    ]
                }
            ],
            "WorktreeRemove": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "$HOME/.claude/hooks/worktree-remove.sh",
                            "timeout": 15,
                        }
                    ]
                }
            ],
        }
        (self.source / "hooks" / "claude-worktree-hooks.json").write_text(
            json.dumps(manifest)
        )
        for name in (
            "worktree-create.sh",
            "worktree-remove.sh",
            "worktree-validate.sh",
        ):
            (self.source / "hooks" / name).write_text("#!/usr/bin/env bash\nexit 0\n")
        (self.target / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "$HOME/.claude/hooks/worktree-hydrate.sh",
                                    }
                                ],
                            }
                        ],
                        "WorktreeCreate": [
                            {
                                "matcher": "",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "$HOME/.claude/scripts/worktree-create.sh",
                                    }
                                ],
                            }
                        ],
                    }
                }
            )
        )

        sync(self.source, self.target)
        settings_path = self.target / "settings.json"
        settings = json.loads(settings_path.read_text())
        self.assertEqual(
            set(manifest), {"SessionStart", "WorktreeCreate", "WorktreeRemove"}
        )
        for event, groups in manifest.items():
            self.assertEqual(settings["hooks"][event], groups)
        self.assertFalse(
            (self.target / "hooks" / "claude-worktree-hooks.json").exists()
        )

        self.assertEqual(sync(self.source, self.target), [])

    def test_agent_graph_hooks_register_for_claude_from_the_shipped_manifest(self):
        shipped = (
            Path(__file__).resolve().parents[1] / "stratarc" / "data" / "hooks" / "claude-agent-graph-hooks.json"
        )
        manifest = json.loads(shipped.read_text())
        self.assertEqual(set(manifest), {"SessionStart", "PreToolUse"})
        self._populate_source()
        (self.source / "hooks" / "claude-agent-graph-hooks.json").write_text(
            shipped.read_text()
        )
        for name in ("agent-graph-session-start.sh", "agent-graph-pre-edit.sh"):
            (self.source / "hooks" / name).write_text("#!/usr/bin/env bash\nexit 0\n")

        sync(self.source, self.target)

        settings = json.loads((self.target / "settings.json").read_text())
        commands = {
            hook["command"]
            for event in ("SessionStart", "PreToolUse")
            for group in settings["hooks"][event]
            for hook in group["hooks"]
        }
        self.assertIn("$HOME/.claude/hooks/agent-graph-session-start.sh", commands)
        self.assertIn("$HOME/.claude/hooks/agent-graph-pre-edit.sh", commands)
        pre_edit = [
            group
            for group in settings["hooks"]["PreToolUse"]
            if any("agent-graph-pre-edit.sh" in h["command"] for h in group["hooks"])
        ]
        self.assertEqual(len(pre_edit), 1)
        self.assertEqual(pre_edit[0]["matcher"], "Edit|Write")
        self.assertEqual(pre_edit[0]["hooks"][0]["timeout"], 8)
        # The launchers are mirrored; the manifest that registers them is not.
        self.assertTrue(
            (self.target / "hooks" / "agent-graph-session-start.sh").exists()
        )
        self.assertTrue((self.target / "hooks" / "agent-graph-pre-edit.sh").exists())
        self.assertFalse(
            (self.target / "hooks" / "claude-agent-graph-hooks.json").exists()
        )

        self.assertEqual(sync(self.source, self.target), [])

    def test_agent_graph_hooks_are_not_registered_without_the_manifest(self):
        self._populate_source()
        for name in ("agent-graph-session-start.sh", "agent-graph-pre-edit.sh"):
            (self.source / "hooks" / name).write_text("#!/usr/bin/env bash\nexit 0\n")

        sync(self.source, self.target)

        settings = json.loads((self.target / "settings.json").read_text())
        self.assertNotIn("agent-graph-", json.dumps(settings["hooks"]))

    # ---- Rule 5: skills add/update, never delete ----

    def test_skills_add_update_no_delete(self):
        self._populate_source()
        # Pre-existing runtime-only skill
        (self.target / "skills" / "user-local").mkdir(parents=True)
        (self.target / "skills" / "user-local" / "SKILL.md").write_text("local\n")

        sync(self.source, self.target)

        # Source skill copied
        self.assertTrue((self.target / "skills" / "my-skill" / "SKILL.md").exists())
        # Runtime-only skill NOT deleted
        self.assertTrue((self.target / "skills" / "user-local" / "SKILL.md").exists())

    # ---- Rule 6: commands mirror ----

    def test_commands_mirror(self):
        self._populate_source()
        (self.target / "commands").mkdir(parents=True)
        (self.target / "commands" / "stale-cmd.md").write_text("stale\n")

        sync(self.source, self.target)

        self.assertTrue((self.target / "commands" / "commit.md").exists())
        self.assertFalse((self.target / "commands" / "stale-cmd.md").exists())

    # ---- Rule 7: agents mirror ----

    def test_agents_mirror(self):
        self._populate_source()
        (self.target / "agents").mkdir(parents=True)
        (self.target / "agents" / "stale-agent.md").write_text("stale\n")

        sync(self.source, self.target)

        self.assertTrue((self.target / "agents" / "reviewer.md").exists())
        self.assertFalse((self.target / "agents" / "stale-agent.md").exists())

    # ---- Rule 8: never writes outside target ----

    def _start_shared_tmpdir_writer(self):
        """Start a separate process that creates a directory in the shared TMPDIR.

        It stands in for another session's test suite running on the same
        machine, which is what made this check fail intermittently (G-05).
        """
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, tempfile;"
                "print(tempfile.mkdtemp(prefix='claude-test-concurrent-', dir=sys.argv[1]))",
                tempfile.gettempdir(),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )

    def _finish_shared_tmpdir_writer(self, writer):
        out, _ = writer.communicate(timeout=30)
        self.assertEqual(writer.returncode, 0, "concurrent writer failed")
        written = Path(out.strip())
        self.addCleanup(shutil.rmtree, written, True)
        self.assertTrue(written.is_dir(), f"concurrent writer made nothing: {out!r}")

    def _assert_sync_writes_nothing_beside_target(self, concurrent_writer=False):
        self._populate_source()
        parent = self.parent
        before = set()
        for item in parent.iterdir():
            before.add(item.name)

        writer = self._start_shared_tmpdir_writer() if concurrent_writer else None
        sync(self.source, self.target)
        if writer is not None:
            self._finish_shared_tmpdir_writer(writer)

        after = set()
        for item in parent.iterdir():
            after.add(item.name)

        # Only the target dir itself should exist; no new siblings
        new_items = after - before
        self.assertEqual(new_items, set(), f"New items outside target: {new_items}")

    def test_never_writes_outside_target(self):
        self._assert_sync_writes_nothing_beside_target()

    def test_never_writes_outside_target_with_concurrent_tmpdir_writer(self):
        self._assert_sync_writes_nothing_beside_target(concurrent_writer=True)

    # ---- Rule 8: protected paths untouched ----

    def test_protected_paths_untouched(self):
        self._populate_source()
        # Create protected paths in target
        for name in ("settings.local.json", "projects", "plugins"):
            p = self.target / name
            if name.endswith(".json"):
                p.write_text("{}")
            else:
                p.mkdir()
                (p / "marker").write_text("keep\n")

        sync(self.source, self.target)

        self.assertTrue((self.target / "settings.local.json").exists())
        self.assertTrue((self.target / "projects" / "marker").exists())
        self.assertTrue((self.target / "plugins" / "marker").exists())

    # ---- Dry run ----

    def test_dry_run_no_changes(self):
        self._populate_source()
        actions = sync(self.source, self.target, dry_run=True)

        # Actions should be reported
        self.assertTrue(len(actions) > 0)
        for a in actions:
            self.assertTrue(
                a.startswith("would "), f"Dry-run action should start with 'would': {a}"
            )

        # Nothing should be written
        self.assertFalse((self.target / "AGENTS.md").exists())
        self.assertFalse((self.target / "rules").exists())

    # ---- Idempotency ----

    def test_idempotent_second_run(self):
        self._populate_source()
        sync(self.source, self.target)
        actions = sync(self.source, self.target)
        # Second run should produce no actions (everything up to date)
        self.assertEqual(
            actions, [], f"Expected no actions on second run, got: {actions}"
        )

    # ---- read_frontmatter ----

    def test_read_frontmatter_basic(self):
        text = "---\nname: test\ndescription: A test\n---\nBody text.\n"
        meta, body = read_frontmatter(text)
        self.assertEqual(meta["name"], "test")
        self.assertEqual(meta["description"], "A test")
        self.assertIn("Body text.", body)

    def test_read_frontmatter_list(self):
        text = "---\nallowed-tools: [Bash, Read, Write]\n---\nBody.\n"
        meta, body = read_frontmatter(text)
        self.assertEqual(meta["allowed-tools"], ["Bash", "Read", "Write"])

    def test_read_frontmatter_no_frontmatter(self):
        text = "Just plain text.\n"
        meta, body = read_frontmatter(text)
        self.assertEqual(meta, {})
        self.assertEqual(body, text)


class TestForeignHookGroupRepoint(unittest.TestCase):
    """F-17: a preserved foreign group's mislocated source script is
    repointed on sync(); a live foreign group and a dangling one survive
    unchanged (sync() never deletes a registration; that is --prune's
    job, driven by the classification in sync.py's reverse pass).
    """

    def setUp(self):
        self.src_tmp = tempfile.TemporaryDirectory()
        self.dst_tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.src_tmp.name)
        self.target = Path(self.dst_tmp.name)
        (self.source / "hooks").mkdir()
        # A shipped name outside claude.py's own legacy_worktree_scripts
        # and legacy_hook_scripts sets, so its "ours" check does not
        # already match a "/scripts/<name>" path the way it deliberately
        # does for the worktree-*.sh migration names; this isolates the
        # new repoint mechanism from that existing, broader match.
        (self.source / "hooks" / "guard.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n"
        )
        # The hooks.json merge block only runs when source ships at least
        # one group; UserPromptSubmit is unused by the fixtures below, so
        # it exercises that gate without interacting with them.
        (self.source / "hooks" / "hooks.json").write_text(
            json.dumps(
                {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "$HOME/.claude/hooks/guard.sh",
                                }
                            ]
                        }
                    ]
                }
            )
        )

    def tearDown(self):
        self.src_tmp.cleanup()
        self.dst_tmp.cleanup()

    def test_mislocated_group_is_repointed_live_and_dangling_survive(self):
        home_dir = str(home())
        mislocated_cmd = f"{home_dir}/.codex/scripts/guard.sh"
        dangling_cmd = f"{home_dir}/.claude/hooks/regen-surfaces.sh"
        live_cmd = (
            f"if [ -x '{home_dir}/.orca/agent-hooks/tool.sh' ]; "
            f"then '{home_dir}/.orca/agent-hooks/tool.sh'; fi"
        )
        (self.target / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"hooks": [{"type": "command", "command": mislocated_cmd}]}
                        ],
                        "PostToolUse": [
                            {"hooks": [{"type": "command", "command": dangling_cmd}]},
                            {"hooks": [{"type": "command", "command": live_cmd}]},
                        ],
                    }
                }
            )
        )

        sync(self.source, self.target)

        settings = json.loads((self.target / "settings.json").read_text())
        pre_commands = [
            h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]
        ]
        self.assertIn("$HOME/.claude/hooks/guard.sh", pre_commands)
        self.assertNotIn(mislocated_cmd, pre_commands)

        post_commands = [
            h["command"] for g in settings["hooks"]["PostToolUse"] for h in g["hooks"]
        ]
        self.assertIn(dangling_cmd, post_commands)
        self.assertIn(live_cmd, post_commands)


class TestRuntimeSettings(unittest.TestCase):
    """runtime_settings.claude in the staged components.json is the source
    for plain settings keys and the default models (C-28 of
    docs/adr/2026-09-23-load-only-selected-components.md)."""

    def setUp(self):
        self.src_tmp = tempfile.TemporaryDirectory()
        self.dst_tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.src_tmp.name)
        self.target = Path(self.dst_tmp.name)

    def tearDown(self):
        self.src_tmp.cleanup()
        self.dst_tmp.cleanup()

    def _write_claude(self, claude: dict) -> None:
        (self.source / "components.json").write_text(
            json.dumps({"version": 1, "runtime_settings": {"claude": claude}}), encoding="utf-8"
        )

    def _write_runtime_settings(self, data: dict) -> None:
        self._write_claude({"settings": data})

    def test_the_retired_runtime_settings_directory_is_not_read(self):
        (self.source / "runtime-settings").mkdir()
        (self.source / "runtime-settings" / "claude.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "old.sh"}}), encoding="utf-8"
        )
        sync(self.source, self.target)
        self.assertFalse((self.target / "settings.json").is_file())

    def test_default_models_are_written_to_their_own_keys(self):
        self._write_claude({"model": "opus", "subagent_model": "sonnet"})
        sync(self.source, self.target)
        settings = self._settings()
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "sonnet")

    def test_a_model_alias_is_translated_for_the_default(self):
        self._write_claude({"model": "opus", "aliases": {"opus": "claude-opus-5-5"}})
        sync(self.source, self.target)
        self.assertEqual(self._settings()["model"], "claude-opus-5-5")

    def _settings(self) -> dict:
        path = self.target / "settings.json"
        self.assertTrue(path.is_file(), "settings.json was not written")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_status_line_is_rendered(self):
        self._write_runtime_settings(
            {"statusLine": {"type": "command", "command": "$HOME/x.sh"}}
        )
        sync(self.source, self.target)
        settings = self._settings()
        self.assertEqual(settings["statusLine"]["type"], "command")
        self.assertEqual(settings["statusLine"]["command"], "$HOME/x.sh")

    def test_unrelated_existing_keys_survive(self):
        (self.target / "settings.json").write_text(
            json.dumps({"theme": "dark", "feedbackSurveyState": {"seen": True}}),
            encoding="utf-8",
        )
        self._write_runtime_settings({"statusLine": {"type": "command", "command": "x"}})
        sync(self.source, self.target)
        settings = self._settings()
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["feedbackSurveyState"], {"seen": True})
        self.assertIn("statusLine", settings)

    def test_source_wins_over_existing_value(self):
        (self.target / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "stale.sh"}}),
            encoding="utf-8",
        )
        self._write_runtime_settings(
            {"statusLine": {"type": "command", "command": "fresh.sh"}}
        )
        sync(self.source, self.target)
        self.assertEqual(self._settings()["statusLine"]["command"], "fresh.sh")

    def test_cleanup_period_days_is_rendered_from_runtime_settings(self):
        """docs/adr/2026-09-25-keep-every-session-log.md: Claude Code deletes
        transcripts after cleanupPeriodDays (default 30). The manifest's
        passthrough settings key is how llm-root raises it, the same path
        statusLine already takes."""
        self._write_runtime_settings({"cleanupPeriodDays": 36500})
        sync(self.source, self.target)
        self.assertEqual(self._settings()["cleanupPeriodDays"], 36500)

    def test_cleanup_period_days_overwrites_a_shorter_existing_value(self):
        (self.target / "settings.json").write_text(
            json.dumps({"cleanupPeriodDays": 30}), encoding="utf-8"
        )
        self._write_runtime_settings({"cleanupPeriodDays": 36500})
        sync(self.source, self.target)
        self.assertEqual(self._settings()["cleanupPeriodDays"], 36500)

    def test_absent_file_writes_no_settings(self):
        sync(self.source, self.target)
        self.assertFalse((self.target / "settings.json").is_file())

    def test_malformed_file_is_ignored(self):
        (self.source / "components.json").write_text("{not json", encoding="utf-8")
        sync(self.source, self.target)
        self.assertFalse((self.target / "settings.json").is_file())

    def test_rerun_is_idempotent(self):
        self._write_runtime_settings({"statusLine": {"type": "command", "command": "x"}})
        sync(self.source, self.target)
        first = self._settings()
        actions = sync(self.source, self.target)
        self.assertEqual(self._settings(), first)
        self.assertFalse(
            [a for a in actions if "settings.json" in a and "render" in a],
            "a second sync should not rewrite unchanged settings",
        )


class TestPluginEnablement(unittest.TestCase):
    """C-22: the adapter owns enabledPlugins as a whole and writes every
    installed plugin, true only when the global column selects it."""

    def setUp(self):
        self.src_tmp = tempfile.TemporaryDirectory()
        self.dst_tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.src_tmp.name)
        self.target = Path(self.dst_tmp.name)
        (self.target / "plugins").mkdir(parents=True)

    def tearDown(self):
        self.src_tmp.cleanup()
        self.dst_tmp.cleanup()

    def _install(self, *keys: str) -> None:
        (self.target / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {key: [{"scope": "user"}] for key in keys}}), encoding="utf-8"
        )

    def _stage(self, *selected: str) -> None:
        plugins = [
            {"name": name, "runtimes": ["claude"], "owner": "anthropic", "wanted": True, "marketplace": "market"}
            for name in selected
        ]
        (self.source / "components.json").write_text(
            json.dumps({"version": 1, "mcp_servers": [], "plugins": plugins}), encoding="utf-8"
        )

    def _enabled(self) -> dict:
        return json.loads((self.target / "settings.json").read_text(encoding="utf-8"))["enabledPlugins"]

    def test_selected_declared_unselected_and_undeclared_read_true_false_false(self):
        # declared-off is declared in the manifest but its global cell is
        # clear, so the stage (which holds only selected entries) omits it.
        self._install("chosen@market", "declared-off@market", "stray@elsewhere")
        self._stage("chosen")
        sync(self.source, self.target)
        self.assertEqual(
            self._enabled(),
            {"chosen@market": True, "declared-off@market": False, "stray@elsewhere": False},
        )

    def test_a_hand_enabled_key_is_overwritten_and_other_settings_survive(self):
        self._install("stray@elsewhere")
        (self.target / "settings.json").write_text(
            json.dumps({"theme": "dark", "enabledPlugins": {"stray@elsewhere": True, "gone@old": True}}),
            encoding="utf-8",
        )
        self._stage()
        actions = sync(self.source, self.target, dry_run=True)
        self.assertTrue(any("enabledPlugins" in a for a in actions), actions)
        sync(self.source, self.target)
        settings = json.loads((self.target / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["enabledPlugins"], {"gone@old": False, "stray@elsewhere": False})

    def test_a_selected_plugin_not_yet_installed_is_written_on(self):
        self._stage("chosen")
        sync(self.source, self.target)
        self.assertEqual(self._enabled(), {"chosen@market": True})

    def test_nothing_installed_or_selected_writes_no_key(self):
        sync(self.source, self.target)
        self.assertFalse((self.target / "settings.json").is_file())

    def test_second_sync_is_current(self):
        self._install("chosen@market", "stray@elsewhere")
        self._stage("chosen")
        sync(self.source, self.target)
        self.assertEqual(sync(self.source, self.target), [])

    def test_container_plugin_remains_enabled_after_repeated_sync(self):
        key = "telegram@claude-plugins-official"
        self._install(key)
        (self.source / "components.json").write_text(json.dumps({
            "version": 1,
            "runtime_settings": {"claude": {"environment_plugins": {"container": [key]}}},
            "plugins": [],
        }), encoding="utf-8")
        with patch.dict(os.environ, {"STRATARC_ENVIRONMENT": "container"}):
            sync(self.source, self.target)
            self.assertEqual(self._enabled()[key], True)
            self.assertEqual(sync(self.source, self.target), [])
        with patch.dict(os.environ, {"STRATARC_ENVIRONMENT": "mac"}):
            sync(self.source, self.target)
            self.assertEqual(self._enabled()[key], False)

    def test_resolved_environment_enables_plugin_without_environment_variable(self):
        key = "telegram@claude-plugins-official"
        self._install(key)
        (self.source / "components.json").write_text(json.dumps({
            "version": 1,
            "runtime_settings": {"claude": {"environment_plugins": {"container": [key]}}},
            "plugins": [],
        }), encoding="utf-8")
        with patch.dict(os.environ, {"STRATARC_ENVIRONMENT": ""}):
            sync(self.source, self.target, environment="container")
        self.assertEqual(self._enabled()[key], True)

    def test_an_unreadable_registry_refuses_rather_than_guess(self):
        from stratarc.adapters._components import RenderRefused

        (self.target / "plugins" / "installed_plugins.json").write_text("{broken", encoding="utf-8")
        self._stage("chosen")
        with self.assertRaises(RenderRefused):
            sync(self.source, self.target)

    def test_a_parseable_registry_without_a_plugins_object_refuses(self):
        from stratarc.adapters._components import RenderRefused

        self._stage()
        for data in (None, [], {}, {"plugins": None}, {"plugins": []}):
            with self.subTest(data=data):
                (self.target / "plugins" / "installed_plugins.json").write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(RenderRefused):
                    sync(self.source, self.target)


class TestGlobalColumnAcceptance(unittest.TestCase):
    """The WI-49 acceptance on a fixture: a control plane whose global column
    selects one plugin and one project column selects another, staged and
    synced, leaves ~/.claude/settings.json enabling only the global column's
    plugin, so a fresh session in llm-root (never a sync-projects target)
    loads that plugin alone; and it writes no MCP server at user scope."""

    CONTROL_PLANE = "\n".join(
        [
            "# control-plane",
            "",
            "## projects",
            "",
            "| project | status | tier | template | origin |",
            "|---|---|---|---|---|",
            "| proj | active | normal | base | - |",
            "",
            "## shared",
            "",
            "| option | global | proj |",
            "|---|---|---|",
            "| [mcp:srv-global](components.json) | x |  |",
            "| [mcp:srv-proj](components.json) |  | x |",
            "| [plugin:plug-global](components.json) | x |  |",
            "| [plugin:plug-proj](components.json) |  | x |",
            "",
        ]
    )

    def test_only_the_global_column_loads(self):
        from stratarc import staging
        from stratarc.control_plane import ControlPlane

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "llm-root"
            root.mkdir()
            (root / "control-plane.md").write_text(self.CONTROL_PLANE)

            def plugin(name):
                return {"name": name, "runtimes": ["claude"], "owner": "anthropic", "wanted": True, "marketplace": "m"}

            def server(name):
                return {"name": name, "runtimes": ["claude"], "owner": "stratarc", "wanted": True, "command": name}

            (root / "components.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "plugins": [plugin("plug-global"), plugin("plug-proj")],
                        "mcp_servers": [server("srv-global"), server("srv-proj")],
                    }
                )
            )
            home = base / "home"
            target = home / ".claude"
            (target / "plugins").mkdir(parents=True)
            (target / "plugins" / "installed_plugins.json").write_text(
                json.dumps({"plugins": {"plug-global@m": [], "plug-proj@m": [], "stray@elsewhere": []}})
            )
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
            stage, _notes = staging.build_stage(root, ControlPlane.load(root / "control-plane.md"), "global")
            try:
                sync(stage, target)
            finally:
                staging.cleanup_all()
            settings = json.loads((target / "settings.json").read_text())
            self.assertEqual(
                settings["enabledPlugins"],
                {"plug-global@m": True, "plug-proj@m": False, "stray@elsewhere": False},
            )
            self.assertEqual(json.loads((home / ".claude.json").read_text()), {"mcpServers": {}})


class TestOwnedOutputs(unittest.TestCase):
    """F-16: owned_outputs names exactly what sync() itself would write."""

    def setUp(self):
        self.src_tmp = tempfile.TemporaryDirectory()
        self.dst_tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.src_tmp.name)
        self.target = Path(self.dst_tmp.name)

    def tearDown(self):
        self.src_tmp.cleanup()
        self.dst_tmp.cleanup()

    def test_owned_directories_match_a_fixture_source(self):
        (self.source / "rules").mkdir()
        (self.source / "rules" / "a.md").write_text("a\n")
        (self.source / "hooks").mkdir()
        (self.source / "hooks" / "guard.sh").write_text("#!/bin/sh\n")
        (self.source / "hooks" / "guard.test.sh").write_text("#!/bin/sh\n")
        (self.source / "commands").mkdir()
        (self.source / "commands" / "c.md").write_text("c\n")
        (self.source / "agents").mkdir()
        (self.source / "agents" / "r.md").write_text("r\n")
        (self.source / "skills" / "s1").mkdir(parents=True)
        (self.source / "skills" / "s1" / "SKILL.md").write_text("s\n")

        owned = {od.kind: od for od in owned_outputs(self.source, self.target)}

        self.assertEqual(owned["rule"].path, self.target / "rules")
        # sync() mirrors no rule, so any file left in target/rules/ is an orphan.
        self.assertEqual(owned["rule"].names, frozenset())
        self.assertEqual(owned["hook"].path, self.target / "hooks")
        self.assertEqual(owned["hook"].names, frozenset({"guard.sh"}))
        self.assertEqual(owned["command"].names, frozenset({"c.md"}))
        self.assertEqual(owned["agent"].names, frozenset({"r.md"}))
        self.assertEqual(owned["skill"].path, self.target / "skills")
        self.assertTrue(owned["skill"].per_item)
        self.assertEqual(owned["skill"].names, frozenset({"s1"}))

    def test_a_synced_orphan_free_target_reports_no_orphans(self):
        self._populate_source()
        sync(self.source, self.target)
        for od in owned_outputs(self.source, self.target):
            present = owned_dir_entries(od.path, per_item=od.per_item)
            self.assertEqual(present - od.names - od.exclude, set())

    def test_a_target_only_file_is_reported_as_an_orphan(self):
        self._populate_source()
        sync(self.source, self.target)
        (self.target / "commands" / "stale.md").write_text("stale\n")
        owned = {od.kind: od for od in owned_outputs(self.source, self.target)}
        present = owned_dir_entries(owned["command"].path)
        orphans = present - owned["command"].names - owned["command"].exclude
        self.assertEqual(orphans, {"stale.md"})

    def test_excluded_skill_is_reported_separately_from_orphans(self):
        self._populate_source()
        (self.source / "skills" / "sync-exclude.json").write_text(
            '{"exclude": {"my-skill": "host-adapted"}}\n'
        )
        (self.target / "skills" / "my-skill").mkdir(parents=True)
        (self.target / "skills" / "my-skill" / "SKILL.md").write_text("kept\n")
        owned = {od.kind: od for od in owned_outputs(self.source, self.target)}
        skill_dir = owned["skill"]
        self.assertNotIn("my-skill", skill_dir.names)
        self.assertIn("my-skill", skill_dir.exclude)
        present = owned_dir_entries(skill_dir.path, per_item=True)
        self.assertEqual(present & skill_dir.exclude, {"my-skill"})
        self.assertEqual(present - skill_dir.names - skill_dir.exclude, set())

    def test_the_claude_managed_synced_bucket_is_excluded_not_orphan(self):
        """The `synced` skill directory is Claude Code's own skill-sync bucket,
        recreated by the runtime itself and never produced by this repo's
        source. It must read as `excluded`, never `orphan`, so `--prune`
        does not delete content the runtime repopulates on its own."""
        self._populate_source()
        sync(self.source, self.target)
        bucket = self.target / "skills" / "synced" / "00000000000000000000000000000000"
        bucket.mkdir(parents=True)
        (bucket / "manifest.json").write_text(
            '{"source": "anthropic-example", "creatorType": "anthropic"}\n'
        )
        owned = {od.kind: od for od in owned_outputs(self.source, self.target)}
        skill_dir = owned["skill"]
        self.assertIn("synced", skill_dir.exclude)
        present = owned_dir_entries(skill_dir.path, per_item=True)
        self.assertEqual(present & skill_dir.exclude, {"synced"})
        self.assertEqual(present - skill_dir.names - skill_dir.exclude, set())

    def _populate_source(self):
        (self.source / "AGENTS.md").write_text("# Global instructions\n")
        (self.source / "rules").mkdir()
        (self.source / "rules" / "a.md").write_text("a\n")
        (self.source / "hooks").mkdir()
        (self.source / "hooks" / "guard.sh").write_text("#!/bin/sh\n")
        (self.source / "commands").mkdir()
        (self.source / "commands" / "c.md").write_text("c\n")
        (self.source / "agents").mkdir()
        (self.source / "agents" / "r.md").write_text("r\n")
        (self.source / "skills" / "s1").mkdir(parents=True)
        (self.source / "skills" / "s1" / "SKILL.md").write_text("s\n")


class TestRepoInstructionSize(unittest.TestCase):
    """Claude Code refuses to load more than 150k characters of instruction
    files. The user scope this adapter writes must leave a project most of
    that budget: CLAUDE.md carries the binding digest and no rule tree loads
    beside it."""

    REPO = Path(__file__).resolve().parents[1] / "examples" / "notes-cli" / "source"

    def test_the_example_user_scope_stays_small(self):
        with tempfile.TemporaryDirectory() as target:
            sync(self.REPO, Path(target))
            loaded = [Path(target) / "CLAUDE.md"] + sorted((Path(target) / "rules").rglob("*.md"))
            size = sum(len(p.read_text(encoding="utf-8")) for p in loaded)
        self.assertLess(size, 40_000)
