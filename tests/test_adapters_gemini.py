"""Tests for the Gemini CLI runtime adapter.

All tests use temporary directories -- never writes to ~/.gemini.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import tempfile
import unittest
from pathlib import Path

import pytest

from agentrc.adapters import gemini  # noqa: E402
from agentrc.adapters._common import owned_dir_entries  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(agentrc_home):
    """Every test reads the home through AGENTRC_HOME, never the real one."""
    return agentrc_home



class TestEventMapping(unittest.TestCase):
    """Verify the Claude -> Gemini event map."""

    def test_pretooluse_maps_to_beforetool(self):
        self.assertEqual(gemini.EVENT_MAP["PreToolUse"], "BeforeTool")

    def test_posttooluse_maps_to_aftertool(self):
        self.assertEqual(gemini.EVENT_MAP["PostToolUse"], "AfterTool")

    def test_stop_maps_to_afteragent(self):
        self.assertEqual(gemini.EVENT_MAP["Stop"], "AfterAgent")

    def test_sessionstart_preserved(self):
        self.assertEqual(gemini.EVENT_MAP["SessionStart"], "SessionStart")

    def test_sessionend_preserved(self):
        self.assertEqual(gemini.EVENT_MAP["SessionEnd"], "SessionEnd")

    def test_notification_preserved(self):
        self.assertEqual(gemini.EVENT_MAP["Notification"], "Notification")

    def test_precompact_maps_to_precompress(self):
        self.assertEqual(gemini.EVENT_MAP["PreCompact"], "PreCompress")

    def test_userpromptsubmit_maps_to_beforeagent(self):
        self.assertEqual(gemini.EVENT_MAP["UserPromptSubmit"], "BeforeAgent")


class TestTimeoutConversion(unittest.TestCase):
    """Timeout seconds -> milliseconds."""

    def test_seconds_to_milliseconds(self):
        group = {"hooks": [{"type": "command", "command": "echo hi", "timeout": 3}]}
        result = gemini._convert_hook_group(group)
        self.assertEqual(result["hooks"][0]["timeout"], 3000)

    def test_no_timeout_left_alone(self):
        group = {"hooks": [{"type": "command", "command": "echo hi"}]}
        result = gemini._convert_hook_group(group)
        self.assertNotIn("timeout", result["hooks"][0])


class TestPathRewrite(unittest.TestCase):
    """$HOME/.claude/hooks/ -> $HOME/.gemini/hooks/."""

    def test_rewrite_claude_to_gemini(self):
        cmd = "$HOME/.claude/hooks/no-emdash-guard.sh"
        self.assertEqual(
            gemini._rewrite_command(cmd),
            "$HOME/.gemini/hooks/no-emdash-guard.sh",
        )

    def test_non_hook_path_untouched(self):
        cmd = "/usr/local/bin/check.sh"
        self.assertEqual(gemini._rewrite_command(cmd), cmd)


class TestStatusMessageDropped(unittest.TestCase):
    """statusMessage is a Claude-only field and should be dropped."""

    def test_status_message_dropped(self):
        group = {
            "hooks": [
                {
                    "type": "command",
                    "command": "echo hi",
                    "timeout": 5,
                    "statusMessage": "Doing stuff",
                }
            ]
        }
        result = gemini._convert_hook_group(group)
        self.assertNotIn("statusMessage", result["hooks"][0])


class TestCanonicalPermissions(unittest.TestCase):
    """Gemini settings and policies use their native configuration surfaces."""

    def test_settings_and_permission_policy_are_generated(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)
            (src_p / "permissions.json").write_text(
                '{"schemaVersion":1,"allow":["Bash(git *)"],"deny":["Bash(git push --force *)"],"ask":[],"defaultMode":"bypassPermissions"}\n'
            )
            (tgt_p / "settings.json").write_text('{"native":true}\n')

            gemini.sync(src_p, tgt_p)

            settings = json.loads((tgt_p / "settings.json").read_text())
            self.assertEqual(settings["general"]["defaultApprovalMode"], "auto_edit")
            self.assertTrue(settings["native"])
            policy = (tgt_p / "policies" / "agentrc-permissions.toml").read_text()
            self.assertIn(
                'commandRegex = "^git\\\\ push\\\\ \\\\-\\\\-force\\\\ .*$', policy
            )


class TestUnmappableEventDropped(unittest.TestCase):
    """Events with no Gemini equivalent should be dropped with a note."""

    def test_subagent_events_dropped(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)
            hooks_dir = src_p / "hooks"
            hooks_dir.mkdir()
            hooks_json = {
                "SubagentStart": [
                    {"hooks": [{"type": "command", "command": "echo start"}]}
                ],
                "SubagentStop": [
                    {"hooks": [{"type": "command", "command": "echo stop"}]}
                ],
                "WorktreeCreate": [
                    {"hooks": [{"type": "command", "command": "echo wt"}]}
                ],
            }
            (hooks_dir / "hooks.json").write_text(json.dumps(hooks_json))
            actions = gemini.sync(src_p, tgt_p)
            drop_actions = [a for a in actions if "drop event" in a]
            self.assertEqual(len(drop_actions), 3)
            # settings.json hooks should be empty (all dropped)
            settings = json.loads((tgt_p / "settings.json").read_text())
            self.assertEqual(settings.get("hooks", {}), {})


class TestForeignHookGroupPreserved(unittest.TestCase):
    """Existing Gemini hook groups NOT referencing our hooks dir are kept."""

    def test_foreign_group_preserved(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)

            # Source has a PreToolUse hook
            hooks_dir = src_p / "hooks"
            hooks_dir.mkdir()
            hooks_json = {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "$HOME/.claude/hooks/guard.sh",
                                "timeout": 3,
                            }
                        ]
                    }
                ]
            }
            (hooks_dir / "hooks.json").write_text(json.dumps(hooks_json))

            # Target already has a foreign BeforeTool group
            foreign_group = {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/local/bin/my-custom-hook.sh",
                        "timeout": 5000,
                    }
                ]
            }
            existing_settings = {
                "ui": {"theme": "Default Light"},
                "hooks": {"BeforeTool": [foreign_group]},
            }
            tgt_p.mkdir(parents=True, exist_ok=True)
            (tgt_p / "settings.json").write_text(json.dumps(existing_settings))

            gemini.sync(src_p, tgt_p)

            settings = json.loads((tgt_p / "settings.json").read_text())
            bt_groups = settings["hooks"]["BeforeTool"]
            # Foreign group first, then source-managed group
            self.assertEqual(len(bt_groups), 2)
            # Foreign group preserved exactly
            self.assertEqual(
                bt_groups[0]["hooks"][0]["command"],
                "/usr/local/bin/my-custom-hook.sh",
            )
            # Source group has rewritten path
            self.assertEqual(
                bt_groups[1]["hooks"][0]["command"],
                "$HOME/.gemini/hooks/guard.sh",
            )

    def test_other_settings_keys_preserved(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)

            hooks_dir = src_p / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "hooks.json").write_text("{}")

            existing = {
                "ui": {"theme": "Default Light"},
                "security": {"auth": {"selectedType": "oauth-personal"}},
                "mcpServers": {"test": {}},
            }
            (tgt_p / "settings.json").write_text(json.dumps(existing))

            gemini.sync(src_p, tgt_p)

            settings = json.loads((tgt_p / "settings.json").read_text())
            self.assertEqual(settings["ui"]["theme"], "Default Light")
            self.assertEqual(
                settings["security"]["auth"]["selectedType"], "oauth-personal"
            )
            self.assertIn("mcpServers", settings)


class TestCommandMdToToml(unittest.TestCase):
    """commands/*.md -> commands/*.toml conversion."""

    def test_basic_conversion(self):
        md = textwrap.dedent("""\
            ---
            description: Create a git commit
            allowed-tools: Bash(git add:*)
            ---

            ## Your task

            Create a commit based on the changes.
        """)
        toml_text = gemini._md_to_toml("commit", md)
        self.assertIn('description = "Create a git commit"', toml_text)
        self.assertIn('prompt = """', toml_text)
        self.assertIn("## Your task", toml_text)
        self.assertIn("Create a commit", toml_text)
        # Ensure it ends with closing triple-quote
        self.assertTrue(toml_text.strip().endswith('"""'))

    def test_no_frontmatter_uses_name(self):
        md = "Just a prompt with no frontmatter."
        toml_text = gemini._md_to_toml("my-cmd", md)
        self.assertIn('description = "my-cmd"', toml_text)

    def test_triple_quotes_escaped(self):
        md = '---\ndescription: test\n---\nHere is some """text""" with quotes.'
        toml_text = gemini._md_to_toml("test", md)
        # The body should not contain raw triple quotes (they'd break TOML)
        body_start = toml_text.index('prompt = """') + len('prompt = """')
        body_end = toml_text.rindex('"""')
        body = toml_text[body_start:body_end]
        self.assertNotIn('"""', body)


class TestFullSync(unittest.TestCase):
    """End-to-end sync with all asset types."""

    def test_full_sync(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)

            # AGENTS.md
            (src_p / "AGENTS.md").write_text("# Global instructions\n")

            # hooks
            hooks_dir = src_p / "hooks"
            hooks_dir.mkdir()
            lib_dir = hooks_dir / "lib"
            lib_dir.mkdir()
            (hooks_dir / "guard.sh").write_text("#!/bin/sh\nexit 0\n")
            os.chmod(hooks_dir / "guard.sh", 0o755)
            (hooks_dir / "guard.test.sh").write_text("# test\n")
            (lib_dir / "util.sh").write_text("# util\n")
            hooks_json = {
                "PreToolUse": [
                    {
                        "matcher": "Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "$HOME/.claude/hooks/guard.sh",
                                "timeout": 3,
                            }
                        ],
                    }
                ],
                "SubagentStart": [
                    {"hooks": [{"type": "command", "command": "echo nope"}]}
                ],
            }
            (hooks_dir / "hooks.json").write_text(json.dumps(hooks_json))

            # skills
            skills_dir = src_p / "skills" / "my-skill"
            skills_dir.mkdir(parents=True)
            (skills_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill\n")

            # commands
            cmds_dir = src_p / "commands"
            cmds_dir.mkdir()
            (cmds_dir / "deploy.md").write_text(
                "---\ndescription: Deploy the app\n---\nRun deploy steps.\n"
            )

            # agents (delivered as ~/.gemini/agents/<name>.md since WI-32)
            agents_dir = src_p / "agents"
            agents_dir.mkdir()
            (agents_dir / "reviewer.md").write_text("---\nname: reviewer\n---\n")

            gemini.sync(src_p, tgt_p)

            # Verify GEMINI.md
            self.assertTrue((tgt_p / "GEMINI.md").exists())
            self.assertEqual(
                (tgt_p / "GEMINI.md").read_text(), "# Global instructions\n"
            )

            # Verify hooks copied (excluding .test.sh and hooks.json)
            self.assertTrue((tgt_p / "hooks" / "guard.sh").exists())
            self.assertTrue((tgt_p / "hooks" / "lib" / "util.sh").exists())
            self.assertFalse((tgt_p / "hooks" / "guard.test.sh").exists())
            self.assertFalse((tgt_p / "hooks" / "hooks.json").exists())
            # Verify exec bit preserved
            mode = os.stat(tgt_p / "hooks" / "guard.sh").st_mode
            self.assertTrue(mode & 0o100)

            # Verify settings.json
            settings = json.loads((tgt_p / "settings.json").read_text())
            self.assertIn("BeforeTool", settings["hooks"])
            bt = settings["hooks"]["BeforeTool"][0]
            self.assertEqual(bt["matcher"], "Write")
            self.assertEqual(
                bt["hooks"][0]["command"],
                "$HOME/.gemini/hooks/guard.sh",
            )
            self.assertEqual(bt["hooks"][0]["timeout"], 3000)
            # SubagentStart dropped
            self.assertNotIn("SubagentStart", settings["hooks"])

            # Verify skills
            self.assertTrue((tgt_p / "skills" / "my-skill" / "SKILL.md").exists())

            # Verify commands
            self.assertTrue((tgt_p / "commands" / "deploy.toml").exists())
            toml = (tgt_p / "commands" / "deploy.toml").read_text()
            self.assertIn('description = "Deploy the app"', toml)

            # Verify agents delivered in Gemini's own shape
            self.assertTrue((tgt_p / "agents" / "reviewer.md").exists())


def _frontmatter(text: str) -> tuple[dict, str]:
    """Parse the adapter's JSON-valued frontmatter lines (a YAML subset)."""
    head, _, body = text.partition("\n---\n")
    lines = head.split("\n")
    assert lines[0] == "---", lines[0]
    return {k: json.loads(v) for k, v in (line.split(": ", 1) for line in lines[1:])}, body


class TestAgentTranslation(unittest.TestCase):
    """WI-32 item 1: Gemini CLI 0.56.0 loads ~/.gemini/agents/*.md.

    Evidence, 2026-09-23: Storage.getUserAgentsDir() is
    ~/.gemini/agents (bundle chunk-LZUWGCRJ.js line 253133), and the loader's
    localAgentSchema is strict. Run through that loader, a byte copy of a
    source agent failed with "tools: Expected array, received string" and
    "Unrecognized key(s) in object: 'color'"; an agent with name, description,
    a tools array of Gemini tool names and a Gemini model loaded.
    """

    AGENT = textwrap.dedent("""\
        ---
        name: code-reviewer
        description: >-
          Reviews code: style, bugs and CLAUDE.md rules.
        tools: Read, Grep, Glob, Bash, Edit, Write, WebFetch, WebSearch, NotebookEdit
        model: opus
        color: green
        ---

        You review code. Follow CLAUDE.md.
        """)

    def _sync(self, agent_text: str, name: str = "code-reviewer") -> str:
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)
            (src_p / "agents").mkdir()
            (src_p / "agents" / f"{name}.md").write_text(agent_text)
            actions = gemini.sync(src_p, tgt_p)
            self.assertIn(f"agent {name}.md -> agents/{name}.md", actions)
            return (tgt_p / "agents" / f"{name}.md").read_text()

    def test_agent_is_written_in_the_shape_gemini_validates(self):
        meta, body = _frontmatter(self._sync(self.AGENT))
        self.assertEqual(set(meta), {"name", "description", "tools"})
        self.assertEqual(meta["name"], "code-reviewer")
        self.assertEqual(meta["description"], "Reviews code: style, bugs and GEMINI.md rules.")
        self.assertEqual(
            meta["tools"],
            [
                "read_file",
                "grep_search",
                "glob",
                "run_shell_command",
                "replace",
                "write_file",
                "web_fetch",
                "google_web_search",
            ],
        )
        self.assertIn("Follow GEMINI.md.", body)
        self.assertNotIn("CLAUDE.md", body)

    def test_a_gemini_model_is_kept_and_a_claude_alias_is_dropped(self):
        meta, _ = _frontmatter(self._sync(self.AGENT.replace("model: opus", "model: gemini-2.5-pro")))
        self.assertEqual(meta["model"], "gemini-2.5-pro")
        for alias in ("inherit", "sonnet", "claude-opus-4-8"):
            with self.subTest(alias=alias):
                meta, _ = _frontmatter(self._sync(self.AGENT.replace("model: opus", f"model: {alias}")))
                self.assertNotIn("model", meta)

    def test_an_agent_without_tools_or_description_still_loads(self):
        meta, _ = _frontmatter(self._sync("---\nname: reviewer\n---\nReview.\n", name="reviewer"))
        self.assertEqual(meta, {"name": "reviewer", "description": "reviewer"})


class TestDryRun(unittest.TestCase):
    """dry_run=True should not write anything."""

    def test_dry_run_no_writes(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)
            (src_p / "AGENTS.md").write_text("# test\n")
            hooks_dir = src_p / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "hooks.json").write_text("{}")

            actions = gemini.sync(src_p, tgt_p, dry_run=True)
            # Target should have no new files
            self.assertEqual(list(tgt_p.iterdir()), [])
            self.assertTrue(len(actions) > 0)


class TestIdempotent(unittest.TestCase):
    """Second sync on identical source returns empty action list."""

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            src_p, tgt_p = Path(src), Path(tgt)

            # Build source with all asset types
            (src_p / "AGENTS.md").write_text("# Global instructions\n")

            hooks_dir = src_p / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "guard.sh").write_text("#!/bin/sh\nexit 0\n")
            os.chmod(hooks_dir / "guard.sh", 0o755)
            lib_dir = hooks_dir / "lib"
            lib_dir.mkdir()
            (lib_dir / "util.sh").write_text("# util\n")
            hooks_json = {
                "PreToolUse": [
                    {
                        "matcher": "Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "$HOME/.claude/hooks/guard.sh",
                                "timeout": 3,
                            }
                        ],
                    }
                ]
            }
            (hooks_dir / "hooks.json").write_text(json.dumps(hooks_json))

            (src_p / "skills" / "my-skill").mkdir(parents=True)
            (src_p / "skills" / "my-skill" / "SKILL.md").write_text(
                "---\nname: my-skill\n---\n# Skill\n"
            )

            (src_p / "commands").mkdir()
            (src_p / "commands" / "deploy.md").write_text(
                "---\ndescription: Deploy the app\n---\nRun deploy steps.\n"
            )

            (src_p / "agents").mkdir()
            (src_p / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n")

            # First sync populates target
            first_actions = gemini.sync(src_p, tgt_p)
            self.assertTrue(len(first_actions) > 0)

            # Second sync on identical source must return empty list
            second_actions = gemini.sync(src_p, tgt_p)
            self.assertEqual(
                second_actions,
                [],
                f"Expected no actions on second sync, got: {second_actions}",
            )


class TestOwnedOutputs(unittest.TestCase):
    """F-16: owned_outputs names exactly what sync() itself would write."""

    def test_owned_directories_match_a_fixture_source(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source, target = Path(src), Path(tgt)
            (source / "hooks").mkdir()
            (source / "hooks" / "guard.sh").write_text("#!/bin/sh\n")
            (source / "hooks" / "guard.test.sh").write_text("#!/bin/sh\n")
            (source / "commands").mkdir()
            (source / "commands" / "deploy.md").write_text(
                "---\ndescription: Deploy\n---\nDeploy.\n"
            )
            (source / "skills" / "s1").mkdir(parents=True)
            (source / "skills" / "s1" / "SKILL.md").write_text("s\n")
            (source / "agents").mkdir()
            (source / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n")

            owned = {od.kind: od for od in gemini.owned_outputs(source, target)}

            self.assertEqual(owned["hook"].path, target / "hooks")
            self.assertEqual(owned["hook"].names, frozenset({"guard.sh"}))
            self.assertEqual(owned["command"].path, target / "commands")
            self.assertEqual(owned["command"].names, frozenset({"deploy.toml"}))
            self.assertTrue(owned["skill"].per_item)
            self.assertEqual(owned["skill"].names, frozenset({"s1"}))
            self.assertEqual(owned["agent"].path, target / "agents")
            self.assertEqual(owned["agent"].names, frozenset({"reviewer.md"}))

    def test_a_target_only_command_is_reported_as_an_orphan(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source, target = Path(src), Path(tgt)
            (source / "commands").mkdir()
            (source / "commands" / "deploy.md").write_text(
                "---\ndescription: Deploy\n---\nDeploy.\n"
            )
            gemini.sync(source, target)
            (target / "commands" / "stale.toml").write_text('description = "x"\n')
            owned = {od.kind: od for od in gemini.owned_outputs(source, target)}
            present = owned_dir_entries(owned["command"].path)
            self.assertEqual(present - owned["command"].names, {"stale.toml"})
