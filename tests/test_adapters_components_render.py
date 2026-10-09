"""Direct tests of scripts/adapters/_components.py's own functions (issue #276).

scripts/adapters/components.test.py drives this module through each adapter's
sync; this file calls its functions directly, so a regression in one shape,
selection rule, alias rule, TOML spelling or ledger step fails here by name.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest


from stratarc.adapters import _components as c  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(stratarc_home):
    """Every test reads the home through STRATARC_HOME, never the real one."""
    return stratarc_home



def server(name, **extra):
    entry = {"name": name, "wanted": True, "owner": "stratarc", "runtimes": ["codex", "gemini", "opencode", "claude"]}
    entry.update(extra)
    return entry


class StageCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.target = self.root / "target"

    def tearDown(self):
        self.directory.cleanup()

    def write_stage(self, document):
        (self.stage / c.STAGED).write_text(json.dumps(document), encoding="utf-8")


class ShapeTests(unittest.TestCase):
    def test_neutral_server_keeps_only_set_runtime_neutral_keys(self):
        entry = server("x", command="run", args=["a"], env={}, url="", transport="stdio", notes="dropped")
        self.assertEqual(c.neutral_server(entry), {"command": "run", "args": ["a"], "transport": "stdio"})

    def test_without_secrets_drops_only_secret_references(self):
        entry = {"command": "run", "env": {"A": "secret://ns/key", "B": "plain", "C": 3}}
        self.assertEqual(c.without_secrets(entry), {"command": "run", "env": {"B": "plain", "C": 3}})
        self.assertEqual(c.without_secrets({"command": "run", "env": {"A": "secret://ns/key"}}), {"command": "run"})

    def test_toml_value_spells_every_supported_type(self):
        self.assertEqual(c.toml_value(True), "true")
        self.assertEqual(c.toml_value(False), "false")
        self.assertEqual(c.toml_value(7), "7")
        self.assertEqual(c.toml_value('say "hi"'), '"say \\"hi\\""')
        self.assertEqual(c.toml_value(["a", 1]), '["a", 1]')
        self.assertEqual(c.toml_value({"bare_key": "v", "needs quote": 1}), '{bare_key = "v", "needs quote" = 1}')
        with self.assertRaises(ValueError):
            c.toml_value(1.5)

    def test_codex_server_drops_transport(self):
        self.assertEqual(c.codex_server(server("x", url="https://h", transport="sse")), {"url": "https://h"})

    def test_claude_server_types_a_command_as_stdio_and_a_url_by_transport(self):
        self.assertEqual(c.claude_server(server("x", command="run")), {"command": "run", "type": "stdio"})
        self.assertEqual(c.claude_server(server("x", url="https://h")), {"url": "https://h", "type": "http"})
        self.assertEqual(c.claude_server(server("x", url="https://h", transport="sse")), {"url": "https://h", "type": "sse"})

    def test_gemini_server_uses_http_url_unless_the_transport_is_sse(self):
        self.assertEqual(c.gemini_server(server("x", url="https://h")), {"httpUrl": "https://h"})
        self.assertEqual(c.gemini_server(server("x", url="https://h", transport="sse")), {"url": "https://h"})

    def test_opencode_server_local_and_remote_shapes(self):
        local = server("x", command="run", args=["a", "b"], env={"K": "v"})
        self.assertEqual(
            c.opencode_server(local, True),
            {"type": "local", "command": ["run", "a", "b"], "environment": {"K": "v"}, "enabled": True},
        )
        self.assertEqual(c.opencode_server(server("x", url="https://h"), False), {"type": "remote", "url": "https://h", "enabled": False})

    def test_a_disabled_opencode_server_carries_no_secret_reference(self):
        entry = server("x", command="run", env={"K": "secret://ns/key"})
        self.assertEqual(c.opencode_server(entry, False), {"type": "local", "command": ["run"], "enabled": False})
        self.assertEqual(c.opencode_server(entry, True)["environment"], {"K": "secret://ns/key"})


class SelectionTests(StageCase):
    def test_staged_is_empty_for_a_missing_malformed_or_non_object_manifest(self):
        self.assertEqual(c.staged(self.stage), {})
        (self.stage / c.STAGED).write_text("{", encoding="utf-8")
        self.assertEqual(c.staged(self.stage), {})
        (self.stage / c.STAGED).write_text("[1]", encoding="utf-8")
        self.assertEqual(c.staged(self.stage), {})

    def test_renders_requires_a_named_llm_root_owned_non_hosted_entry_for_the_runtime(self):
        self.assertTrue(c.renders(server("x"), "codex"))
        self.assertFalse(c.renders(server("x"), "cursor"))
        self.assertFalse(c.renders(server(""), "codex"))
        self.assertFalse(c.renders(server("x", owner="other"), "codex"))
        self.assertFalse(c.renders(server("x", hosted=True), "codex"))
        self.assertFalse(c.renders("not a dict", "codex"))

    def test_selected_servers_renders_and_names_each_left_out_server(self):
        self.write_stage({
            "mcp_servers": [
                server("keep", command="run"),
                server("unwanted", wanted=False),
                server("other-runtime", runtimes=["gemini"]),
                server("foreign", owner="reviewbot"),
                server("hosted", hosted=True),
                server("secret", command="run", env={"B": "secret://ns/b", "A": "secret://ns/a"}),
                "not a dict",
                server(""),
            ]
        })
        rendered, notes = c.selected_servers(self.stage, "codex")
        self.assertEqual(list(rendered), ["keep"])
        text = "\n".join(notes)
        self.assertEqual(len(notes), 3)
        self.assertIn("mcp server foreign is not rendered: installed by reviewbot", text)
        self.assertIn("mcp server hosted is not rendered: a hosted connector", text)
        self.assertIn("mcp server secret is not rendered: env A, B is a secret:// reference", text)

    def test_claude_user_scope_is_named_but_a_project_scope_renders(self):
        self.write_stage({"mcp_servers": [server("keep", command="run")]})
        rendered, notes = c.selected_servers(self.stage, "claude")
        self.assertEqual(rendered, {})
        self.assertIn("mcp server keep is not rendered at user scope", notes[0])
        rendered, notes = c.selected_servers(self.stage, "claude", project=True)
        self.assertEqual((list(rendered), notes), (["keep"], []))

    def test_plugin_key_and_selected_plugins(self):
        self.assertEqual(c.plugin_key({"name": "p", "marketplace": "m"}), "p@m")
        self.assertEqual(c.plugin_key({"name": "p"}), "p")
        self.write_stage({
            "plugins": [
                {"name": "a", "marketplace": "m", "wanted": True, "runtimes": ["claude"]},
                {"name": "b", "wanted": False, "runtimes": ["claude"]},
                {"name": "c", "wanted": True, "runtimes": ["codex"]},
                {"wanted": True, "runtimes": ["claude"]},
            ]
        })
        self.assertEqual(list(c.selected_plugins(self.stage, "claude")), ["a@m"])

    def test_enablement_writes_every_installed_or_selected_key_true_only_when_selected(self):
        self.assertEqual(
            c.enablement(["old@m", "kept@m"], ["kept@m", "new@m"]),
            {"kept@m": True, "new@m": True, "old@m": False},
        )

    def test_notes_names_a_selected_plugin_a_runtime_does_not_enable(self):
        self.write_stage({"plugins": [{"name": "p", "wanted": True, "runtimes": ["codex", "claude"]}]})
        self.assertTrue(any(n.startswith("plugin p is selected but not enabled here") for n in c.notes(self.stage, "codex")))
        self.assertEqual(c.notes(self.stage, "claude"), [])


class ModelTests(StageCase):
    def test_alias_for_and_translate_model(self):
        aliases = {"opus": "gpt-x", "empty": "", "number": 3}
        self.assertEqual(c.alias_for(" opus ", aliases), "gpt-x")
        self.assertIsNone(c.alias_for("empty", aliases))
        self.assertIsNone(c.alias_for("number", aliases))
        self.assertIsNone(c.alias_for("opus", None))
        self.assertEqual(c.translate_model("opus", aliases), "gpt-x")
        self.assertEqual(c.translate_model(" other ", aliases), "other")
        self.assertIsNone(c.translate_model("  ", aliases))
        self.assertIsNone(c.translate_model(None, aliases))

    def test_runtime_settings_is_empty_unless_an_object(self):
        self.write_stage({"runtime_settings": {"codex": ["not", "a", "dict"], "gemini": {"model": "m"}}})
        self.assertEqual(c.runtime_settings(self.stage, "codex"), {})
        self.assertEqual(c.runtime_settings(self.stage, "gemini"), {"model": "m"})
        self.assertEqual(c.runtime_settings(self.stage, "opencode"), {})

    def test_default_models_translate_and_leave_out_what_a_runtime_cannot_read(self):
        self.write_stage({
            "runtime_settings": {
                "codex": {"model": "opus", "subagent_model": "sonnet", "aliases": {"opus": "gpt-big"}},
                "gemini": {"model": "gemini-pro", "subagent_model": "gemini-pro"},
                "opencode": {"model": "inherit"},
                "claude": {"model": "opus", "subagent_model": "sonnet"},
            }
        })
        self.assertEqual(c.default_models(self.stage, "codex"), {"model": "gpt-big", "subagent_model": None})
        self.assertEqual(c.default_models(self.stage, "gemini"), {"model": "gemini-pro", "subagent_model": None})
        self.assertEqual(c.default_models(self.stage, "opencode"), {"model": None, "subagent_model": None})
        self.assertEqual(c.default_models(self.stage, "claude"), {"model": "opus", "subagent_model": "sonnet"})

    def test_agent_aliases_keeps_only_string_pairs(self):
        self.write_stage({"runtime_settings": {"codex": {"aliases": {"opus": "gpt-big", "bad": "", "n": 1}}}})
        self.assertEqual(c.agent_aliases(self.stage, "codex"), {"opus": "gpt-big"})
        self.assertEqual(c.agent_aliases(self.stage, "gemini"), {})

    def test_notes_name_an_unaliased_agent_model_once_per_model(self):
        self.write_stage({"runtime_settings": {"codex": {"aliases": {}}}})
        agents = self.stage / "agents"
        agents.mkdir()
        (agents / "one.md").write_text("---\nname: one\nmodel: opus\n---\nbody\n", encoding="utf-8")
        (agents / "two.md").write_text("---\nname: two\nmodel: opus\n---\nbody\n", encoding="utf-8")
        (agents / "three.md").write_text("---\nname: three\nmodel: inherit\n---\nbody\n", encoding="utf-8")
        notes = c.notes(self.stage, "codex")
        self.assertEqual(notes, [
            "model opus (agents one, two) has no alias in runtime_settings.codex.aliases in components.json; "
            "each of those agents inherits the session model"
        ])
        self.assertEqual(c.notes(self.stage, "claude"), [])


class TomlKeyTests(unittest.TestCase):
    def test_header_path_reads_every_header_spelling(self):
        self.assertEqual(c._header_path("[mcp_servers.x]"), ("mcp_servers", "x"))
        self.assertEqual(c._header_path('  [ mcp_servers . "a.b" ]  # note'), ("mcp_servers", "a.b"))
        self.assertEqual(c._header_path("[[mcp_servers.'lit']]"), ("mcp_servers", "lit"))
        self.assertIsNone(c._header_path("key = 1"))
        self.assertEqual(c._header_path("[mcp_servers.$bad]"), ())

    def test_line_key_path_reads_dotted_and_quoted_keys(self):
        self.assertEqual(c._line_key_path("x.command = 'run'"), ("x", "command"))
        self.assertEqual(c._line_key_path('"a b" = 1'), ("a b",))
        self.assertEqual(c._line_key_path("not an assignment"), ())
        self.assertEqual(c._line_key_path("# comment"), ())

    def test_remove_codex_server_removes_every_definition_of_one_name_only(self):
        text = (
            "model = 'm'\n"
            "mcp_servers.gone.command = 'a'\n"
            "[mcp_servers.gone]\ncommand = 'a'\n"
            "[mcp_servers.gone.env]\nK = 'v'\n"
            "[mcp_servers]\ngone = { command = 'a' }\nkept = { command = 'b' }\n"
            "[mcp_servers.kept2]\ncommand = 'c'\n"
        )
        self.assertEqual(
            c._remove_codex_server(text, "gone"),
            "model = 'm'\n[mcp_servers]\nkept = { command = 'b' }\n[mcp_servers.kept2]\ncommand = 'c'\n",
        )

    def test_codex_table_spells_one_server(self):
        self.assertEqual(
            c._codex_table("x", {"command": "run", "args": ["a"]}),
            '[mcp_servers.x]\ncommand = "run"\nargs = ["a"]\n',
        )


class RenderTests(StageCase):
    def ledger(self):
        path = self.target / c.ledger_name()
        return json.loads(path.read_text()) if path.exists() else None

    def test_merge_mapping_drops_previous_names_no_longer_desired(self):
        merged = c._merge_mapping({"hand": 1, "old": 2, "keep": 3}, {"keep": 4, "new": 5}, ["old", "keep"])
        self.assertEqual(merged, {"hand": 1, "keep": 4, "new": 5})
        self.assertEqual(c._merge_mapping(None, {"a": 1}, []), {"a": 1})

    def test_gemini_render_writes_the_server_and_the_ledger_then_removes_both(self):
        self.write_stage({"mcp_servers": [server("s", command="run")]})
        self.target.mkdir()
        (self.target / "settings.json").write_text(json.dumps({"theme": "x", "mcpServers": {"hand": {"command": "h"}}}))
        self.assertEqual(c.render("gemini", self.stage, self.target, False), ["render components.json mcp servers into settings.json"])
        settings = json.loads((self.target / "settings.json").read_text())
        self.assertEqual(settings["mcpServers"], {"hand": {"command": "h"}, "s": {"command": "run"}})
        self.assertEqual(self.ledger(), {"mcp_servers": ["s"]})
        self.assertEqual(c.render("gemini", self.stage, self.target, False), [])
        self.write_stage({"mcp_servers": []})
        c.render("gemini", self.stage, self.target, False)
        settings = json.loads((self.target / "settings.json").read_text())
        self.assertEqual(settings, {"theme": "x", "mcpServers": {"hand": {"command": "h"}}})
        self.assertIsNone(self.ledger())

    def test_the_last_rendered_server_removed_drops_the_key(self):
        self.write_stage({"mcp_servers": [server("s", command="run")]})
        c.render("gemini", self.stage, self.target, False)
        self.write_stage({})
        c.render("gemini", self.stage, self.target, False)
        self.assertEqual(json.loads((self.target / "settings.json").read_text()), {})

    def test_a_dry_run_reports_the_action_and_writes_nothing(self):
        self.write_stage({"mcp_servers": [server("s", command="run")]})
        self.assertEqual(c.render("opencode", self.stage, self.target, True), ["render components.json mcp servers into opencode.jsonc"])
        self.assertFalse(self.target.exists())

    def test_opencode_render_reads_jsonc(self):
        self.write_stage({"mcp_servers": [server("s", command="run")]})
        self.target.mkdir()
        (self.target / "opencode.jsonc").write_text('{\n  // a comment\n  "theme": "x"\n}\n')
        c.render("opencode", self.stage, self.target, False)
        written = json.loads((self.target / "opencode.jsonc").read_text())
        self.assertEqual(written, {"theme": "x", "mcp": {"s": {"type": "local", "command": ["run"], "enabled": True}}})

    def test_codex_render_is_idempotent_and_keeps_other_content(self):
        if c.tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        self.write_stage({"mcp_servers": [server("s", command="run", args=["a"])]})
        self.target.mkdir()
        (self.target / "config.toml").write_text("model = 'm'\n")
        self.assertEqual(c.render("codex", self.stage, self.target, False), ["render components.json mcp servers into config.toml"])
        text = (self.target / "config.toml").read_text()
        parsed = c.tomllib.loads(text)
        self.assertEqual(parsed, {"model": "m", "mcp_servers": {"s": {"command": "run", "args": ["a"]}}})
        self.assertEqual(c.render("codex", self.stage, self.target, False), [])
        self.assertEqual((self.target / "config.toml").read_text(), text)

    def test_codex_render_refuses_a_file_that_does_not_parse(self):
        if c.tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        self.write_stage({"mcp_servers": [server("s", command="run")]})
        self.target.mkdir()
        (self.target / "config.toml").write_text("model = \n")
        with self.assertRaises(c.RenderRefused):
            c.render("codex", self.stage, self.target, False)
        self.assertEqual((self.target / "config.toml").read_text(), "model = \n")
        self.assertIsNone(self.ledger())

    def test_a_runtime_without_a_renderer_does_nothing(self):
        self.write_stage({"mcp_servers": [server("s", command="run")]})
        self.assertEqual(c.render("claude", self.stage, self.target, False), [])
        self.assertEqual(c.render("cursor", self.stage, self.target, False), [])
        self.assertFalse(self.target.exists())

    def test_nothing_desired_and_nothing_rendered_before_touches_nothing(self):
        for runtime in c.RENDERED_RUNTIMES:
            with self.subTest(runtime):
                self.assertEqual(c.render(runtime, self.stage, self.target, False), [])
        self.assertFalse(self.target.exists())
