"""external_inventory() per adapter, against a fixture home.

Each adapter lists, from the locations in the record's table, every external
item its runtime holds; a location that exists and cannot be read is
unreadable, never empty; a location that does not exist yields nothing.
"""

from __future__ import annotations

import importlib
import json
import plistlib
import tempfile
import unittest
from pathlib import Path

import pytest

from agentrc.adapters import _inventory as inventory


@pytest.fixture(autouse=True)
def _isolated_home(agentrc_home):
    """Every test reads the home through AGENTRC_HOME, never the real one."""
    return agentrc_home



def adapter(name: str):
    return importlib.import_module(f"agentrc.adapters.{name}")


def names(items, kind):
    return sorted(i.name for i in items if i.kind == kind and not i.error)


def unreadable_kinds(items):
    return sorted(i.kind for i in items if i.error)


class FixtureHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def write(self, relative: str, content) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            content = json.dumps(content)
        path.write_text(content)
        return path


class TestEveryAdapterExposesTheInventory(unittest.TestCase):
    def test_each_registered_adapter_has_external_inventory(self):
        from agentrc.adapters._common import runtime_registry

        for name in runtime_registry():
            with self.subTest(runtime=name):
                self.assertTrue(callable(getattr(adapter(name), "external_inventory", None)))

    def test_an_empty_target_yields_only_the_unconfirmed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for name, relative in (
                ("claude", ".claude"),
                ("codex", ".codex"),
                ("gemini", ".gemini"),
                ("opencode", ".config/opencode"),
                ("cursor", ".cursor"),
            ):
                with self.subTest(runtime=name):
                    items = adapter(name).external_inventory(home / relative)
                    self.assertEqual([i for i in items if not i.error], [])


class TestClaude(FixtureHome):
    def test_plugins_marketplaces_mcp_servers_and_connectors(self):
        target = self.home / ".claude"
        self.write(
            ".claude/plugins/installed_plugins.json",
            {"version": 2, "plugins": {"figma@claude-plugins-official": [{"scope": "user"}]}},
        )
        self.write(".claude/plugins/known_marketplaces.json", {"claude-plugins-official": {"source": {}}})
        self.write(".claude/settings.json", {"enabledPlugins": {"extra@other-market": True, "off@other-market": False}})
        self.write(
            ".claude.json",
            {
                "mcpServers": {"railway": {"command": "npx", "env": {"MODE": "prod"}}},
                "projects": {"/work/app": {"mcpServers": {"local-db": {"command": "db"}}}},
            },
        )
        self.write("projects/active/app/.mcp.json", {"mcpServers": {"project-server": {"url": "https://x.test"}}})

        items = adapter("claude").external_inventory(target)

        self.assertEqual(names(items, "plugin"), ["extra@other-market", "figma@claude-plugins-official"])
        self.assertEqual(names(items, "marketplace"), ["claude-plugins-official"])
        self.assertEqual(names(items, "mcp_server"), ["local-db", "project-server", "railway"])
        railway = next(i for i in items if i.name == "railway")
        self.assertEqual(railway.env, (("mcpServers.railway.env.MODE", "prod"),))
        self.assertIn("connector", unreadable_kinds(items))

    def test_a_corrupt_registry_is_unreadable_never_empty(self):
        self.write(".claude/plugins/installed_plugins.json", "{not json")
        self.write(".claude.json", "[1, 2")
        items = adapter("claude").external_inventory(self.home / ".claude")
        self.assertIn("plugin", unreadable_kinds(items))
        self.assertIn("mcp_server", unreadable_kinds(items))


class TestCodex(FixtureHome):
    def test_mcp_servers_and_plugins_from_config_toml(self):
        self.write(
            ".codex/config.toml",
            'model = "x"\n\n[mcp_servers.graft]\ncommand = "graft"\n\n'
            '[mcp_servers.node_repl]\ncommand = "node"\n\n[mcp_servers.node_repl.env]\nNODE_ENV = "dev"\n\n'
            '[plugins."browser@openai-bundled"]\nenabled = true\n',
        )
        items = adapter("codex").external_inventory(self.home / ".codex")
        self.assertEqual(names(items, "mcp_server"), ["graft", "node_repl"])
        self.assertEqual(names(items, "plugin"), ["browser@openai-bundled"])
        node = next(i for i in items if i.name == "node_repl")
        self.assertEqual(node.env, (("mcp_servers.node_repl.env.NODE_ENV", "dev"),))

    def test_corrupt_toml_is_unreadable(self):
        self.write(".codex/config.toml", "[mcp_servers.x\n")
        items = adapter("codex").external_inventory(self.home / ".codex")
        self.assertEqual(unreadable_kinds(items), ["mcp_server"])

    def test_agents_skills_still_reported_beside_a_corrupt_config_toml(self):
        # A pre-fix version of external_inventory() returned early for every
        # config.toml problem branch, which would have silently dropped the
        # ~/.agents/skills scan whenever config.toml also happened to be
        # unreadable. The two are independent: one bad root must not hide
        # the other's items.
        self.write(".codex/config.toml", "[mcp_servers.x\n")
        self.write(".agents/skills/defuddle/SKILL.md", "x\n")
        items = adapter("codex").external_inventory(self.home / ".codex")
        self.assertEqual(unreadable_kinds(items), ["mcp_server"])
        self.assertEqual(names(items, "skill"), ["defuddle"])

    def test_agents_skills_root_reports_a_skill_per_directory(self):
        """Decision 3 of docs/adr/2026-09-23-deliver-and-measure-only-
        available-skills.md: ~/.agents/skills is a second root Codex lists
        skills from that no adapter writes, so every directory there is a
        foreign copy candidate, reported alongside config.toml's items."""
        self.write(".agents/skills/defuddle/SKILL.md", "x\n")
        self.write(".agents/skills/obsidian-bases/SKILL.md", "x\n")
        (self.home / ".agents" / "skills" / "not-a-dir.txt").write_text("x\n")
        items = adapter("codex").external_inventory(self.home / ".codex")
        self.assertEqual(names(items, "skill"), ["defuddle", "obsidian-bases"])
        defuddle = next(i for i in items if i.name == "defuddle")
        self.assertEqual(defuddle.location, str(self.home / ".agents" / "skills" / "defuddle"))

    def test_no_agents_skills_root_reports_no_skill_items(self):
        # Confirmed by test_an_empty_target_yields_only_the_unconfirmed_rows
        # too; this names the case directly for this decision.
        items = adapter("codex").external_inventory(self.home / ".codex")
        self.assertEqual(names(items, "skill"), [])


class TestGemini(FixtureHome):
    def test_mcp_servers_and_extensions(self):
        self.write(".gemini/settings.json", {"mcpServers": {"pal": {"command": "pal"}}, "hooks": {}})
        self.write(".gemini/extensions/conductor/gemini-extension.json", {"name": "conductor"})
        self.write(".gemini/extensions/extension-enablement.json", {})
        items = adapter("gemini").external_inventory(self.home / ".gemini")
        self.assertEqual(names(items, "mcp_server"), ["pal"])
        self.assertEqual(names(items, "extension"), ["conductor"])


class TestOpenCode(FixtureHome):
    def test_mcp_plugin_keys_and_plugin_files_other_than_ours(self):
        self.write(
            ".config/opencode/opencode.jsonc",
            '{\n  // comment\n  "mcp": {"docs": {"type": "remote", "headers": {"X-Mode": "a"}}},\n  "plugin": ["opencode-foo"],\n}\n',
        )
        self.write(".config/opencode/opencode.json", {"mcp": {"other": {"type": "local"}}})
        self.write(".config/opencode/plugins/agentrc-hooks.ts", "// ours\n")
        self.write(".config/opencode/plugins/status-panel.js", "// foreign\n")
        items = adapter("opencode").external_inventory(self.home / ".config" / "opencode")
        self.assertEqual(names(items, "mcp_server"), ["docs", "other"])
        self.assertEqual(names(items, "plugin"), ["opencode-foo", "status-panel.js"])
        docs = next(i for i in items if i.name == "docs")
        self.assertEqual(docs.env, (("mcp.docs.headers.X-Mode", "a"),))

    def test_block_comments_are_jsonc(self):
        """Codex review of PR 85: a /* */ comment is valid JSONC and must not
        make the file unreadable, while /* inside a string is kept."""
        self.write(
            ".config/opencode/opencode.jsonc",
            '/* global config */\n{\n  "mcp": {\n    /* a server */\n    "docs": {"url": "https://x.test/*/y"}\n  }\n}\n',
        )
        items = adapter("opencode").external_inventory(self.home / ".config" / "opencode")
        self.assertEqual(unreadable_kinds(items), [])
        self.assertEqual(names(items, "mcp_server"), ["docs"])
        self.assertEqual(
            inventory.strip_jsonc('{"a": "b /* c */ d" /* gone */}'),
            '{"a": "b /* c */ d" }',
        )
        # Second Codex review: a trailing comma is removed outside strings only.
        self.assertEqual(
            json.loads(inventory.strip_jsonc('{"a": "first, ] last", "b": [1, 2,],}')),
            {"a": "first, ] last", "b": [1, 2]},
        )


class TestCursor(FixtureHome):
    def test_mcp_servers_and_unconfirmed_plugins(self):
        self.write(".cursor/mcp.json", {"mcpServers": {"railway": {"command": "npx"}}})
        (self.home / ".cursor" / "plugins" / "cache").mkdir(parents=True)
        items = adapter("cursor").external_inventory(self.home / ".cursor")
        self.assertEqual(names(items, "mcp_server"), ["railway"])
        self.assertEqual(unreadable_kinds(items), ["plugin"])


class TestMachine(FixtureHome):
    def test_launch_agents_carry_label_program_and_environment(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        with (agents / "sh.brew.ollama.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "Label": "sh.brew.ollama",
                    "ProgramArguments": ["/opt/homebrew/bin/ollama", "serve"],
                    "EnvironmentVariables": {"OLLAMA_HOST": "127.0.0.1"},
                },
                handle,
            )
        (agents / "broken.plist").write_bytes(b"not a plist")
        # Codex review of PR 85: truncated XML raises ExpatError from plistlib.
        (agents / "truncated.plist").write_bytes(b'<?xml version="1.0"?>\n<plist version="1.0"><dict><key>Label</key>')
        # Second Codex review: EnvironmentVariables of the wrong shape.
        with (agents / "odd-env.plist").open("wb") as handle:
            plistlib.dump({"Label": "odd.env", "EnvironmentVariables": ["A=B"]}, handle)
        items = inventory.machine_inventory(self.home, {"dependencies": []})
        service = next(i for i in items if i.name == "sh.brew.ollama")
        self.assertIn("/opt/homebrew/bin/ollama", service.match_text)
        self.assertEqual(service.env, (("EnvironmentVariables.OLLAMA_HOST", "127.0.0.1"),))
        self.assertEqual(unreadable_kinds(items), ["service", "service", "service"])

    def test_declared_dependencies_report_presence(self):
        bindir = self.home / "bin"
        bindir.mkdir()
        tool = bindir / "present-tool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        document = {
            "dependencies": [
                {"name": "present-tool", "command": "present-tool"},
                {"name": "absent-tool", "command": "absent-tool"},
            ]
        }
        items = inventory.dependency_items(document, path=str(bindir))
        self.assertEqual({i.name: i.present for i in items}, {"present-tool": True, "absent-tool": False})

    def test_a_dependency_platform_that_does_not_match_the_host_is_skipped(self):
        # docs/bugs/gemini-runtime-oauth-does-not-persist-in-the-container.md:
        # dbus-daemon and gnome-keyring are container-only (Linux) additions
        # nobody expects on a Mac's PATH; a platform-scoped dependency is
        # left out of the inventory entirely on a host it does not name,
        # never reported missing.
        document = {
            "dependencies": [
                {"name": "linux-only", "command": "linux-only-tool", "platform": "linux"},
                {"name": "darwin-only", "command": "darwin-only-tool", "platform": "darwin"},
                {"name": "everywhere", "command": "present-tool"},
            ]
        }
        bindir = self.home / "bin"
        bindir.mkdir()
        (bindir / "present-tool").write_text("#!/bin/sh\n")
        (bindir / "present-tool").chmod(0o755)
        items = inventory.dependency_items(document, path=str(bindir), host_platform="darwin")
        self.assertEqual({i.name for i in items}, {"darwin-only", "everywhere"})
        items = inventory.dependency_items(document, path=str(bindir), host_platform="linux")
        self.assertEqual({i.name for i in items}, {"linux-only", "everywhere"})


class TestClassify(unittest.TestCase):
    def item(self, kind, name, runtime="claude", **kw):
        return inventory.Item(runtime, kind, name, "/loc", **kw)

    def test_declared_undeclared_returned_unreadable(self):
        document = {
            "mcp_servers": [
                {"name": "kept", "runtimes": ["claude"], "wanted": True, "owner": "agentrc"},
                {"name": "gone", "runtimes": ["claude"], "wanted": False, "owner": "some-installer"},
            ],
            "plugins": [
                {"name": "figma", "marketplace": "claude-plugins-official", "runtimes": ["claude"], "wanted": True}
            ],
        }
        self.assertEqual(inventory.classify(document, self.item("mcp_server", "kept"))[0], "declared")
        self.assertEqual(inventory.classify(document, self.item("mcp_server", "stray"))[0], "undeclared")
        state, entry = inventory.classify(document, self.item("mcp_server", "gone"))
        self.assertEqual((state, entry["owner"]), ("returned", "some-installer"))
        self.assertEqual(inventory.classify(document, self.item("mcp_server", "kept", runtime="codex"))[0], "undeclared")
        self.assertEqual(
            inventory.classify(document, self.item("plugin", "figma@claude-plugins-official"))[0], "declared"
        )
        self.assertEqual(inventory.classify(document, self.item("marketplace", "claude-plugins-official"))[0], "declared")
        self.assertEqual(inventory.classify(document, inventory.unreadable("claude", "connector", "/x", "why"))[0], "unreadable")

    def test_a_skill_is_declared_only_by_a_third_party_skills_entry(self):
        # Decision 3 of docs/adr/2026-09-23-deliver-and-measure-only-
        # available-skills.md: a foreign skill copy is undeclared unless
        # third_party_skills names it for this runtime, the same shape
        # owned_outputs already uses for a skill sync.py finds in place.
        document = {
            "third_party_skills": [
                {"name": "impeccable", "runtimes": ["codex"], "wanted": True, "owner": "impeccable", "installer": "its own installer"},
            ],
        }
        self.assertEqual(inventory.classify(document, self.item("skill", "impeccable", runtime="codex"))[0], "declared")
        self.assertEqual(inventory.classify(document, self.item("skill", "defuddle", runtime="codex"))[0], "undeclared")
        # Declared for codex only; the same name on another runtime is not covered.
        self.assertEqual(inventory.classify(document, self.item("skill", "impeccable", runtime="claude"))[0], "undeclared")

    def test_a_multi_directory_third_party_skills_entry_declares_each_of_its_skills(self):
        # skill_entry() already supports one installer's several directories
        # under one entry's "skills" list (reviewbot creates ten); classify(),
        # reached from external_inventory() for a foreign codex skill copy,
        # must match the same way, not only by the entry's own "name".
        document = {
            "third_party_skills": [
                {"name": "reviewbot", "runtimes": ["codex"], "wanted": True, "skills": ["reviewbot-1", "reviewbot-2"]},
            ],
        }
        self.assertEqual(inventory.classify(document, self.item("skill", "reviewbot-1", runtime="codex"))[0], "declared")
        self.assertEqual(inventory.classify(document, self.item("skill", "reviewbot-2", runtime="codex"))[0], "declared")
        self.assertEqual(inventory.classify(document, self.item("skill", "reviewbot", runtime="codex"))[0], "declared")
        self.assertEqual(inventory.classify(document, self.item("skill", "reviewbot-3", runtime="codex"))[0], "undeclared")

    def test_an_unwanted_service_is_matched_by_its_program(self):
        document = {
            "services": [
                {
                    "name": "ollama-app",
                    "runtimes": ["claude"],
                    "wanted": False,
                    "owner": "Ollama.app",
                    "label": "com.electron.ollama",
                    "match": "/Applications/Ollama.app",
                    "remove": "osascript -e 'tell application \"Ollama\" to quit'",
                }
            ]
        }
        item = self.item("service", "com.other.label", runtime="machine", match_text="com.other.label /Applications/Ollama.app/x")
        self.assertEqual(inventory.classify(document, item)[0], "returned")

    def test_an_unwanted_service_is_matched_by_a_resolved_program_path(self):
        document = {
            "services": [
                {
                    "name": "homebrew-ollama",
                    "runtimes": ["claude"],
                    "wanted": False,
                    "owner": "Homebrew",
                    "label": "sh.brew.ollama",
                    "match": ["/opt/homebrew/Cellar/ollama/", "/opt/homebrew/opt/ollama/bin/ollama"],
                    "remove": "brew services stop ollama",
                }
            ]
        }
        item = self.item(
            "service",
            "com.other.label",
            runtime="machine",
            match_text="com.other.label /opt/homebrew/Cellar/ollama/0.12.0/bin/ollama",
        )
        state, entry = inventory.classify(document, item)
        self.assertEqual((state, entry["name"] if entry else None), ("returned", "homebrew-ollama"))


class TestSkillEntry(unittest.TestCase):
    """One third-party skill entry may install several directories (a foreign tool such as reviewbot
    installs ten), so the entry names them and each one resolves to it."""

    document = {
        "third_party_skills": [
            {
                "name": "reviewbot",
                "runtimes": ["claude", "codex"],
                "owner": "reviewbot",
                "wanted": True,
                "installer": "reviewbot skills install",
                "skills": ["reviewbot-review", "reviewbot-fix"],
            },
            {
                "name": "impeccable",
                "runtimes": ["claude"],
                "owner": "impeccable",
                "wanted": False,
                "installer": "npx impeccable install",
            },
        ]
    }

    def test_a_named_directory_resolves_to_its_entry(self):
        for name in ("reviewbot-review", "reviewbot-fix"):
            with self.subTest(name=name):
                entry = inventory.skill_entry(self.document, "claude", name)
                self.assertEqual(entry["name"] if entry else None, "reviewbot")

    def test_the_entry_name_still_resolves(self):
        entry = inventory.skill_entry(self.document, "claude", "impeccable")
        self.assertEqual(entry["name"] if entry else None, "impeccable")

    def test_a_directory_no_entry_names_does_not_resolve(self):
        self.assertIsNone(inventory.skill_entry(self.document, "claude", "reviewbot-unknown"))

    def test_a_runtime_the_entry_does_not_name_does_not_resolve(self):
        self.assertIsNone(inventory.skill_entry(self.document, "gemini", "reviewbot-review"))
