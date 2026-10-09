"""Tests for the OpenCode runtime adapter.

All tests use temp dirs -- never writes to ~/.config/opencode.
"""

from __future__ import annotations

import json
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentrc.adapters._common import owned_dir_entries
from agentrc.adapters._text import strip_jsonc_comments
from agentrc.adapters.opencode import (
    _build_agent_frontmatter,
    _translate_command_frontmatter,
    _translate_tools_to_permission,
    owned_outputs,
    sync,
)


@pytest.fixture(autouse=True)
def _isolated_home(agentrc_home):
    """Every test reads the home through AGENTRC_HOME, never the real one."""
    return agentrc_home


# The OpenCode config schema, downloaded from https://opencode.ai/config.json
# on 2026-09-23 while OpenCode 1.4.2 was installed. WI-32 item 3: OpenCode
# 1.4.2 refused the deployed config outright ("Configuration is invalid at
# ~/.config/opencode/opencode.jsonc ↳ Unrecognized key: "permissions""), and
# this schema has additionalProperties false with a "permission" object.
SCHEMA_PATH = Path(__file__).resolve().parent / "fixtures" / "adapters" / "opencode-config-schema-2026-09-23.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _schema_errors(instance: object, ref: str = "#/$defs/Config") -> list[str]:
    """Validate against the fixture schema; jsonschema when present, else a structural check."""
    schema = _schema()
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - exercised only where jsonschema is absent
        config = schema["$defs"]["Config"]
        if ref.endswith("/Config"):
            return [f"unknown key {k}" for k in instance if k not in config["properties"]]
        return []
    target = dict(schema)
    target["$ref"] = ref
    # The model field points at an external models.dev schema; offline, a
    # string model is checked by shape in the tests themselves.
    agent = target["$defs"]["AgentConfig"]["properties"]
    agent["model"] = {"type": "string"}
    validator = jsonschema.Draft202012Validator(target)
    return [error.message for error in validator.iter_errors(instance)]


def _agent_frontmatter(text: str) -> dict:
    """Parse the adapter's JSON-valued agent frontmatter (a YAML subset)."""
    head = text.split("\n---\n", 1)[0].split("\n")
    assert head[0] == "---"
    return {k: json.loads(v) for k, v in (line.split(": ", 1) for line in head[1:])}


class TestToolsToPermission(unittest.TestCase):
    """Claude's tools allowlist becomes denies for the OpenCode tools left out.

    Allowing the listed tools at agent level would widen the global policy
    (agent rules take precedence over the global ones), so a listed tool gets
    no entry and inherits the global rule.
    """

    def test_unlisted_core_tools_are_denied(self):
        self.assertEqual(
            _translate_tools_to_permission("Read, Grep, Glob, Bash"),
            {"edit": "deny", "webfetch": "deny", "websearch": "deny"},
        )

    def test_list_input_and_write_counts_as_edit(self):
        self.assertEqual(
            _translate_tools_to_permission(["Read", "Write"]),
            {"grep": "deny", "glob": "deny", "bash": "deny", "webfetch": "deny", "websearch": "deny"},
        )

    def test_no_tools_means_no_restriction(self):
        self.assertEqual(_translate_tools_to_permission(None), {})


class TestAgentFrontmatter(unittest.TestCase):
    def test_basic_agent(self):
        fm = {
            "name": "code-reviewer",
            "description": "Reviews code changes: bugs and style",
            "tools": "Read, Grep, Glob, Bash",
            "model": "opus",
            "color": "green",
        }
        meta = _agent_frontmatter(_build_agent_frontmatter(fm) + "Body.\n")
        self.assertEqual(meta["mode"], "subagent")
        self.assertEqual(meta["description"], "Reviews code changes: bugs and style")
        # name is the filename; opus is a Claude alias, not provider/model;
        # green is neither a hex color nor an OpenCode theme color.
        self.assertEqual(set(meta), {"description", "mode", "permission"})
        self.assertEqual(meta["permission"], {"edit": "deny", "webfetch": "deny", "websearch": "deny"})
        self.assertEqual(_schema_errors(meta, "#/$defs/AgentConfig"), [])

    def test_provider_model_and_theme_color_are_kept(self):
        meta = _agent_frontmatter(
            _build_agent_frontmatter(
                {"description": "d", "model": "anthropic/claude-sonnet-4-20250514", "color": "accent"}
            )
            + "Body.\n"
        )
        self.assertEqual(meta["model"], "anthropic/claude-sonnet-4-20250514")
        self.assertEqual(meta["color"], "accent")
        self.assertEqual(_schema_errors(meta, "#/$defs/AgentConfig"), [])

    def test_mode_always_subagent(self):
        result = _build_agent_frontmatter({"description": "test"})
        self.assertIn('mode: "subagent"', result)


class TestCommandFrontmatter(unittest.TestCase):
    def test_keeps_description_and_model(self):
        fm = {
            "description": "Create a git commit",
            "allowed-tools": "Bash(git add:*), Bash(git commit:*)",
            "model": "sonnet",
        }
        result = _translate_command_frontmatter(fm)
        self.assertIn("description: Create a git commit", result)
        self.assertIn("model: sonnet", result)
        # allowed-tools has no OC equivalent, must be dropped
        self.assertNotIn("allowed-tools", result)

    def test_drops_argument_hint(self):
        fm = {
            "description": "Feature dev",
            "argument-hint": "Optional feature description",
        }
        result = _translate_command_frontmatter(fm)
        self.assertNotIn("argument-hint", result)


class TestStripJsoncComments(unittest.TestCase):
    def test_strip_comments_and_trailing_commas(self):
        text = textwrap.dedent("""\
        {
          // This is a comment
          "$schema": "https://opencode.ai/config.json",
        }
        """)
        cleaned = strip_jsonc_comments(text)
        import json

        data = json.loads(cleaned)
        self.assertEqual(data["$schema"], "https://opencode.ai/config.json")

    def test_a_string_value_shaped_like_a_trailing_comma_is_not_corrupted(self):
        # Issue #315: the trailing-comma pass used to run over the whole
        # text with no string awareness, so a string whose content happens
        # to end in ",}" had that comma deleted from the string itself.
        text = textwrap.dedent("""\
        {
          "note": "keep,}"
        }
        """)
        cleaned = strip_jsonc_comments(text)
        data = json.loads(cleaned)
        self.assertEqual(data["note"], "keep,}")

    def test_block_comments_are_stripped(self):
        # Issue #315: /* ... */ was never recognized at all, so a valid
        # JSONC file using one failed json.loads after the strip.
        text = textwrap.dedent("""\
        {
          /* leading block comment
             spanning multiple lines */
          "provider": {} /* trailing */
        }
        """)
        cleaned = strip_jsonc_comments(text)
        data = json.loads(cleaned)
        self.assertEqual(data["provider"], {})

    def test_a_slash_inside_a_string_is_not_mistaken_for_a_comment(self):
        text = textwrap.dedent("""\
        {
          "url": "https://example.com/a/*not-a-comment*/b"
        }
        """)
        cleaned = strip_jsonc_comments(text)
        data = json.loads(cleaned)
        self.assertEqual(data["url"], "https://example.com/a/*not-a-comment*/b")


class TestSync(unittest.TestCase):
    def setUp(self):
        self._src_td = TemporaryDirectory()
        self._tgt_td = TemporaryDirectory()
        self.source = Path(self._src_td.name)
        self.target = Path(self._tgt_td.name)

    def tearDown(self):
        self._src_td.cleanup()
        self._tgt_td.cleanup()

    def _write(self, rel_path: str, content: str) -> Path:
        p = self.source / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_permissions_policy_merge_preserves_unmanaged_config(self):
        self._write(
            "permissions.json",
            '{"schemaVersion":1,"allow":["Bash(git *)"],"deny":["Bash(git push --force *)"],"ask":[],"defaultMode":"bypassPermissions"}\n',
        )
        (self.target / "opencode.jsonc").write_text(
            '{"$schema":"https://opencode.ai/config.json","share":"disabled"}\n'
        )

        sync(self.source, self.target)

        config = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertEqual(config["share"], "disabled")
        self.assertNotIn("permissions", config)
        self.assertEqual(config["permission"]["*"], "allow")
        self.assertEqual(
            config["permission"]["bash"], {"git *": "allow", "git push --force *": "deny"}
        )

    def test_rendered_config_is_valid_against_the_opencode_schema(self):
        """WI-32 item 3, the defect: a stale "permissions" array is removed, not kept."""
        self._write(
            "permissions.json",
            json.dumps(
                {
                    "schemaVersion": 1,
                    "defaultMode": "default",
                    "additionalDirectories": ["~/projects"],
                    "runtimeDirectories": ["~/.config/opencode"],
                    "allow": ["Read(~/projects/**)", "Edit(~/projects/**)", "Bash(git *)", "Bash(rm *)"],
                    "deny": ["Bash(rm -r*)", "Read(**/.env)", "Edit(//etc/**)"],
                    "ask": ["Bash(git push *)"],
                }
            ),
        )
        (self.target / "opencode.jsonc").write_text(
            '{\n  // legacy V2 rules the adapter wrote before WI-32\n'
            '  "permissions": [{"action": "*", "resource": "*", "effect": "ask"}],\n'
            '  "$schema": "https://opencode.ai/config.json"\n}\n'
        )

        sync(self.source, self.target)

        config = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertEqual(_schema_errors(config), [])
        self.assertNotIn("permissions", config)
        permission = config["permission"]
        # "*" is the catch-all; OpenCode applies the last matching rule, so
        # within a tool allow comes first, ask next and deny last.
        self.assertEqual(list(permission)[0], "*")
        self.assertEqual(permission["*"], "ask")
        self.assertEqual(
            list(permission["bash"].items()),
            [("git *", "allow"), ("rm *", "allow"), ("git push *", "ask"), ("rm -r*", "deny")],
        )
        self.assertEqual(permission["read"], {"~/projects/**": "allow", "**/.env": "deny"})
        self.assertEqual(permission["edit"], {"~/projects/**": "allow", "/etc/**": "deny"})
        self.assertEqual(
            permission["external_directory"],
            {"~/projects/**": "allow", "~/.config/opencode/**": "allow"},
        )

    def test_a_reordered_permission_object_is_rewritten(self):
        """Codex review of PR 64: OpenCode applies the last matching rule, so
        the same entries in another order are a different policy, and dict
        equality alone would leave them in place."""
        self._write(
            "permissions.json",
            json.dumps(
                {
                    "schemaVersion": 1,
                    "defaultMode": "default",
                    "allow": ["Read(~/projects/**)"],
                    "deny": ["Read(~/projects/secret/**)"],
                    "ask": [],
                }
            ),
        )
        sync(self.source, self.target)
        config = json.loads((self.target / "opencode.jsonc").read_text())
        rendered = list(config["permission"]["read"].items())
        self.assertEqual(rendered[-1], ("~/projects/secret/**", "deny"))

        # The deny moved before the broad allow: the deny no longer wins.
        config["permission"]["read"] = dict(reversed(rendered))
        (self.target / "opencode.jsonc").write_text(json.dumps(config))

        actions = sync(self.source, self.target)

        self.assertIn("render permissions.json into opencode.jsonc", actions)
        config = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertEqual(list(config["permission"]["read"].items()), rendered)

    def test_runtime_settings_opencode_provider_is_rendered_into_opencode_jsonc(self):
        # docs/adr/2026-09-25-route-cheap-work-to-free-llm-tiers.md: a custom
        # OpenAI-compatible provider entry, the same passthrough mechanism
        # claude.py uses for runtime_settings.claude.settings (C-28).
        self._write(
            "components.json",
            json.dumps(
                {
                    "version": 1,
                    "runtime_settings": {
                        "opencode": {
                            "provider": {
                                "gateway": {
                                    "npm": "@ai-sdk/openai-compatible",
                                    "options": {
                                        "baseURL": "http://127.0.0.1:3001/v1",
                                        "apiKey": "{env:GATEWAY_KEY}",
                                    },
                                    "models": {"auto:vetted": {"name": "gateway auto:vetted"}},
                                }
                            }
                        }
                    },
                }
            ),
        )

        actions = sync(self.source, self.target)

        self.assertTrue(any("provider" in action for action in actions), actions)
        config = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertEqual(
            config["provider"]["gateway"]["options"]["apiKey"], "{env:GATEWAY_KEY}",
        )
        self.assertEqual(config["provider"]["gateway"]["npm"], "@ai-sdk/openai-compatible")

    def test_provider_passthrough_preserves_unrelated_config_and_other_providers(self):
        self._write(
            "components.json",
            json.dumps(
                {"version": 1, "runtime_settings": {"opencode": {"provider": {"gateway": {"npm": "@ai-sdk/openai-compatible"}}}}}
            ),
        )
        (self.target / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "share": "disabled",
                    "provider": {"other": {"npm": "@ai-sdk/some-other"}},
                }
            )
        )

        sync(self.source, self.target)

        config = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertEqual(config["share"], "disabled")
        self.assertEqual(config["provider"]["other"], {"npm": "@ai-sdk/some-other"})
        self.assertEqual(config["provider"]["gateway"], {"npm": "@ai-sdk/openai-compatible"})

    def test_no_provider_declared_leaves_opencode_jsonc_untouched(self):
        (self.target / "opencode.jsonc").write_text('{"$schema": "https://opencode.ai/config.json"}\n')

        actions = sync(self.source, self.target)

        self.assertFalse(any("provider" in action for action in actions), actions)
        config = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertNotIn("provider", config)

    def test_the_example_policy_renders_a_valid_config(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "notes-cli" / "source"
        (self.source / "permissions.json").write_text((example / "permissions.json").read_text())
        sync(self.source, self.target)
        config = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertEqual(_schema_errors(config), [])
        self.assertEqual(config["permission"]["bash"]["sudo *"], "deny")

    def test_agents_md_byte_copy(self):
        self._write("AGENTS.md", "# AGENTS\nGlobal instructions here.")
        actions = sync(self.source, self.target)
        self.assertIn("copy AGENTS.md", actions)
        dst = self.target / "AGENTS.md"
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_text(), "# AGENTS\nGlobal instructions here.")

    def test_command_translation(self):
        self._write(
            "commands/commit.md",
            textwrap.dedent("""\
            ---
            allowed-tools: Bash(git add:*), Bash(git commit:*)
            description: Create a git commit
            ---

            Stage and commit changes.
            """),
        )
        actions = sync(self.source, self.target)
        self.assertTrue(any("translate commands/commit.md" in a for a in actions))
        dst = self.target / "commands" / "commit.md"
        self.assertTrue(dst.exists())
        content = dst.read_text()
        # description kept
        self.assertIn("description: Create a git commit", content)
        # allowed-tools dropped
        self.assertNotIn("allowed-tools", content)
        # body preserved
        self.assertIn("Stage and commit changes.", content)

    def test_agent_translation_tools_to_permissions(self):
        self._write(
            "agents/code-reviewer.md",
            textwrap.dedent("""\
            ---
            name: code-reviewer
            description: Reviews code for adherence to guidelines
            tools: Read, Grep, Glob, Bash
            model: opus
            color: green
            ---

            You are an expert code reviewer.

            Check CLAUDE.md for project guidelines.
            """),
        )
        actions = sync(self.source, self.target)
        self.assertTrue(any("translate agents/code-reviewer.md" in a for a in actions))
        dst = self.target / "agents" / "code-reviewer.md"
        self.assertTrue(dst.exists())
        content = dst.read_text()

        meta = _agent_frontmatter(content)
        # mode: subagent added, description kept, name dropped (filename is
        # the id), the Claude alias and the Claude color dropped.
        self.assertEqual(
            meta,
            {
                "description": "Reviews code for adherence to guidelines",
                "mode": "subagent",
                "permission": {"edit": "deny", "webfetch": "deny", "websearch": "deny"},
            },
        )
        self.assertEqual(_schema_errors(meta, "#/$defs/AgentConfig"), [])
        # CLAUDE.md -> AGENTS.md in body
        self.assertIn("AGENTS.md", content)
        self.assertNotIn("CLAUDE.md", content)
        # body preserved
        self.assertIn("You are an expert code reviewer.", content)

    def test_hooks_and_plugin_are_synced(self):
        self._write("hooks/guard.sh", "#!/usr/bin/env bash\nexit 0\n")
        self._write(
            "hooks/opencode-runtime-hooks.ts", "export const Hook = async () => ({})\n"
        )
        actions = sync(self.source, self.target)
        self.assertTrue(any("copy hooks/guard.sh" in action for action in actions))
        self.assertIn("install plugins/agentrc-hooks.ts", actions)
        self.assertTrue((self.target / "hooks" / "guard.sh").exists())
        self.assertTrue((self.target / "plugins" / "agentrc-hooks.ts").exists())

    def test_skills_mirrored(self):
        self._write(
            "skills/my-skill/SKILL.md",
            "---\nname: my-skill\ndescription: A test skill\n---\n\nDo things.\n",
        )
        sync(self.source, self.target)
        # Should mirror to target/skill/ (singular, per OC docs)
        dst = self.target / "skill" / "my-skill" / "SKILL.md"
        self.assertTrue(dst.exists())
        self.assertIn("name: my-skill", dst.read_text())

    def test_rules_global_copied(self):
        self._write("rules/decisions.md", "# Decisions\n\nMake them.\n")
        self._write("rules/naming-conventions.md", "# naming-conventions\n\nShould remain scoped.\n")
        self._write(
            "rules/tiers.json",
            json.dumps({"global": ["decisions.md"], "common": ["naming-conventions.md"], "project": []}),
        )
        actions = sync(self.source, self.target)
        # Only global rules copied
        self.assertTrue(any("rules/decisions.md" in a for a in actions))
        dst = self.target / "rules" / "decisions.md"
        self.assertTrue(dst.exists())
        # a name in the common tier of rules/tiers.json should NOT be copied
        self.assertFalse((self.target / "rules" / "naming-conventions.md").exists())
        # AGENTS.md carries the generated digest, so no manual configuration
        # step should be reported for a copied rule.
        self.assertFalse(any("manual:" in action for action in actions))

    def test_rules_without_a_tier_manifest_exclude_only_readme(self):
        """The common tier comes from rules/tiers.json alone; with no manifest
        there is no common tier, so every rule but README.md is copied."""
        self._write("rules/README.md", "# rules\n")
        self._write("rules/decisions.md", "# Decisions\n\nMake them.\n")
        self._write("rules/naming-conventions.md", "# naming-conventions\n\nShould remain scoped.\n")
        sync(self.source, self.target)
        self.assertTrue((self.target / "rules" / "decisions.md").exists())
        self.assertTrue((self.target / "rules" / "naming-conventions.md").exists())
        self.assertFalse((self.target / "rules" / "README.md").exists())

    def test_dry_run_no_writes(self):
        self._write("AGENTS.md", "# test")
        self._write("commands/test.md", "---\ndescription: test\n---\n\nTest.\n")
        actions = sync(self.source, self.target, dry_run=True)
        self.assertTrue(len(actions) > 0)
        # Target should be empty (nothing written)
        target_files = list(self.target.rglob("*"))
        self.assertEqual(len(target_files), 0)

    def test_target_only_agent_is_reported_as_an_orphan(self):
        """F-16 inverts the old "survives forever" guarantee.

        docs/adr/2026-09-21-runtime-output-ownership-and-prune.md: a
        target-only agent is an orphan, pruned by sync.py --prune, not
        preserved indefinitely. sync() itself still never deletes a
        target-only file on its own (deletion is --prune's job, driven by
        owned_outputs), so that half of the old guarantee still holds;
        what inverts is that the file is no longer content anyone owns.
        """
        target_only = self.target / "agents" / "custom-agent.md"
        target_only.parent.mkdir(parents=True, exist_ok=True)
        target_only.write_text("# Custom agent\n")

        self._write("AGENTS.md", "# test")
        sync(self.source, self.target)

        # sync() alone still does not touch a target-only file.
        self.assertTrue(target_only.exists())
        self.assertEqual(target_only.read_text(), "# Custom agent\n")

        # But the reverse pass no longer counts it a survivor: it is an
        # orphan, which sync.py --prune removes.
        owned = {od.kind: od for od in owned_outputs(self.source, self.target)}
        agents_dir = owned["agent"]
        present = owned_dir_entries(agents_dir.path)
        orphans = present - agents_dir.names - agents_dir.exclude
        self.assertIn("custom-agent.md", orphans)

    def test_owned_directories_match_a_fixture_source(self):
        self._write("commands/deploy.md", "---\ndescription: Deploy\n---\nDeploy.\n")
        self._write(
            "agents/reviewer.md", "---\ndescription: Reviews\n---\nReview it.\n"
        )
        self._write("hooks/guard.sh", "#!/usr/bin/env bash\nexit 0\n")
        self._write("hooks/guard.test.sh", "#!/usr/bin/env bash\nexit 0\n")
        self._write("rules/decisions.md", "# Decisions\n\nMake them.\n")
        self._write("skills/s1/SKILL.md", "---\nname: s1\n---\nBody.\n")

        owned = {od.kind: od for od in owned_outputs(self.source, self.target)}

        self.assertEqual(owned["command"].names, frozenset({"deploy.md"}))
        self.assertEqual(owned["agent"].names, frozenset({"reviewer.md"}))
        self.assertEqual(owned["hook"].names, frozenset({"guard.sh"}))
        self.assertEqual(owned["rule"].names, frozenset({"decisions.md"}))
        self.assertEqual(owned["skill"].path, self.target / "skill")
        self.assertTrue(owned["skill"].per_item)
        self.assertEqual(owned["skill"].names, frozenset({"s1"}))


class TestAgentToolsMapValues(unittest.TestCase):
    """Verify the specific tool name translations documented in the adapter."""

    def test_bash_is_the_bash_key(self):
        self.assertNotIn("bash", _translate_tools_to_permission("Bash"))
        self.assertEqual(_translate_tools_to_permission("Read")["bash"], "deny")

    def test_edit_alone_leaves_write_allowed(self):
        self.assertNotIn("edit", _translate_tools_to_permission("Edit"))

    def test_unknown_tool_restricts_nothing_extra(self):
        self.assertEqual(
            _translate_tools_to_permission("CustomTool"),
            {k: "deny" for k in ("read", "edit", "grep", "glob", "bash", "webfetch", "websearch")},
        )


class TestIdempotent(unittest.TestCase):
    """Second sync on identical source returns empty action list."""

    def test_idempotent(self):
        with TemporaryDirectory() as src, TemporaryDirectory() as tgt:
            source = Path(src)
            target = Path(tgt)

            # Build source with all asset types
            source_files = {
                "AGENTS.md": "# Global instructions\n",
                "commands/commit.md": "---\ndescription: Create a commit\n---\n\nStage and commit.\n",
                "agents/code-reviewer.md": "---\nname: code-reviewer\ndescription: Reviews code\ntools: Read, Grep\nmodel: opus\ncolor: green\n---\n\nReview code against CLAUDE.md.\n",
                "skills/my-skill/SKILL.md": "---\nname: my-skill\ndescription: A test skill\n---\n\nDo things.\n",
                "rules/decisions.md": "# Decisions\n\nMake them.\n",
            }
            for rel_path, content in source_files.items():
                p = source / rel_path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)

            # First sync populates target
            first_actions = sync(source, target)
            self.assertTrue(len(first_actions) > 0)

            # Second sync on identical source must return empty list
            second_actions = sync(source, target)
            self.assertEqual(
                second_actions,
                [],
                f"Expected no actions on second sync, got: {second_actions}",
            )


class TestCanonicalOpenCodeBridge(unittest.TestCase):
    def test_post_write_reconciles_control_plane(self):
        bridge = (
            Path(__file__).resolve().parents[1] / "agentrc" / "data" / "hooks" / "opencode-runtime-hooks.ts"
        )
        source = bridge.read_text()
        self.assertIn(
            'invoke("reconcile-control-plane.sh", payload("PostToolUse"', source
        )
        self.assertIn('"tool.execute.after": async (input:', source)
        self.assertIn("args: Args", source)
        self.assertIn("editedInput(input.tool, input.args)", source)

    def test_session_created_runs_the_session_start_hook(self):
        """WI-32 item 5: OpenCode 1.4.2 emits session.created to plugins.

        The string is in the installed binary (`strings` on
        opencode-darwin-arm64/bin/opencode, 2026-09-23) and in the plugin
        documentation. A child session (a task subagent) carries a parentID
        and is not a new session of its own, so it is skipped: since G-02 its
        branch sends the subagent cap's start signal and returns before the
        SessionStart call.
        """
        bridge = (
            Path(__file__).resolve().parents[1] / "agentrc" / "data" / "hooks" / "opencode-runtime-hooks.ts"
        )
        source = bridge.read_text()
        created = source.index('if (event.type === "session.created") {')
        child = source.index("if (info?.parentID) {", created)
        child_return = source.index("return", child)
        start = source.index('invokeIfPresent("session-start.sh", sessionStartPayload(', created)
        self.assertLess(child, child_return)
        self.assertLess(child_return, start)
        self.assertIn('source: "startup"', source)
