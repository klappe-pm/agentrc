"""Tests for staging: the filtered view of a source root that one control-plane column hands to the adapters."""

from __future__ import annotations

import json
from importlib.resources import as_file
from pathlib import Path

import pytest

from stratarc import staging
from stratarc.resources import data_dir
from stratarc.rules_digest import render_root

TOKEN = "${STRATARC_SOURCE}"


class Plane:
    """A stand-in control plane: `rules`, `hooks` and so on name the ids a column selects; `enabled` names the single-cell rows."""

    def __init__(self, enabled=(), **selected):
        self._enabled = set(enabled)
        self._selected = {prefix: dict(value) if isinstance(value, dict) else value for prefix, value in selected.items()}

    def enabled(self, column: str, item: str) -> bool:
        return item in self._enabled

    def enabled_ids(self, column: str, prefix: str) -> set[str]:
        value = self._selected.get(prefix.rstrip(":") + "s", set())
        if isinstance(value, dict):
            return set(value.get(column, ()))
        return set(value)


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    return path


@pytest.fixture
def stage_of(root):
    """Build a stage for the source root and remove it afterwards."""

    def build(plane, column="global"):
        stage, notes = staging.build_stage(root, plane, column)
        return stage, notes

    yield build
    staging.cleanup_all()


def write(path: Path, text: str = "", *, binary: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary is not None:
        path.write_bytes(binary)
    else:
        path.write_text(text, encoding="utf-8")
    return path


class TestFlatRuleStaging:
    def test_stages_single_level_rules(self, root, stage_of):
        write(root / "rules" / "flat-rule.md", "# flat-rule\n")
        stage, notes = stage_of(Plane(rules={"flat-rule"}))
        assert notes == []
        assert (stage / "rules" / "flat-rule.md").read_text(encoding="utf-8") == "# flat-rule\n"

    def test_a_selected_rule_with_no_file_is_a_note(self, root, stage_of):
        _stage, notes = stage_of(Plane(rules={"absent"}))
        assert notes == ["rule:absent not found under rules/"]


class TestSourceRootRendering:
    """A staged rule has its token rendered to the root the stage is built from; a rule without the token is copied byte for byte."""

    @pytest.fixture
    def rules_root(self, root) -> Path:
        write(root / "rules" / "with-token.md", f"# with-token\n\n## binding\n\nRun `python3 {TOKEN}/scripts/x.py`.\n")
        write(root / "rules" / "plain.md", binary=b"# plain\n\n## binding\n\nNo token.\r\n")
        write(root / "rules" / "README.md", f"# README.MD\n\nSource: {TOKEN}/rules/.\n")
        write(root / "rules" / "tiers.json", json.dumps({"global": ["with-token.md", "plain.md"]}))
        return root

    def test_the_token_renders_to_the_stage_root(self, rules_root, stage_of):
        stage, notes = stage_of(Plane(rules={"with-token", "plain"}))
        assert notes == []
        staged = (stage / "rules" / "with-token.md").read_text(encoding="utf-8")
        assert TOKEN not in staged
        assert f"python3 {render_root(rules_root)}/scripts/x.py" in staged

    def test_the_readme_renders_too(self, rules_root, stage_of):
        stage, _ = stage_of(Plane(rules={"with-token", "plain"}))
        readme = (stage / "rules" / "README.md").read_text(encoding="utf-8")
        assert TOKEN not in readme
        assert f"Source: {render_root(rules_root)}/rules/." in readme

    def test_a_rule_without_the_token_is_byte_identical(self, rules_root, stage_of):
        stage, _ = stage_of(Plane(rules={"with-token", "plain"}))
        assert (stage / "rules" / "plain.md").read_bytes() == (rules_root / "rules" / "plain.md").read_bytes()

    def test_the_tier_manifest_is_staged_for_the_global_column(self, rules_root, stage_of):
        stage, _ = stage_of(Plane(rules={"with-token", "plain"}))
        assert (stage / "rules" / "tiers.json").read_bytes() == (rules_root / "rules" / "tiers.json").read_bytes()

    def test_the_tier_manifest_is_not_staged_for_a_project_column(self, rules_root, stage_of):
        stage, _ = stage_of(Plane(rules={"with-token", "plain"}), "some-project")
        assert not (stage / "rules" / "tiers.json").exists()

    def test_the_staged_agents_md_renders_the_token(self, root, stage_of):
        write(root / "AGENTS.md", f"# agents\n\nRun `python3 {TOKEN}/scripts/sync.py`.\n\nFull rule: `{TOKEN}/rules/x.md`\n")
        stage, _ = stage_of(Plane(enabled={"AGENTS.md"}))
        staged = (stage / "AGENTS.md").read_text(encoding="utf-8")
        assert TOKEN not in staged
        assert f"python3 {render_root(root)}/scripts/sync.py" in staged
        assert f"Full rule: `{render_root(root)}/rules/x.md`" in staged

    def test_a_projects_own_agents_md_renders_to_the_source_root(self, root, stage_of):
        write(root / "projects-root" / "demo" / "AGENTS.md", f"# demo\n\nSee {TOKEN}/docs.\n")
        stage, _ = stage_of(Plane(enabled={"project:AGENTS.md"}), "demo")
        assert (stage / "AGENTS.md").read_text(encoding="utf-8") == f"# demo\n\nSee {render_root(root)}/docs.\n"

    def test_a_binding_only_copy_points_at_the_stage_root(self, root, stage_of):
        write(root / "rules" / "with-token.md", f"# with-token\n\n## binding\n\nSee {TOKEN}/docs.\n\n## rationale\n\nWhy.\n")
        stage, _ = stage_of(Plane(rules={"with-token"}), "some-project")
        staged = (stage / "rules" / "with-token.md").read_text(encoding="utf-8")
        assert TOKEN not in staged
        assert f"See {render_root(root)}/docs." in staged
        assert f"Full rule: `{render_root(root)}/rules/with-token.md`." in staged
        assert "Why." not in staged


class TestSharedRulesInProjectStage:
    """A project stage carries a rule the user scope also ships as its binding section only, so the two copies never double the instruction size."""

    RULE = "# shared-rule\n\n## binding\n\nDo the thing.\n\n## rationale\n\nLong reasons.\n"

    def stage_rule(self, root, stage_of, global_rules: set[str], text: str = RULE) -> str:
        write(root / "rules" / "shared-rule.md", text)
        plane = Plane(rules={"global": global_rules, "some-project": {"shared-rule"}})
        stage, notes = stage_of(plane, "some-project")
        assert notes == []
        return (stage / "rules" / "shared-rule.md").read_text(encoding="utf-8")

    def test_a_rule_the_global_column_ships_is_staged_binding_only(self, root, stage_of):
        staged = self.stage_rule(root, stage_of, {"shared-rule"})
        assert "## binding\n\nDo the thing.\n" in staged
        assert "Long reasons." not in staged
        assert staged.startswith("# shared-rule\n")

    def test_a_rule_only_the_project_ships_is_staged_whole(self, root, stage_of):
        assert self.stage_rule(root, stage_of, set()) == self.RULE

    def test_a_shared_rule_without_a_binding_section_is_staged_whole(self, root, stage_of):
        whole = "# shared-rule\n\nAll of it.\n"
        assert self.stage_rule(root, stage_of, {"shared-rule"}, whole) == whole


class TestPackagedHooks:
    """With an empty source root the stage is built from the guards packaged with stratarc."""

    def test_a_selected_guard_is_staged_from_the_package(self, root, stage_of):
        stage, notes = stage_of(Plane(hooks={"prose-guard"}))
        assert notes == []
        assert (stage / "hooks" / "prose-guard.sh").read_bytes() == (
            Path(str(data_dir("hooks"))) / "prose-guard.sh"
        ).read_bytes()

    def test_the_guard_library_rides_along(self, root, stage_of):
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        assert (stage / "hooks" / "lib" / "guard-utils.sh").is_file()
        assert not list((stage / "hooks" / "lib").glob("*.test.*"))

    def test_the_registry_is_filtered_to_the_selected_guards(self, root, stage_of):
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        registry = json.loads((stage / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        commands = [h["command"] for groups in registry.values() for g in groups for h in g["hooks"]]
        assert commands == ["$HOME/.claude/hooks/prose-guard.sh"]

    def test_a_project_stage_rewrites_the_hook_path_prefix(self, root, stage_of):
        stage, _ = stage_of(Plane(hooks={"prose-guard"}), "some-project")
        text = (stage / "hooks" / "hooks.json").read_text(encoding="utf-8")
        assert "$CLAUDE_PROJECT_DIR/.claude/hooks/prose-guard.sh" in text
        assert "$HOME/.claude/hooks/" not in text

    def test_nothing_is_staged_when_no_guard_is_selected(self, root, stage_of):
        stage, _ = stage_of(Plane())
        assert not (stage / "hooks").exists()

    def test_a_guard_named_nowhere_is_a_note(self, root, stage_of):
        _stage, notes = stage_of(Plane(hooks={"no-such-guard"}))
        assert notes == ["hook:no-such-guard not found under hooks/"]

    def test_the_worktree_manifest_is_staged_for_the_global_column(self, root, stage_of):
        selected = {"worktree-create", "worktree-remove", "worktree-validate"}
        stage, notes = stage_of(Plane(hooks=selected))
        assert notes == []
        assert (stage / "hooks" / "claude-worktree-hooks.json").is_file()

    def test_the_worktree_manifest_is_not_staged_for_a_project(self, root, stage_of):
        selected = {"worktree-create", "worktree-remove", "worktree-validate"}
        stage, _ = stage_of(Plane(hooks=selected), "some-project")
        assert not (stage / "hooks" / "claude-worktree-hooks.json").exists()

    def stage_agent_graph(self, stage_of, selected: set[str], column: str = "global"):
        stage, notes = stage_of(Plane(hooks=selected), column)
        assert notes == []
        staged = stage / "hooks" / "claude-agent-graph-hooks.json"
        return json.loads(staged.read_text(encoding="utf-8")) if staged.is_file() else None

    def test_stages_the_agent_graph_manifest_when_both_hooks_are_selected(self, root, stage_of):
        manifest = self.stage_agent_graph(stage_of, {"agent-graph-session-start", "agent-graph-pre-edit"})
        assert set(manifest) == {"SessionStart", "PreToolUse"}

    def test_the_agent_graph_manifest_keeps_only_the_selected_hooks(self, root, stage_of):
        manifest = self.stage_agent_graph(stage_of, {"agent-graph-session-start"})
        assert set(manifest) == {"SessionStart"}

    def test_the_agent_graph_manifest_is_not_staged_when_no_hook_is_selected(self, root, stage_of):
        assert self.stage_agent_graph(stage_of, set()) is None

    def test_the_agent_graph_manifest_is_staged_for_the_global_column_only(self, root, stage_of):
        selected = {"agent-graph-session-start", "agent-graph-pre-edit"}
        assert self.stage_agent_graph(stage_of, selected, column="some-project") is None


class TestSourceOverlay:
    """The source root's own hooks/ overlays the packaged guards: per file, with the source winning."""

    def packaged(self, name: str) -> bytes:
        return (Path(str(data_dir("hooks"))) / name).read_bytes()

    def test_a_source_hook_of_the_same_name_replaces_the_packaged_one(self, root, stage_of):
        write(root / "hooks" / "prose-guard.sh", "#!/usr/bin/env bash\n# the source's own guard\n")
        stage, notes = stage_of(Plane(hooks={"prose-guard", "env-dump-guard"}))
        assert notes == []
        assert "the source's own guard" in (stage / "hooks" / "prose-guard.sh").read_text(encoding="utf-8")
        assert (stage / "hooks" / "env-dump-guard.sh").read_bytes() == self.packaged("env-dump-guard.sh")

    def test_a_hook_only_the_source_has_is_staged(self, root, stage_of):
        write(root / "hooks" / "block-database-commits.sh", "#!/usr/bin/env bash\n")
        stage, notes = stage_of(Plane(hooks={"block-database-commits"}))
        assert notes == []
        assert (stage / "hooks" / "block-database-commits.sh").is_file()

    def test_registries_merge_with_the_source_group_winning(self, root, stage_of):
        write(
            root / "hooks" / "hooks.json",
            json.dumps(
                {
                    "PreToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/prose-guard.sh", "timeout": 9}],
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/block-database-commits.sh"}],
                        },
                    ]
                }
            ),
        )
        write(root / "hooks" / "block-database-commits.sh", "#!/usr/bin/env bash\n")
        selected = {"prose-guard", "env-dump-guard", "block-database-commits"}
        stage, _ = stage_of(Plane(hooks=selected))
        registry = json.loads((stage / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        groups = registry["PreToolUse"]
        by_hook = {h["command"].rsplit("/", 1)[-1]: (g.get("matcher"), h.get("timeout")) for g in groups for h in g["hooks"]}
        # The source's prose-guard group replaced the packaged one rather than joining it.
        assert by_hook["prose-guard.sh"] == ("Write", 9)
        assert sum(1 for g in groups for h in g["hooks"] if h["command"].endswith("prose-guard.sh")) == 1
        # A packaged group the source does not mention survives, and the source's new group is added.
        assert "env-dump-guard.sh" in by_hook
        assert by_hook["block-database-commits.sh"] == ("Bash", None)

    def test_merge_registries_replaces_by_hook_names_and_appends_the_rest(self):
        base = {"Stop": [{"hooks": [{"command": "$HOME/.claude/hooks/a.sh"}]}, {"hooks": [{"command": "$HOME/.claude/hooks/b.sh"}]}]}
        over = {
            "Stop": [{"matcher": "x", "hooks": [{"command": "$HOME/.claude/hooks/b.sh"}]}, {"hooks": [{"command": "$HOME/.claude/hooks/c.sh"}]}],
            "SessionStart": [{"hooks": [{"command": "$HOME/.claude/hooks/d.sh"}]}],
        }
        merged = staging._merge_registries(base, over)
        assert [staging._group_hook_names(g) for g in merged["Stop"]] == [("a",), ("b",), ("c",)]
        assert merged["Stop"][1]["matcher"] == "x"
        assert list(merged) == ["Stop", "SessionStart"]

    def test_lib_is_copied_from_the_package_then_the_source(self, root, stage_of):
        write(root / "hooks" / "lib" / "guard-utils.sh", "# overridden by the source\n")
        write(root / "hooks" / "lib" / "extra.sh", "# only in the source\n")
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        lib = stage / "hooks" / "lib"
        assert (lib / "guard-utils.sh").read_text(encoding="utf-8") == "# overridden by the source\n"
        assert (lib / "extra.sh").read_text(encoding="utf-8") == "# only in the source\n"
        assert (lib / "secret-scan.sh").read_bytes() == self.packaged("lib/secret-scan.sh")

    def test_fixtures_and_tests_under_lib_are_never_staged(self, root, stage_of):
        write(root / "hooks" / "lib" / "fixtures" / "case.txt", "x\n")
        write(root / "hooks" / "lib" / "check.test.sh", "#!/usr/bin/env bash\n")
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        lib = stage / "hooks" / "lib"
        assert lib.is_dir()
        assert not (lib / "fixtures").exists()
        assert not (lib / "check.test.sh").exists()

    def test_package_data_never_holds_a_private_directory(self):
        packaged = data_dir("hooks")
        with as_file(packaged) as hooks:
            assert not [p for p in Path(hooks).rglob("*") if p.name == "private"]

    def test_a_package_only_stage_has_no_private_directory(self, root, stage_of):
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        assert (stage / "hooks" / "lib").is_dir()
        assert not (stage / "hooks" / "lib" / "private").exists()

    def test_source_root_private_files_are_staged_for_the_global_column(self, root, stage_of):
        write(root / "hooks" / "lib" / "private" / "env-dump-patterns.json", '{"p": 1}\n')
        write(root / "hooks" / "lib" / "private" / "nested" / "more.json", "{}\n")
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        private = stage / "hooks" / "lib" / "private"
        assert (private / "env-dump-patterns.json").read_text(encoding="utf-8") == '{"p": 1}\n'
        assert (private / "nested" / "more.json").is_file()

    def test_private_files_are_not_staged_where_no_hook_is_carried(self, root, stage_of):
        write(root / "hooks" / "lib" / "private" / "env-dump-patterns.json", "{}\n")
        stage, _ = stage_of(Plane())
        assert not (stage / "hooks").exists()

    def test_private_files_are_not_staged_into_a_project_column(self, root, stage_of):
        write(root / "hooks" / "lib" / "private" / "env-dump-patterns.json", "{}\n")
        stage, _ = stage_of(Plane(hooks={"prose-guard"}), column="demo")
        assert (stage / "hooks" / "lib").is_dir()
        assert not (stage / "hooks" / "lib" / "private").exists()

    def test_a_private_file_removed_from_the_source_is_pruned_from_the_runtime(self, root, stage_of, tmp_path):
        from stratarc.adapters._common import mirror_dir

        private = write(root / "hooks" / "lib" / "private" / "env-dump-patterns.json", "{}\n")
        target = tmp_path / "runtime" / "hooks"
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        mirror_dir(stage / "hooks", target, delete_extra=True)
        assert (target / "lib" / "private" / "env-dump-patterns.json").is_file()
        private.unlink()
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        mirror_dir(stage / "hooks", target, delete_extra=True)
        assert not (target / "lib" / "private" / "env-dump-patterns.json").exists()
        assert not (target / "lib" / "private").exists()

    def test_the_source_runtime_hooks_module_wins(self, root, stage_of):
        write(root / "hooks" / "opencode-runtime-hooks.ts", "// the source's own\n")
        stage, _ = stage_of(Plane(hooks={"prose-guard"}))
        assert (stage / "hooks" / "opencode-runtime-hooks.ts").read_text(encoding="utf-8") == "// the source's own\n"


class TestComponentStaging:
    """The global stage carries a components.json holding only the wanted MCP servers and plugins the column selects."""

    MANIFEST = {
        "version": 1,
        "mcp_servers": [
            {"name": "chosen", "runtimes": ["codex"], "owner": "me", "wanted": True, "command": "npx"},
            {"name": "left-out", "runtimes": ["codex"], "owner": "me", "wanted": True, "command": "npx"},
            {"name": "unwanted", "runtimes": ["codex"], "owner": "x", "wanted": False, "command": "x"},
        ],
        "plugins": [
            {"name": "plug", "runtimes": ["claude"], "owner": "acme", "wanted": True, "marketplace": "m"},
        ],
        "third_party_skills": [
            {"name": "s", "runtimes": ["claude"], "owner": "o", "wanted": False, "installer": "o"},
        ],
    }

    def stage_manifest(self, root, stage_of, column: str, **extra):
        write(root / "components.json", json.dumps({**self.MANIFEST, **extra}))
        plane = Plane(mcps={"chosen", "unwanted"}, plugins={"plug"})
        stage, _ = stage_of(plane, column)
        path = stage / "components.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def test_global_stage_holds_only_selected_wanted_components(self, root, stage_of):
        staged = self.stage_manifest(root, stage_of, "global")
        assert [e["name"] for e in staged["mcp_servers"]] == ["chosen"]
        assert [e["name"] for e in staged["plugins"]] == ["plug"]
        assert "third_party_skills" not in staged

    def test_global_stage_carries_runtime_settings(self, root, stage_of):
        settings = {"claude": {"settings": {"theme": "dark"}}}
        assert self.stage_manifest(root, stage_of, "global", runtime_settings=settings)["runtime_settings"] == settings

    def test_project_stage_carries_its_own_selection_and_no_runtime_settings(self, root, stage_of):
        staged = self.stage_manifest(root, stage_of, "demo", runtime_settings={"claude": {"model": "m"}})
        assert [e["name"] for e in staged["mcp_servers"]] == ["chosen"]
        assert [e["name"] for e in staged["plugins"]] == ["plug"]
        assert "runtime_settings" not in staged

    def test_no_manifest_stages_no_components(self, root, stage_of):
        stage, _ = stage_of(Plane())
        assert not (stage / "components.json").exists()

    def test_an_unreadable_manifest_stages_no_components(self, root, stage_of):
        write(root / "components.json", "{not json")
        stage, _ = stage_of(Plane())
        assert not (stage / "components.json").exists()


class TestForeignHookStaging:
    """A wanted foreign hook is rendered by every adapter, so the global stage carries it and the file its registration names."""

    MANIFEST = {
        "version": 1,
        "foreign_hooks": [
            {
                "name": "review-agent-hook",
                "runtimes": ["claude"],
                "owner": "acme",
                "wanted": True,
                "match": "reviewer agent-hook run",
                "registration": {"claude": "scripts/reviewer/claude.json"},
            },
            {"name": "other", "runtimes": ["claude"], "owner": "other", "wanted": False, "match": "other"},
        ],
        "third_party_skills": [
            {
                "name": "reviewer",
                "runtimes": ["claude"],
                "owner": "acme",
                "wanted": True,
                "installer": "reviewer skills install",
                "skills": ["reviewer-review"],
            }
        ],
    }
    BODY = {"hooks": {"Stop": [{"hooks": [{"command": "reviewer agent-hook run", "type": "command"}]}]}}

    def stage_hooks(self, root, stage_of, column: str):
        write(root / "components.json", json.dumps(self.MANIFEST))
        write(root / "scripts" / "reviewer" / "claude.json", json.dumps(self.BODY))
        stage, _ = stage_of(Plane(), column)
        return stage, json.loads((stage / "components.json").read_text(encoding="utf-8"))

    def test_the_global_stage_carries_the_wanted_entries_only(self, root, stage_of):
        _stage, staged = self.stage_hooks(root, stage_of, "global")
        assert [e["name"] for e in staged["foreign_hooks"]] == ["review-agent-hook"]
        assert [e["name"] for e in staged["third_party_skills"]] == ["reviewer"]

    def test_the_global_stage_carries_the_registration_file_at_its_own_path(self, root, stage_of):
        stage, _ = self.stage_hooks(root, stage_of, "global")
        captured = stage / "scripts" / "reviewer" / "claude.json"
        assert captured.is_file()
        assert json.loads(captured.read_text(encoding="utf-8")) == self.BODY

    def test_a_project_stage_carries_neither(self, root, stage_of):
        _stage, staged = self.stage_hooks(root, stage_of, "demo")
        assert "foreign_hooks" not in staged
        assert "third_party_skills" not in staged


class TestProjectDirectories:
    def test_a_project_stage_carries_the_directories_its_rows_select(self, root, stage_of):
        write(root / "projects-root" / "demo" / "rules" / "local.md", "# local\n")
        write(root / "projects-root" / "demo" / "agents" / "helper.md", "# helper\n")
        stage, _ = stage_of(Plane(enabled={"project:rules"}), "demo")
        assert (stage / "rules" / "local.md").is_file()
        assert not (stage / "agents").exists()

    def test_a_project_skill_replaces_the_shared_skill_of_the_same_name(self, root, stage_of, capsys):
        write(root / "skills" / "tidy" / "SKILL.md", "shared\n")
        write(root / "projects-root" / "demo" / "skills" / "tidy" / "SKILL.md", "project\n")
        plane = Plane(enabled={"project:skills"}, skills={"demo": {"tidy"}})
        stage, _ = stage_of(plane, "demo")
        assert (stage / "skills" / "tidy" / "SKILL.md").read_text(encoding="utf-8") == "project\n"
        assert "replaces the shared skill:tidy" in capsys.readouterr().err


class TestEngineNameInTheStage:
    """A stage holds no stratarc.toml of its own, so it records the source root's engine name for the adapters that read it."""

    def test_a_custom_named_source_root_renders_the_servers_it_owns(self, root, stage_of):
        from stratarc.adapters import _components

        write(root / "stratarc.toml", 'name = "custom"\n')
        manifest = {
            "version": 1,
            "mcp_servers": [{"name": "mine", "runtimes": ["codex"], "owner": "custom", "wanted": True, "command": "npx"}],
        }
        write(root / "components.json", json.dumps(manifest))
        stage, _ = stage_of(Plane(mcps={"mine"}))
        rendered, notes = _components.selected_servers(stage, "codex")
        assert list(rendered) == ["mine"]
        assert notes == []
        assert _components.ledger_name(stage) == "custom-mcp-servers.json"
