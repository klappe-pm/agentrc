"""Tests for the Codex runtime adapter.

Uses temp dirs only -- never writes to ~/.codex.
"""

import json
import os
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

import pytest


from agentrc.adapters import codex
from agentrc.adapters._common import home, owned_dir_entries
from agentrc.adapters._text import set_top_level


@pytest.fixture(autouse=True)
def _isolated_home(agentrc_home):
    """Every test reads the home through AGENTRC_HOME, never the real one."""
    return agentrc_home



class TestHooksJsonMerge(unittest.TestCase):
    """hooks.json: wrapper key, path rewrite, merge preserves foreign groups."""

    def test_memory_settings_are_disabled_without_erasing_other_tables(self):
        original = (
            '[features]\nmemories = true\nother = true\n'
            '[memories]\nuse_memories = true\ngenerate_memories = true\n'
            '[desktop]\nexternal-agent-import-sync-enabled = true\nother = "keep"\n'
        )
        updated = original
        for table, key in (
            ("features", "memories"),
            ("memories", "use_memories"),
            ("memories", "generate_memories"),
            ("desktop", "external-agent-import-sync-enabled"),
        ):
            updated = codex._set_table_scalar(updated, table, key, "false")
        parsed = tomllib.loads(updated)
        self.assertFalse(parsed["features"]["memories"])
        self.assertFalse(parsed["memories"]["use_memories"])
        self.assertFalse(parsed["memories"]["generate_memories"])
        self.assertFalse(parsed["desktop"]["external-agent-import-sync-enabled"])
        self.assertTrue(parsed["features"]["other"])
        self.assertEqual(parsed["desktop"]["other"], "keep")
        self.assertEqual(codex._set_table_scalar(updated, "features", "memories", "false"), updated)

    def test_hooks_json_wrapper_and_rewrite(self):
        """Source hooks.json is rewritten and wrapped in {"hooks": {...}}."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            # Create source structure
            (source / "hooks").mkdir()
            (source / "hooks" / "no-emdash-guard.sh").write_text("#!/bin/sh\nexit 0\n")
            os.chmod(source / "hooks" / "no-emdash-guard.sh", 0o755)

            # Source hooks.json (Claude format -- no wrapper key)
            src_hooks = {
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
            (source / "hooks" / "hooks.json").write_text(json.dumps(src_hooks))

            codex.sync(source, target)

            # Verify hooks.json was written
            result_path = target / "hooks.json"
            self.assertTrue(result_path.exists(), "hooks.json not created")

            result = json.loads(result_path.read_text())

            # Must have top-level "hooks" wrapper
            self.assertIn("hooks", result, "Missing top-level 'hooks' wrapper key")

            # Path rewrite: .claude -> .codex
            inner = result["hooks"]
            self.assertIn("PreToolUse", inner)
            cmd = inner["PreToolUse"][0]["hooks"][0]["command"]
            self.assertIn(".codex/hooks/", cmd, f"Path not rewritten: {cmd}")
            self.assertNotIn(".claude/hooks/", cmd, f"Old path still present: {cmd}")

    def test_merge_preserves_foreign_hook_group(self):
        """Foreign hook groups (orca, graft) survive merge."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            # Source has one hook script and hooks.json
            (source / "hooks").mkdir()
            (source / "hooks" / "guard.sh").write_text("#!/bin/sh\nexit 0\n")

            src_hooks = {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "$HOME/.claude/hooks/guard.sh",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
            (source / "hooks" / "hooks.json").write_text(json.dumps(src_hooks))

            # Pre-existing target hooks.json with a foreign group (orca)
            home_dir = str(home())
            existing = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"if [ -x '{home_dir}/.orca/agent-hooks/codex-hook.sh' ]; then /bin/sh '{home_dir}/.orca/agent-hooks/codex-hook.sh'; fi",
                                    "timeout": 10,
                                }
                            ]
                        }
                    ],
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo foreign-session-start",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ],
                }
            }
            (target / "hooks.json").write_text(json.dumps(existing))

            codex.sync(source, target)

            result = json.loads((target / "hooks.json").read_text())
            inner = result["hooks"]

            # PreToolUse should have our group + the foreign orca group
            pre_groups = inner["PreToolUse"]
            commands = []
            for g in pre_groups:
                for h in g.get("hooks", []):
                    commands.append(h.get("command", ""))

            # Our rewritten hook should be present
            has_ours = any(".codex/hooks/guard.sh" in c for c in commands)
            self.assertTrue(has_ours, f"Our hook not found in: {commands}")

            # Foreign orca hook should survive
            has_foreign = any("orca" in c for c in commands)
            self.assertTrue(has_foreign, f"Foreign orca hook was deleted: {commands}")

            # SessionStart foreign group should survive (not in source)
            self.assertIn("SessionStart", inner)
            ss_cmds = [
                h.get("command", "")
                for g in inner["SessionStart"]
                for h in g.get("hooks", [])
            ]
            self.assertTrue(
                any("foreign-session-start" in c for c in ss_cmds),
                "Foreign SessionStart hook was deleted",
            )


class TestCodexHookEvents(unittest.TestCase):
    """G-02: hooks.json registers PostToolUseFailure, which Codex 0.156.1
    does not have, beside SubagentStart and SubagentStop, which it does. The
    adapter drops the one Codex has no event for and passes the rest."""

    def test_post_tool_use_failure_is_dropped_and_subagent_events_kept(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, target = Path(src), Path(dst)
            (source / "hooks").mkdir()
            (source / "hooks" / "tool-budget-guard.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
            group = [{"hooks": [{"type": "command", "command": "$HOME/.claude/hooks/tool-budget-guard.sh"}]}]
            (source / "hooks" / "hooks.json").write_text(
                json.dumps({event: group for event in ("PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop")})
            )
            codex.sync(source, target)
            result = json.loads((target / "hooks.json").read_text())["hooks"]
            self.assertNotIn("PostToolUseFailure", result)
            for event in ("PostToolUse", "SubagentStart", "SubagentStop"):
                self.assertIn(event, result)
            # The drop is deliberate, so a second sync finds Codex current.
            self.assertEqual(codex.sync(source, target, dry_run=True), [])
            self.assertIn("PostToolUseFailure", codex.RUNTIME["hook_events"])


class TestForeignHookGroupRepointAndTimeout(unittest.TestCase):
    """F-17: mislocated repointed, dangling and live foreign survive as
    they are (sync() never deletes a registration), and the one named
    graft-hooks.cjs timeout is normalised to seconds.
    """

    def setUp(self):
        self.src_tmp = tempfile.TemporaryDirectory()
        self.dst_tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.src_tmp.name)
        self.target = Path(self.dst_tmp.name)
        (self.source / "hooks").mkdir()
        (self.source / "hooks" / "worktree-create.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n"
        )
        (self.source / "hooks" / "hooks.json").write_text(
            json.dumps(
                {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "$HOME/.claude/hooks/worktree-create.sh",
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

    def test_mislocated_repointed_dangling_and_live_foreign_survive(self):
        home_dir = str(home())
        mislocated_cmd = f"{home_dir}/.claude/scripts/worktree-create.sh"
        dangling_cmd = f"{home_dir}/.codex/hooks/regen-surfaces.sh"
        live_cmd = (
            f"if [ -x '{home_dir}/.orca/agent-hooks/codex-hook.sh' ]; "
            f"then '{home_dir}/.orca/agent-hooks/codex-hook.sh'; fi"
        )
        existing = {
            "hooks": {
                "WorktreeCreate": [
                    {"hooks": [{"type": "command", "command": mislocated_cmd}]}
                ],
                "PostToolUse": [
                    {
                        "hooks": [
                            {"type": "command", "command": dangling_cmd},
                            {
                                "type": "command",
                                "command": live_cmd,
                                "timeout": 10,
                            },
                        ]
                    }
                ],
            }
        }
        (self.target / "hooks.json").write_text(json.dumps(existing))

        codex.sync(self.source, self.target)

        result = json.loads((self.target / "hooks.json").read_text())["hooks"]
        wt_commands = [h["command"] for g in result["WorktreeCreate"] for h in g["hooks"]]
        self.assertIn("$HOME/.codex/hooks/worktree-create.sh", wt_commands)
        self.assertNotIn(mislocated_cmd, wt_commands)

        post_hooks = [h for g in result["PostToolUse"] for h in g["hooks"]]
        post_commands = [h["command"] for h in post_hooks]
        self.assertIn(dangling_cmd, post_commands)
        self.assertIn(live_cmd, post_commands)
        live_hook = next(h for h in post_hooks if h["command"] == live_cmd)
        self.assertEqual(live_hook["timeout"], 10)

    def test_graft_hooks_timeout_is_normalised_to_seconds(self):
        graft_cmd = f'node "{home()}/.codex/hooks/graft/graft-hooks.cjs" post-edit-sync'
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": graft_cmd,
                                "timeout": 10000,
                            }
                        ]
                    }
                ]
            }
        }
        (self.target / "hooks.json").write_text(json.dumps(existing))

        codex.sync(self.source, self.target)

        result = json.loads((self.target / "hooks.json").read_text())["hooks"]
        hooks = [h for g in result["PostToolUse"] for h in g["hooks"]]
        graft_hook = next(h for h in hooks if h["command"] == graft_cmd)
        self.assertEqual(graft_hook["timeout"], 10)

    def test_only_the_named_timeout_value_is_converted(self):
        """A foreign group's other timeouts, and a differing value on the
        same command, are never touched."""
        home_dir = str(home())
        graft_cmd = f'node "{home_dir}/.codex/hooks/graft/graft-hooks.cjs" post-edit-sync'
        other_cmd = f"'{home_dir}/.codex/hooks/run-project-tests.sh'"
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": graft_cmd,
                                "timeout": 9999,
                            },
                            {
                                "type": "command",
                                "command": other_cmd,
                                "timeout": 65,
                            },
                        ]
                    }
                ]
            }
        }
        (self.target / "hooks.json").write_text(json.dumps(existing))

        codex.sync(self.source, self.target)

        result = json.loads((self.target / "hooks.json").read_text())["hooks"]
        hooks = [h for g in result["PostToolUse"] for h in g["hooks"]]
        graft_hook = next(h for h in hooks if h["command"] == graft_cmd)
        other_hook = next(h for h in hooks if h["command"] == other_cmd)
        self.assertEqual(graft_hook["timeout"], 9999)
        self.assertEqual(other_hook["timeout"], 65)


class TestAgentTranslation(unittest.TestCase):
    """agents/*.md -> agents/*.toml: name, description, body, CLAUDE.md swap."""

    def test_agent_md_to_toml_roundtrip(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            (source / "agents").mkdir()
            agent_md = textwrap.dedent("""\
                ---
                name: test-agent
                description: A test agent for review
                tools: Read, Bash
                model: opus
                color: blue
                ---

                # test-agent

                Review code against CLAUDE.md guidelines.

                ## steps

                1. Read the diff
                2. Check CLAUDE.md rules
            """)
            (source / "agents" / "test-agent.md").write_text(agent_md)

            codex.sync(source, target)

            toml_path = target / "agents" / "test-agent.toml"
            self.assertTrue(toml_path.exists(), "TOML agent file not created")

            content = toml_path.read_text()

            # Check key fields
            self.assertIn('name = "test-agent"', content)
            self.assertIn("description = ", content)
            self.assertIn("developer_instructions", content)

            # CLAUDE.md -> AGENTS.md swap
            self.assertIn("AGENTS.md", content)
            self.assertNotIn("CLAUDE.md", content)

            # Claude/OpenCode-only source metadata is not valid Codex role TOML.
            self.assertNotIn('model = "opus"', content)
            self.assertNotIn('color = "blue"', content)
            self.assertNotIn('tools = "Read, Bash"', content)
            tomllib.loads(content)

    def test_a_codex_model_and_reasoning_effort_reach_the_role_file(self):
        """WI-32 item 2: Codex 0.156.1 reads model and model_reasoning_effort.

        Live check 2026-09-23: a role file carrying both was listed by
        `codex exec` with its model, while one carrying `color` was ignored
        with "unknown field `color`". A Claude alias such as `opus` is not a
        Codex model ("The 'opus' model is not supported when using Codex with
        a ChatGPT account"), so it is still omitted and the role inherits.
        """
        fm = {
            "name": "fast-reviewer",
            "description": "Reviews quickly",
            "model": "gpt-5.1-codex-mini",
            "model_reasoning_effort": "low",
            "color": "green",
            "tools": "Read, Bash",
        }
        parsed = tomllib.loads(codex._agent_md_to_toml(fm, "Review it.\n"))
        self.assertEqual(parsed["model"], "gpt-5.1-codex-mini")
        self.assertEqual(parsed["model_reasoning_effort"], "low")
        self.assertEqual(
            set(parsed), {"name", "description", "developer_instructions", "model", "model_reasoning_effort"}
        )

    def test_claude_model_names_are_not_written_to_codex(self):
        for alias in ("opus", "sonnet", "haiku", "inherit", "claude-opus-4-8"):
            with self.subTest(alias=alias):
                parsed = tomllib.loads(
                    codex._agent_md_to_toml({"name": "a", "description": "d", "model": alias}, "b")
                )
                self.assertNotIn("model", parsed)


class TestHistoryRetention(unittest.TestCase):
    """docs/adr/2026-09-25-keep-every-session-log.md: Codex has no automatic
    time or count based pruning of ~/.codex/sessions rollout files in 0.156.1
    (verified against codex doctor, codex --help and the binary's embedded
    default config). [history].persistence is the one retention-adjacent
    key, already defaulting to save-all; the adapter pins it explicitly so a
    hand-set persistence = "none" is corrected back."""

    def _policy(self) -> dict:
        return {"schemaVersion": 1, "allow": [], "deny": [], "ask": []}

    def test_history_persistence_is_pinned_to_save_all(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            target = Path(root) / "target"
            source.mkdir()
            (source / "permissions.json").write_text(json.dumps(self._policy()))
            codex.sync(source, target)
            config = tomllib.loads((target / "config.toml").read_text())
            self.assertEqual(config["history"]["persistence"], "save-all")

    def test_history_persistence_none_is_corrected(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            target = Path(root) / "target"
            source.mkdir()
            target.mkdir()
            (source / "permissions.json").write_text(json.dumps(self._policy()))
            (target / "config.toml").write_text('[history]\npersistence = "none"\n')
            codex.sync(source, target)
            config = tomllib.loads((target / "config.toml").read_text())
            self.assertEqual(config["history"]["persistence"], "save-all")

    def test_history_table_gains_persistence_without_touching_other_keys(self):
        updated = codex._set_table_scalar('[history]\nmax_bytes = 999\n', "history", "persistence", '"save-all"')
        parsed = tomllib.loads(updated)
        self.assertEqual(parsed["history"]["persistence"], "save-all")
        self.assertEqual(parsed["history"]["max_bytes"], 999)


class TestCanonicalPermissions(unittest.TestCase):
    """Canonical permissions render native Codex settings and rules."""

    def test_explicit_approval_policy_reaches_global_and_project_configs(self):
        _rewrite_codex_head = pytest.importorskip("agentrc.project_permissions")._rewrite_codex_head

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            target = Path(root) / "target"
            source.mkdir()
            policy = {
                "schemaVersion": 1,
                "defaultMode": "bypassPermissions",
                "codexApprovalPolicy": "on-request",
                "allow": [],
                "deny": ["Read(~/Library/Keychains/**)"],
                "ask": [],
            }
            (source / "permissions.json").write_text(json.dumps(policy))
            codex.sync(source, target)
            config = tomllib.loads((target / "config.toml").read_text())
            self.assertEqual(config["approval_policy"], "on-request")
            self.assertEqual(config["approvals_reviewer"], "auto_review")
            self.assertEqual(config["default_permissions"], "agentrc")
            self.assertEqual(
                config["permissions"]["agentrc"]["filesystem"]["~/Library/Keychains"],
                "deny",
            )
            project = _rewrite_codex_head('approval_policy = "never"\n', policy)
            self.assertEqual(tomllib.loads(project)["approval_policy"], "on-request")
            self.assertEqual(codex.sync(source, target), [])

    def test_explicit_approval_policy_is_validated(self):
        from agentrc.permissions import load

        with tempfile.TemporaryDirectory() as root:
            source = Path(root)
            policy = {"schemaVersion": 1, "allow": [], "deny": [], "ask": []}
            for value in (None, True, {}, "always", "on-failure"):
                with self.subTest(value=value):
                    policy["codexApprovalPolicy"] = value
                    (source / "permissions.json").write_text(json.dumps(policy))
                    with self.assertRaises(ValueError):
                        load(source)

    def test_explicit_never_and_legacy_approval_defaults_are_preserved(self):
        from agentrc.permissions import codex_approval_policy

        self.assertEqual(
            codex_approval_policy({"codexApprovalPolicy": "never"}), "never"
        )
        self.assertEqual(
            codex_approval_policy({"defaultMode": "bypassPermissions"}), "never"
        )
        self.assertEqual(codex_approval_policy({}), "on-request")

    def test_network_policy_enables_access_and_filtering(self):
        configurations = (
            "",
            "[features]\nnetwork_proxy = false\nother = true\n",
            '[features.network_proxy]\nenabled = false\n[features.network_proxy.domains]\n"existing.example" = "deny"\n',
        )
        for existing in configurations:
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as root:
                source = Path(root) / "source"
                target = Path(root) / "target"
                source.mkdir()
                target.mkdir()
                policy = {
                    "schemaVersion": 1,
                    "allow": [],
                    "deny": ["Read(~/.ssh/**)"],
                    "ask": [],
                    "codexNetwork": {
                        "enabled": True,
                        "domains": {"**.github.com": "allow"},
                    },
                }
                (source / "permissions.json").write_text(json.dumps(policy))
                (target / "config.toml").write_text(existing)
                codex.sync(source, target)
                config = tomllib.loads((target / "config.toml").read_text())
                self.assertEqual(
                    config["permissions"]["agentrc"]["network"], policy["codexNetwork"]
                )
                proxy = config["features"]["network_proxy"]
                self.assertTrue(proxy["enabled"] if isinstance(proxy, dict) else proxy)
                if isinstance(proxy, dict):
                    self.assertEqual(proxy["domains"]["existing.example"], "deny")
                self.assertEqual(
                    config["permissions"]["agentrc"]["filesystem"]["~/.ssh"], "deny"
                )
                self.assertEqual(codex.sync(source, target), [])

    def test_network_policy_is_optional_and_validated(self):
        from agentrc.permissions import load

        with tempfile.TemporaryDirectory() as root:
            source = Path(root)
            policy = {"schemaVersion": 1, "allow": [], "deny": [], "ask": []}
            for value in (
                None,
                {"enabled": "true"},
                {"enabled": True, "domains": []},
                {"domains": {"github.com": "maybe"}},
            ):
                with self.subTest(value=value):
                    policy["codexNetwork"] = value
                    (source / "permissions.json").write_text(json.dumps(policy))
                    with self.assertRaises(ValueError):
                        load(source)
            policy.pop("codexNetwork")
            self.assertNotIn(
                "network",
                tomllib.loads(codex._codex_permissions_block(policy))["permissions"][
                    "agentrc"
                ],
            )

    def test_shell_environment_passthrough_is_additive_and_survives_set_table(self):
        configurations = (
            "",
            '[shell_environment_policy]\ninherit = "core"\n\n'
            '[shell_environment_policy.set]\nEXISTING_VAR = "1"\n',
            '[shell_environment_policy]\ninherit = "core"\ninclude_only = ["ALREADY_THERE"]\n',
            '[shell_environment_policy]\ninherit = "all"\n\ninclude = ["GH_TOKEN"]\n',
        )
        for existing in configurations:
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as root:
                source = Path(root) / "source"
                target = Path(root) / "target"
                source.mkdir()
                target.mkdir()
                policy = {
                    "schemaVersion": 1,
                    "allow": [],
                    "deny": [],
                    "ask": [],
                    "codexShellEnvironment": {"passthrough": ["GH_TOKEN"]},
                }
                (source / "permissions.json").write_text(json.dumps(policy))
                (target / "config.toml").write_text(existing)
                codex.sync(source, target)
                config = tomllib.loads((target / "config.toml").read_text())
                shell_env = config["shell_environment_policy"]
                # include_only replaces whatever inherit would have supplied,
                # so reaching GH_TOKEN also requires "all" and every name the
                # runtime's own "core" mode carries (PATH, HOME, and so on).
                self.assertEqual(shell_env["inherit"], "all")
                self.assertIn("GH_TOKEN", shell_env["include_only"])
                self.assertIn("PATH", shell_env["include_only"])
                self.assertIn("HOME", shell_env["include_only"])
                if "EXISTING_VAR" in existing:
                    self.assertEqual(shell_env["set"]["EXISTING_VAR"], "1")
                    self.assertIn("EXISTING_VAR", shell_env["include_only"])
                if "ALREADY_THERE" in existing:
                    self.assertIn("ALREADY_THERE", shell_env["include_only"])
                # A bare `include` key is not a real Codex passthrough
                # mechanism; a stale one from an earlier version of this
                # adapter must not linger once include_only is rendered.
                self.assertNotIn("include", shell_env)
                self.assertEqual(codex.sync(source, target), [])

    # WI-103: Codex and hand edits write include_only as a multi-line array.
    # Rewriting only its first line orphaned the continuation lines and broke
    # config.toml with "key with no value, expected `=`".
    MULTILINE_POLICY = (
        "[shell_environment_policy]\n"
        'inherit = "all"\n'
        "include_only = [\n"
        '    "ALL_PROXY",\n'
        '    "CONTINUATION_ONLY", # a comment with ] and "quotes"\n'
        "    'LITERAL_NAME',\n"
        '    "BRACKET]NAME",\n'
        "]\n"
        'exclude = ["X"]\n'
        "\n"
        "[tui]\n"
        'notification_condition = "always"\n'
    )

    def test_set_top_level_replaces_a_multiline_array_whole(self):
        body = self.MULTILINE_POLICY.split("\n", 1)[1].split("\n[tui]")[0]
        updated = set_top_level(body, "include_only", json.dumps(["ONE"]))
        self.assertEqual(updated, 'inherit = "all"\ninclude_only = ["ONE"]\nexclude = ["X"]\n')
        tomllib.loads(updated)
        removed = set_top_level(body, "include_only", None)
        self.assertEqual(removed, 'inherit = "all"\nexclude = ["X"]\n')

    def test_list_top_level_reads_a_multiline_array(self):
        body = self.MULTILINE_POLICY.split("\n", 1)[1].split("\n[tui]")[0]
        self.assertEqual(
            codex._list_top_level(body, "include_only"),
            ["ALL_PROXY", "CONTINUATION_ONLY", "LITERAL_NAME", "BRACKET]NAME"],
        )

    def test_multiline_include_only_round_trips_through_sync(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            target = Path(root) / "target"
            source.mkdir()
            target.mkdir()
            policy = {
                "schemaVersion": 1,
                "allow": [],
                "deny": [],
                "ask": [],
                "codexShellEnvironment": {"passthrough": ["GH_TOKEN"]},
            }
            (source / "permissions.json").write_text(json.dumps(policy))
            (target / "config.toml").write_text(self.MULTILINE_POLICY)
            codex.sync(source, target)
            text = (target / "config.toml").read_text()
            config = tomllib.loads(text)
            shell_env = config["shell_environment_policy"]
            for name in ("GH_TOKEN", "PATH", "CONTINUATION_ONLY", "LITERAL_NAME", "BRACKET]NAME"):
                self.assertIn(name, shell_env["include_only"])
            self.assertEqual(shell_env["exclude"], ["X"])
            self.assertEqual(config["tui"]["notification_condition"], "always")
            self.assertEqual(text.count("include_only"), 1)
            self.assertEqual(codex.sync(source, target), [])

    def test_shell_environment_passthrough_is_optional_and_validated(self):
        from agentrc.permissions import load

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            target = Path(root) / "target"
            source.mkdir()
            target.mkdir()
            policy = {"schemaVersion": 1, "allow": [], "deny": [], "ask": []}
            for value in (
                {"passthrough": "GH_TOKEN"},
                {"passthrough": ["gh_token"]},
                {"passthrough": ["GH TOKEN"]},
                "not-an-object",
            ):
                with self.subTest(value=value):
                    policy["codexShellEnvironment"] = value
                    (source / "permissions.json").write_text(json.dumps(policy))
                    with self.assertRaises(ValueError):
                        load(source)
            policy.pop("codexShellEnvironment")
            existing = (
                '[shell_environment_policy]\ninherit = "core"\n\n'
                '[shell_environment_policy.set]\nEXISTING_VAR = "1"\n'
            )
            (source / "permissions.json").write_text(json.dumps(policy))
            (target / "config.toml").write_text(existing)
            codex.sync(source, target)
            shell_env = tomllib.loads((target / "config.toml").read_text())[
                "shell_environment_policy"
            ]
            self.assertEqual(shell_env, {"inherit": "core", "set": {"EXISTING_VAR": "1"}})

    def test_canonical_config_replaces_and_removes_legacy_fields(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)
            tree = source / "tree"
            (tree / "repo" / ".git").mkdir(parents=True)
            (source / "permissions.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "allow": ["Bash(git *)"],
                        "deny": [
                            "Bash(git push --force *)",
                            "Read(**/.env)",
                            "Edit(//etc/**)",
                            "Read(~/.ssh/**)",
                        ],
                        "ask": [],
                        "defaultMode": "bypassPermissions",
                        "additionalDirectories": [str(tree)],
                    }
                )
                + "\n"
            )
            (target / "config.toml").write_text(
                'approval_policy = "on-request"\n[features]\nweb_search_request = true\n'
            )

            codex.sync(source, target)

            config = tomllib.loads((target / "config.toml").read_text())
            self.assertEqual(config["approval_policy"], "never")
            self.assertEqual(config["web_search"], "live")
            self.assertNotIn("web_search_request", config.get("features", {}))
            self.assertEqual(config["default_permissions"], "agentrc")
            self.assertNotIn("sandbox_mode", config)
            self.assertIn("permissions", config)
            filesystem = config["permissions"]["agentrc"]["filesystem"]
            self.assertEqual(filesystem[":workspace_roots"]["**/.env"], "deny")
            self.assertEqual(filesystem["~/.ssh"], "deny")
            self.assertNotIn(
                "/etc", filesystem, "an Edit-only rule must not deny reads"
            )
            self.assertEqual(
                config["projects"][str(tree / "repo")]["trust_level"], "trusted"
            )
            rules = (target / "rules" / "agentrc.rules").read_text()
            self.assertIn('pattern=["git", "push", "--force"]', rules)
            self.assertIn('decision="forbidden"', rules)

    def test_profile_table_keys_survive_canonical_rewrite(self):
        """Keys inside [profiles.*] tables are runtime-owned and must not be rewritten."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)
            (source / "permissions.json").write_text(
                '{"schemaVersion":1,"allow":[],"deny":[],"ask":[],"defaultMode":"bypassPermissions"}\n'
            )
            (target / "config.toml").write_text(
                'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n\n'
                '[profiles.careful]\napproval_policy = "untrusted"\nsandbox_mode = "read-only"\n'
            )

            codex.sync(source, target)

            config = tomllib.loads((target / "config.toml").read_text())
            self.assertEqual(config["approval_policy"], "never")
            self.assertNotIn("sandbox_mode", config)
            self.assertEqual(
                config["profiles"]["careful"]["approval_policy"], "untrusted"
            )
            self.assertEqual(config["profiles"]["careful"]["sandbox_mode"], "read-only")

    def test_trust_entries_are_appended_once_and_respect_existing(self):
        """Every repository under the additional directories gets a trust table, existing tables are untouched, and a rerun is a no-op."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)
            tree = source / "tree"
            (tree / "repo-a" / ".git").mkdir(parents=True)
            (tree / "repo-b" / ".git").mkdir(parents=True)
            (tree / "repo-b" / "nested" / ".git").mkdir(parents=True)
            (tree / "node_modules" / "dep" / ".git").mkdir(parents=True)
            (tree / "plain").mkdir()
            (source / "permissions.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "allow": [],
                        "deny": [],
                        "ask": [],
                        "defaultMode": "bypassPermissions",
                        "additionalDirectories": [str(tree)],
                    }
                )
                + "\n"
            )
            (target / "config.toml").write_text(
                f'model = "x"\n\n[projects.{json.dumps(str(tree / "repo-b"))}]\ntrust_level = "untrusted"\n'
            )

            first = codex.sync(source, target)
            text = (target / "config.toml").read_text()
            config = tomllib.loads(text)

            self.assertIn("trust 2 repositories in config.toml", first)
            self.assertEqual(
                config["projects"][str(tree / "repo-a")]["trust_level"], "trusted"
            )
            self.assertEqual(
                config["projects"][str(tree / "repo-b" / "nested")]["trust_level"],
                "trusted",
            )
            self.assertEqual(
                config["projects"][str(tree / "repo-b")]["trust_level"], "untrusted"
            )
            self.assertNotIn(str(tree / "node_modules"), text)
            self.assertNotIn(str(tree / "plain"), text)
            self.assertEqual(text.count("[projects."), 3)
            self.assertTrue(text.rstrip().endswith("# END agentrc permissions"))

            second = codex.sync(source, target)
            self.assertEqual(second, [])
            self.assertEqual((target / "config.toml").read_text(), text)

    def test_unmarked_permission_tables_are_replaced_not_duplicated(self):
        """A managed block the runtime rewrote without its markers is stripped, not duplicated.

        Codex sorts tables and drops comments when it re-serializes config.toml.
        Appending beside the demarkered copy produces two [permissions.agentrc]
        tables, which is invalid TOML and blocks every Codex turn.
        """
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)
            (source / "permissions.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "allow": [],
                        "deny": ["Read(~/.ssh/**)"],
                        "ask": [],
                        "defaultMode": "bypassPermissions",
                    }
                )
                + "\n"
            )
            # Markers gone, tables sorted into the body: what Codex leaves behind.
            (target / "config.toml").write_text(
                'model = "x"\n\n'
                '[permissions.agentrc]\nextends = ":workspace"\n\n'
                '[permissions.agentrc.filesystem]\n"~/.ssh" = "deny"\n\n'
                '[tui]\nnotification_condition = "always"\n'
            )

            codex.sync(source, target)

            text = (target / "config.toml").read_text()
            config = tomllib.loads(text)
            self.assertEqual(text.count("[permissions.agentrc]"), 1)
            self.assertEqual(
                config["permissions"]["agentrc"]["filesystem"]["~/.ssh"], "deny"
            )
            self.assertEqual(config["tui"]["notification_condition"], "always")
            self.assertEqual(config["model"], "x")


class TestSkillSurvival(unittest.TestCase):
    """Target-only skills must survive sync."""

    def test_target_only_skill_survives(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            # Source has one skill
            (source / "skills" / "shared-skill").mkdir(parents=True)
            (source / "skills" / "shared-skill" / "SKILL.md").write_text(
                "---\nname: shared-skill\n---\n# Shared\n"
            )

            # Target already has its own skill
            (target / "skills" / "codex-native-skill").mkdir(parents=True)
            (target / "skills" / "codex-native-skill" / "SKILL.md").write_text(
                "---\nname: codex-native-skill\n---\n# Native\n"
            )

            codex.sync(source, target)

            # Source skill was copied
            self.assertTrue((target / "skills" / "shared-skill" / "SKILL.md").exists())
            # Target-only skill survived
            self.assertTrue(
                (target / "skills" / "codex-native-skill" / "SKILL.md").exists()
            )


class TestCommandsToPrompts(unittest.TestCase):
    """commands/*.md -> prompts/*.md."""

    def test_commands_copied_to_prompts(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            (source / "commands").mkdir()
            cmd_content = textwrap.dedent("""\
                ---
                description: Test command
                argument-hint: <arg>
                ---

                # /test

                Do something.
            """)
            (source / "commands" / "test.md").write_text(cmd_content)

            codex.sync(source, target)

            dst = target / "prompts" / "test.md"
            self.assertTrue(dst.exists())
            self.assertEqual(dst.read_text(), cmd_content)


class TestAgentsMdCopy(unittest.TestCase):
    """AGENTS.md byte copy."""

    def test_agents_md_copied(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            content = "# AGENTS.md\n\nGlobal instructions.\n"
            (source / "AGENTS.md").write_text(content)

            codex.sync(source, target)

            self.assertEqual((target / "AGENTS.md").read_text(), content)


class TestDryRun(unittest.TestCase):
    """dry_run=True produces actions but writes nothing."""

    def test_dry_run_no_writes(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            (source / "AGENTS.md").write_text("# test\n")

            actions = codex.sync(source, target, dry_run=True)

            self.assertTrue(len(actions) > 0)
            self.assertFalse((target / "AGENTS.md").exists())


class TestHookScriptsCopied(unittest.TestCase):
    """Hook scripts are copied, test files are excluded."""

    def test_hook_scripts_exclude_tests(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            (source / "hooks").mkdir()
            (source / "hooks" / "guard.sh").write_text("#!/bin/sh\n")
            (source / "hooks" / "guard.test.sh").write_text("#!/bin/sh\n")
            (source / "hooks" / "lib").mkdir()
            (source / "hooks" / "lib" / "helper.sh").write_text("#!/bin/sh\n")

            # Minimal hooks.json to avoid errors
            (source / "hooks" / "hooks.json").write_text("{}")

            codex.sync(source, target)

            self.assertTrue((target / "hooks" / "guard.sh").exists())
            self.assertFalse((target / "hooks" / "guard.test.sh").exists())
            self.assertTrue((target / "hooks" / "lib" / "helper.sh").exists())


class TestIdempotent(unittest.TestCase):
    """Second sync on identical source returns empty action list."""

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            # Build a source tree with all asset types
            (source / "AGENTS.md").write_text("# Global instructions\n")

            (source / "hooks").mkdir()
            (source / "hooks" / "guard.sh").write_text("#!/bin/sh\nexit 0\n")
            os.chmod(source / "hooks" / "guard.sh", 0o755)
            (source / "hooks" / "lib").mkdir()
            (source / "hooks" / "lib" / "util.sh").write_text("# util\n")
            hooks_data = {
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
            (source / "hooks" / "hooks.json").write_text(json.dumps(hooks_data))

            (source / "skills" / "my-skill").mkdir(parents=True)
            (source / "skills" / "my-skill" / "SKILL.md").write_text(
                "---\nname: my-skill\n---\n# Skill\n"
            )

            (source / "agents").mkdir()
            (source / "agents" / "reviewer.md").write_text(
                "---\nname: reviewer\ndescription: Reviews code\n---\n\nReview it.\n"
            )

            (source / "commands").mkdir()
            (source / "commands" / "deploy.md").write_text(
                "---\ndescription: Deploy\n---\nDeploy steps.\n"
            )

            # First sync populates target
            first_actions = codex.sync(source, target)
            self.assertTrue(len(first_actions) > 0)

            # Second sync on identical source must return empty list
            second_actions = codex.sync(source, target)
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
            (source / "agents").mkdir()
            (source / "agents" / "reviewer.md").write_text(
                "---\nname: reviewer\n---\nBody.\n"
            )
            (source / "commands").mkdir()
            (source / "commands" / "deploy.md").write_text(
                "---\ndescription: Deploy\n---\nDeploy.\n"
            )
            (source / "skills" / "s1").mkdir(parents=True)
            (source / "skills" / "s1" / "SKILL.md").write_text("s\n")

            owned = {od.kind: od for od in codex.owned_outputs(source, target)}

            self.assertEqual(owned["hook"].path, target / "hooks")
            self.assertEqual(owned["hook"].names, frozenset({"guard.sh"}))
            self.assertEqual(owned["agent"].path, target / "agents")
            self.assertEqual(owned["agent"].names, frozenset({"reviewer.toml"}))
            self.assertEqual(owned["prompt"].path, target / "prompts")
            self.assertEqual(owned["prompt"].names, frozenset({"deploy.md"}))
            self.assertTrue(owned["skill"].per_item)
            self.assertEqual(owned["skill"].names, frozenset({"s1"}))

    def test_orphan_agent_and_hook_are_reported(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as tgt:
            source, target = Path(src), Path(tgt)
            (source / "hooks").mkdir()
            (source / "hooks" / "guard.sh").write_text("#!/bin/sh\n")
            (source / "agents").mkdir()
            codex.sync(source, target)
            # A .toml agent whose .md source is gone, and an unshipped
            # hook script, both from the harness era.
            (target / "agents").mkdir(parents=True, exist_ok=True)
            (target / "agents" / "retired-agent.toml").write_text('name = "x"\n')
            (target / "hooks" / "plan-read-guard.sh").write_text("#!/bin/sh\n")

            owned = {od.kind: od for od in codex.owned_outputs(source, target)}
            agent_orphans = owned_dir_entries(owned["agent"].path) - owned[
                "agent"
            ].names
            hook_orphans = owned_dir_entries(owned["hook"].path) - owned["hook"].names
            self.assertEqual(agent_orphans, {"retired-agent.toml"})
            self.assertIn("plan-read-guard.sh", hook_orphans)
