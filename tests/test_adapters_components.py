"""Each adapter renders the MCP servers the global column selects from components.json into its runtime's own configuration.

One declared MCP server renders into each runtime's configuration shape; a hand-added server is preserved and reported undeclared; what a runtime cannot take is named, never silently dropped. The stage carries a components.json holding only what the column selected, so the adapters never read the control plane themselves.
"""

from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11 and later
except ImportError:  # the older framework python3 on macOS
    tomllib = None

from stratarc.adapters import _components as components_render
from stratarc.adapters._inventory import strip_jsonc


@pytest.fixture(autouse=True)
def _isolated_home(stratarc_home):
    """Every test reads the home through STRATARC_HOME, never the real one."""
    return stratarc_home



def adapter(name: str):
    return importlib.import_module(f"stratarc.adapters.{name}")


def server(name: str, **extra) -> dict:
    entry = {
        "name": name,
        "runtimes": ["claude", "codex", "gemini", "opencode", "cursor"],
        "owner": "stratarc",
        "wanted": True,
        "command": "npx",
        "args": ["-y", f"@example/{name}"],
    }
    entry.update(extra)
    return entry


class Rendering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.home = self.root / "home"

    def select(self, *entries: dict) -> None:
        (self.stage / "components.json").write_text(
            json.dumps({"version": 1, "mcp_servers": list(entries), "plugins": []}) + "\n"
        )

    # ----- Codex: [mcp_servers.<name>] tables in config.toml -----

    def codex_config(self) -> dict:
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        return tomllib.loads((self.home / ".codex" / "config.toml").read_text())

    def test_codex_renders_a_selected_server_and_keeps_a_hand_added_one(self):
        target = self.home / ".codex"
        target.mkdir(parents=True)
        (target / "config.toml").write_text('model = "gpt"\n\n[mcp_servers.handmade]\ncommand = "mine"\n')
        self.select(server("railway", env={"MODE": "fast"}))
        actions = adapter("codex").sync(self.stage, target)
        self.assertTrue(any("mcp" in a for a in actions), actions)
        config = self.codex_config()
        self.assertEqual(
            config["mcp_servers"]["railway"],
            {"command": "npx", "args": ["-y", "@example/railway"], "env": {"MODE": "fast"}},
        )
        self.assertEqual(config["mcp_servers"]["handmade"], {"command": "mine"})
        self.assertEqual(config["model"], "gpt")
        self.assertEqual(adapter("codex").sync(self.stage, target), [], "a second sync is current")

    def test_codex_removes_a_server_it_rendered_once_it_is_deselected(self):
        target = self.home / ".codex"
        self.select(server("railway"), server("linear"))
        adapter("codex").sync(self.stage, target)
        self.select(server("railway"))
        adapter("codex").sync(self.stage, target)
        self.assertEqual(sorted(self.codex_config()["mcp_servers"]), ["railway"])
        self.select()
        adapter("codex").sync(self.stage, target)
        self.assertNotIn("mcp_servers", self.codex_config())

    def test_codex_replaces_a_changed_server_and_its_subtable(self):
        target = self.home / ".codex"
        target.mkdir(parents=True)
        self.select(server("railway"))
        adapter("codex").sync(self.stage, target)
        # Codex re-serializes config.toml into sub-tables; the next render must
        # replace that shape too, never declare the table twice.
        text = (target / "config.toml").read_text()
        text += '\n[mcp_servers.railway.env]\nOLD = "1"\n'
        (target / "config.toml").write_text(text)
        adapter("codex").sync(self.stage, target)
        self.assertEqual(
            self.codex_config()["mcp_servers"]["railway"], {"command": "npx", "args": ["-y", "@example/railway"]}
        )

    def test_codex_recognizes_every_valid_table_header_shape(self):
        """Codex review 1 of PR 92: a header with a trailing comment was not
        recognized, so the render appended a second [mcp_servers.demo] and
        Codex refused the file; an indented header was not a boundary, so an
        unrelated server after a managed one was deleted with it."""
        target = self.home / ".codex"
        target.mkdir(parents=True)
        (target / "config.toml").write_text(
            "[mcp_servers.demo] # installed server\n"
            'command = "old"\n'
            "  [mcp_servers.neighbour]\n"
            '  command = "keep"\n'
            '[ mcp_servers . "spaced" ]\n'
            'command = "old"\n'
            "[mcp_servers.spaced.env]  # re-serialized sub-table\n"
            'OLD = "1"\n'
            "[[profiles_list]]\n"
            'name = "p"\n'
        )
        (target / "stratarc-mcp-servers.json").write_text(json.dumps({"mcp_servers": ["demo", "spaced"]}))
        self.select(server("demo"), server("spaced"))
        adapter("codex").sync(self.stage, target)
        config = self.codex_config()
        self.assertEqual(config["mcp_servers"]["demo"], {"command": "npx", "args": ["-y", "@example/demo"]})
        self.assertEqual(config["mcp_servers"]["spaced"], {"command": "npx", "args": ["-y", "@example/spaced"]})
        self.assertEqual(config["mcp_servers"]["neighbour"], {"command": "keep"})
        self.assertEqual(config["profiles_list"], [{"name": "p"}])
        self.select(server("spaced"))
        adapter("codex").sync(self.stage, target)
        config = self.codex_config()
        self.assertEqual(sorted(config["mcp_servers"]), ["neighbour", "spaced"])
        self.assertEqual(config["profiles_list"], [{"name": "p"}])

    def test_codex_replaces_inline_and_dotted_definitions(self):
        """Codex review 2 of PR 92: a managed server written as an inline
        table under [mcp_servers], or as a dotted key at the root, was left
        in place, so the render declared it twice and a deselect left it
        installed."""
        target = self.home / ".codex"
        cases = {
            "under the table": (
                '[mcp_servers]\ndemo = { command = "old" }\nneighbour = { command = "keep" }\n"quoted".command = "old"\n',
                ("demo", "quoted"),
            ),
            "dotted at the root": (
                'mcp_servers.demo = { command = "old" }\nmcp_servers.neighbour.command = "keep"\nmodel = "gpt"\n\n[other]\nx = 1\n',
                ("demo",),
            ),
        }
        for label, (text, managed) in cases.items():
            with self.subTest(label):
                target.mkdir(parents=True, exist_ok=True)
                (target / "config.toml").write_text(text)
                (target / "stratarc-mcp-servers.json").write_text(json.dumps({"mcp_servers": list(managed)}))
                self.select(*(server(name) for name in managed))
                adapter("codex").sync(self.stage, target)
                config = self.codex_config()
                for name in managed:
                    self.assertEqual(config["mcp_servers"][name], {"command": "npx", "args": ["-y", f"@example/{name}"]}, name)
                self.assertEqual(config["mcp_servers"]["neighbour"], {"command": "keep"})
                self.select()
                adapter("codex").sync(self.stage, target)
                self.assertEqual(self.codex_config()["mcp_servers"], {"neighbour": {"command": "keep"}})

    def test_codex_refuses_a_shape_it_cannot_replace_and_writes_nothing(self):
        target = self.home / ".codex"
        target.mkdir(parents=True)
        original = '[mcp_servers]\ndemo.args = [\n  "a",\n]\n'
        (target / "config.toml").write_text(original)
        (target / "stratarc-mcp-servers.json").write_text(json.dumps({"mcp_servers": ["demo"]}))
        self.select(server("demo"))
        with self.assertRaises(components_render.RenderRefused) as refused:
            adapter("codex").sync(self.stage, target)
        self.assertIn("demo", str(refused.exception))
        self.assertIn("by hand", str(refused.exception))
        self.assertEqual((target / "config.toml").read_text(), original)
        self.assertEqual(json.loads((target / "stratarc-mcp-servers.json").read_text()), {"mcp_servers": ["demo"]})

    def test_codex_refuses_to_write_without_a_toml_parser(self):
        """Codex review 4 of PR 92: on a Python without tomllib the
        verification was skipped and the line-based edit still written,
        which could leave Codex a file it cannot load."""
        from unittest.mock import patch

        target = self.home / ".codex"
        target.mkdir(parents=True)
        original = '[mcp_servers]\ndemo.args = [\n  "a",\n]\n'
        (target / "config.toml").write_text(original)
        (target / "stratarc-mcp-servers.json").write_text(json.dumps({"mcp_servers": ["demo"]}))
        self.select(server("demo"))
        with patch.object(components_render, "tomllib", None):
            with self.assertRaises(components_render.RenderRefused) as refused:
                adapter("codex").sync(self.stage, target)
        self.assertIn("3.11", str(refused.exception))
        self.assertEqual((target / "config.toml").read_text(), original)

    def test_codex_never_alters_content_outside_the_managed_servers(self):
        """Codex review 3 of PR 92: a header-shaped line inside a multi-line
        string was read as a real table, so updating demo deleted part of
        the operator's instructions; the result still parsed, so the check
        let it through. The render now compares everything outside the
        managed servers before and after, and refuses on any difference."""
        target = self.home / ".codex"
        target.mkdir(parents=True)
        original = (
            'developer_instructions = """\nExample:\n[mcp_servers.demo]\ncommand = "example"\n[notes]\nKeep this line.\n"""\n'
            '\n[mcp_servers.demo]\ncommand = "old"\n'
        )
        (target / "config.toml").write_text(original)
        (target / "stratarc-mcp-servers.json").write_text(json.dumps({"mcp_servers": ["demo"]}))
        self.select(server("demo"))
        with self.assertRaises(components_render.RenderRefused) as refused:
            adapter("codex").sync(self.stage, target)
        self.assertIn("outside", str(refused.exception))
        self.assertEqual((target / "config.toml").read_text(), original)

    # ----- Gemini CLI: mcpServers in settings.json -----

    def test_gemini_renders_a_selected_server_and_keeps_a_hand_added_one(self):
        target = self.home / ".gemini"
        target.mkdir(parents=True)
        (target / "settings.json").write_text(json.dumps({"theme": "x", "mcpServers": {"handmade": {"command": "mine"}}}))
        self.select(server("railway"), server("remote", command=None, args=None, url="https://example.test/mcp"))
        adapter("gemini").sync(self.stage, target)
        settings = json.loads((target / "settings.json").read_text())
        self.assertEqual(settings["mcpServers"]["railway"], {"command": "npx", "args": ["-y", "@example/railway"]})
        self.assertEqual(settings["mcpServers"]["remote"], {"httpUrl": "https://example.test/mcp"})
        self.assertEqual(settings["mcpServers"]["handmade"], {"command": "mine"})
        self.assertEqual(settings["theme"], "x")
        self.assertEqual(adapter("gemini").sync(self.stage, target), [])
        self.select()
        adapter("gemini").sync(self.stage, target)
        self.assertEqual(json.loads((target / "settings.json").read_text())["mcpServers"], {"handmade": {"command": "mine"}})

    # ----- OpenCode: mcp in opencode.jsonc -----

    def test_opencode_renders_a_selected_server_and_keeps_a_hand_added_one(self):
        target = self.home / ".config" / "opencode"
        target.mkdir(parents=True)
        (target / "opencode.jsonc").write_text('{\n  // mine\n  "mcp": {"handmade": {"type": "local", "command": ["mine"]}},\n}\n')
        self.select(server("railway", env={"MODE": "fast"}))
        adapter("opencode").sync(self.stage, target)
        config = json.loads(strip_jsonc((target / "opencode.jsonc").read_text()))
        self.assertEqual(
            config["mcp"]["railway"],
            {"type": "local", "command": ["npx", "-y", "@example/railway"], "environment": {"MODE": "fast"}, "enabled": True},
        )
        self.assertEqual(config["mcp"]["handmade"], {"type": "local", "command": ["mine"]})
        self.assertEqual(adapter("opencode").sync(self.stage, target), [])

    # ----- what a runtime cannot take is named -----

    def test_claude_and_cursor_write_no_mcp_configuration_and_say_why(self):
        self.select(server("railway"))
        claude = self.home / ".claude"
        cursor = self.home / ".cursor"
        adapter("claude").sync(self.stage, claude)
        adapter("cursor").sync(self.stage, cursor)
        self.assertFalse((self.home / ".claude.json").exists())
        self.assertFalse((cursor / "mcp.json").exists())
        claude_notes = components_render.notes(self.stage, "claude")
        cursor_notes = components_render.notes(self.stage, "cursor")
        self.assertTrue(any("railway" in n and "--mcp-config" in n for n in claude_notes), claude_notes)
        self.assertTrue(any("railway" in n and "live" in n for n in cursor_notes), cursor_notes)

    def test_a_server_with_a_secret_reference_is_named_and_never_written(self):
        target = self.home / ".codex"
        self.select(server("vault", env={"API_KEY": "secret://vault/key"}), server("railway"))
        adapter("codex").sync(self.stage, target)
        config = self.codex_config()
        self.assertEqual(sorted(config["mcp_servers"]), ["railway"])
        self.assertNotIn("secret://", (target / "config.toml").read_text())
        notes = components_render.notes(self.stage, "codex")
        self.assertTrue(any("vault" in n and "secret://" in n for n in notes), notes)

    def test_a_server_another_tool_owns_is_declared_not_rendered(self):
        target = self.home / ".gemini"
        self.select(server("reviewbot", owner="reviewbot"))
        adapter("gemini").sync(self.stage, target)
        settings_path = target / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        self.assertNotIn("reviewbot", settings.get("mcpServers") or {})
        self.assertTrue(any("reviewbot" in n and "installed by reviewbot" in n for n in components_render.notes(self.stage, "gemini")))

    def test_a_runtime_the_entry_does_not_name_gets_nothing(self):
        target = self.home / ".gemini"
        self.select(server("codex-only", runtimes=["codex"]))
        adapter("gemini").sync(self.stage, target)
        settings_path = target / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        self.assertNotIn("codex-only", settings.get("mcpServers") or {})

    def test_no_stage_manifest_changes_nothing(self):
        target = self.home / ".codex"
        target.mkdir(parents=True)
        (target / "config.toml").write_text("[mcp_servers.handmade]\ncommand = \"mine\"\n")
        self.assertEqual(adapter("codex").sync(self.stage, target), [])
        self.assertEqual(self.codex_config()["mcp_servers"], {"handmade": {"command": "mine"}})


class Models(unittest.TestCase):
    """C-28 of docs/adr/2026-09-23-load-only-selected-components.md: each
    adapter writes runtime_settings.<runtime>.model and subagent_model to the
    runtime's own keys and translates an agent's model through aliases; a
    Claude short name with no alias on another runtime is named, never passed
    through silently."""

    AGENT = "---\nname: helper\ndescription: helps\nmodel: opus\n---\nBody.\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = self.root / "stage"
        (self.stage / "agents").mkdir(parents=True)
        (self.stage / "agents" / "helper.md").write_text(self.AGENT)
        self.home = self.root / "home"

    def settings(self, runtime: str, block: dict) -> None:
        (self.stage / "components.json").write_text(
            json.dumps({"version": 1, "runtime_settings": {runtime: block}, "mcp_servers": [], "plugins": []}) + "\n"
        )

    def test_codex_writes_both_default_models_and_the_agent_alias(self):
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        target = self.home / ".codex"
        target.mkdir(parents=True)
        (target / "config.toml").write_text('approval_policy = "never"\n\n[agents]\nmax_threads = 4\n')
        self.settings("codex", {"model": "opus", "subagent_model": "sonnet", "aliases": {"opus": "gpt-5", "sonnet": "gpt-5-mini"}})
        adapter("codex").sync(self.stage, target)
        config = tomllib.loads((target / "config.toml").read_text())
        self.assertEqual(config["model"], "gpt-5")
        self.assertEqual(config["approval_policy"], "never")
        self.assertEqual(config["agents"], {"max_threads": 4, "default_subagent_model": "gpt-5-mini"})
        role = tomllib.loads((target / "agents" / "helper.toml").read_text())
        self.assertEqual(role["model"], "gpt-5")
        self.assertEqual(adapter("codex").sync(self.stage, target), [])
        self.assertEqual(components_render.notes(self.stage, "codex"), [])

    def test_non_claude_defaults_omit_inheritance_sentinels(self):
        for runtime in ("codex", "gemini", "opencode"):
            for sentinel in ("inherit", "default"):
                with self.subTest(runtime=runtime, sentinel=sentinel):
                    self.settings(runtime, {"model": sentinel, "subagent_model": sentinel})
                    self.assertEqual(components_render.default_models(self.stage, runtime), {"model": None, "subagent_model": None})

    def test_gemini_writes_model_name_and_the_agent_alias(self):
        target = self.home / ".gemini"
        self.settings("gemini", {"model": "opus", "aliases": {"opus": "gemini-2.5-pro"}})
        adapter("gemini").sync(self.stage, target)
        settings = json.loads((target / "settings.json").read_text())
        self.assertEqual(settings["model"], {"name": "gemini-2.5-pro"})
        self.assertIn('model: "gemini-2.5-pro"', (target / "agents" / "helper.md").read_text())

    def test_opencode_writes_model_and_the_agent_alias(self):
        target = self.home / ".config" / "opencode"
        self.settings("opencode", {"model": "opus", "aliases": {"opus": "openai/gpt-5"}})
        adapter("opencode").sync(self.stage, target)
        config = json.loads(strip_jsonc((target / "opencode.jsonc").read_text()))
        self.assertEqual(config["model"], "openai/gpt-5")
        self.assertIn('model: "openai/gpt-5"', (target / "agents" / "helper.md").read_text())

    def test_a_short_name_with_no_alias_is_named_and_not_written(self):
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        target = self.home / ".codex"
        self.settings("codex", {"model": "opus"})
        adapter("codex").sync(self.stage, target)
        config_path = target / "config.toml"
        config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
        self.assertNotIn("model", config)
        self.assertNotIn("model", tomllib.loads((target / "agents" / "helper.toml").read_text()))
        notes = " ".join(components_render.notes(self.stage, "codex"))
        self.assertIn("model opus", notes)
        self.assertIn("helper", notes)
        self.assertIn("runtime_settings.codex", notes)

    def test_a_subagent_model_with_no_runtime_wide_key_is_named(self):
        self.settings("gemini", {"subagent_model": "gemini-2.5-flash"})
        notes = " ".join(components_render.notes(self.stage, "gemini"))
        self.assertIn("subagent_model", notes)

    def test_claude_reads_its_own_short_names(self):
        self.settings("claude", {})
        self.assertEqual(components_render.notes(self.stage, "claude"), [])


class ReversePassAfterRendering(unittest.TestCase):
    """A rendered server is declared; a hand-added one is undeclared."""

    def test_rendered_is_declared_and_hand_added_is_undeclared(self):
        sync = pytest.importorskip("stratarc.sync")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = server("railway")
            (root / "components.json").write_text(json.dumps({"version": 1, "mcp_servers": [entry]}))
            stage = root / "stage"
            stage.mkdir()
            (stage / "components.json").write_text(json.dumps({"version": 1, "mcp_servers": [entry]}))
            target = root / ".codex"
            target.mkdir()
            (target / "config.toml").write_text('[mcp_servers.handmade]\ncommand = "mine"\n')
            adapter("codex").sync(stage, target)
            findings = sync.reverse_pass(root, stage, {"codex": ("stratarc.adapters.codex", target)}, home=root)
            labels = {f.kind: [] for f in findings["codex"]}
            for f in findings["codex"]:
                labels[f.kind].append(f.label)
            self.assertTrue(any("railway" in label for label in labels.get("declared", [])), labels)
            self.assertTrue(any("handmade" in label for label in labels.get("undeclared", [])), labels)
