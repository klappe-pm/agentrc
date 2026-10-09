"""Tests for the project permissions sweep."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from stratarc import deploy_guard, project_permissions
from stratarc.permissions import (
    claude_project_permissions,
    codex_approval_policy,
    gemini_approval_mode,
    opencode_permission,
)
from stratarc.project_permissions import candidates, projects_dir, refuse_if_stale, short, sweep

# The canonical policy written into <source>/permissions.json for every test. Expected values are computed from this dict through the permissions helpers rather than hardcoded, so the tests track the renderer instead of duplicating it.
POLICY = json.loads((Path(__file__).resolve().parent / "fixtures" / "project_permissions" / "permissions.json").read_text(encoding="utf-8"))

SCOPED = "scoped-project"


def write_json(path: Path, data) -> None:
    """Write a JSON file at path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def write_text(path: Path, text: str) -> None:
    """Write a text file at path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def read_json(path: Path):
    return json.loads(path.read_text())


@pytest.fixture
def source(tmp_path: Path, stratarc_home: Path) -> Path:
    path = tmp_path.resolve() / "source-root"
    write_json(path / "permissions.json", POLICY)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "projects"
    path.mkdir()
    return path


class TestSweep:
    def test_a_claude_file_with_permissions_and_hooks_keeps_its_key_order(self, source, root):
        settings_path = root / "proj1" / ".claude" / "settings.json"
        write_json(
            settings_path,
            {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "permissions": {"allow": ["Bash(old *)"], "deny": ["Bash(worse *)"], "defaultMode": "acceptEdits"},
                "hooks": {"PreToolUse": []},
            },
        )

        actions = sweep(source, root)

        settings = read_json(settings_path)
        assert settings["permissions"] == claude_project_permissions(POLICY)
        assert "defaultMode" not in settings["permissions"]
        assert settings["hooks"] == {"PreToolUse": []}
        assert list(settings) == ["$schema", "permissions", "hooks"]
        assert actions == [f"write {short(settings_path)}"]

    def test_a_claude_file_with_neither_permissions_nor_auto_mode_is_untouched(self, source, root):
        settings_path = root / "proj2" / ".claude" / "settings.json"
        original = json.dumps({"hooks": {"PreToolUse": []}}, indent=2) + "\n"
        write_text(settings_path, original)

        actions = sweep(source, root)

        assert settings_path.read_text() == original
        assert actions == []

    def test_a_top_level_auto_mode_with_no_permissions_is_removed_and_replaced(self, source, root):
        settings_path = root / "proj3" / ".claude" / "settings.json"
        write_json(settings_path, {"autoMode": {"allow": ["$defaults"]}, "theme": "dark"})

        sweep(source, root)

        settings = read_json(settings_path)
        assert "autoMode" not in settings
        assert settings["permissions"] == claude_project_permissions(POLICY)
        assert settings["theme"] == "dark"

    def test_settings_local_json_is_never_listed_or_touched(self, source, root):
        claude_dir = root / "proj4" / ".claude"
        local_path = claude_dir / "settings.local.json"
        write_json(claude_dir / "settings.json", {"permissions": {"allow": ["Bash(old *)"]}})
        local_original = json.dumps({"permissions": {"allow": ["Bash(local *)"]}}, indent=2) + "\n"
        write_text(local_path, local_original)

        assert local_path not in [path for _, path in candidates(root)]

        sweep(source, root)

        assert local_path.read_text() == local_original

    def test_a_file_under_a_pruned_directory_is_never_descended_into(self, source, root):
        nested = root / "proj5" / "node_modules" / "pkg" / ".claude" / "settings.json"
        original = json.dumps({"permissions": {"allow": ["Bash(old *)"]}}, indent=2) + "\n"
        write_text(nested, original)

        assert nested not in [path for _, path in candidates(root)]

        sweep(source, root)

        assert nested.read_text() == original

    def test_a_codex_top_level_section_is_rewritten_and_the_table_tail_is_preserved(self, source, root):
        config_path = root / "proj6" / ".codex" / "config.toml"
        tail = '[mcp_servers.graph]\ncommand = "graph-mcp"\napproval_policy = "untrusted"\n'
        write_text(
            config_path,
            'model = "x"\napproval_policy = "on-request"\nsandbox_mode = "workspace-write"\ndefault_permissions = "mine"\n\n' + tail,
        )

        sweep(source, root)

        top, marker, new_tail = config_path.read_text().partition("[mcp_servers.graph]\n")
        assert marker, "table header should still be present"
        assert marker + new_tail == tail
        assert f"approval_policy = {json.dumps(codex_approval_policy(POLICY))}" in top
        assert "sandbox_mode" not in top
        assert "default_permissions" not in top
        assert 'model = "x"' in top

    def test_a_codex_config_with_only_a_table_is_untouched(self, source, root):
        config_path = root / "proj7" / ".codex" / "config.toml"
        original = '[mcp_servers.graph]\ncommand = "graph-mcp"\n'
        write_text(config_path, original)

        actions = sweep(source, root)

        assert config_path.read_text() == original
        assert actions == []

    def test_a_gemini_default_approval_mode_is_rewritten_and_mcp_servers_are_kept(self, source, root):
        settings_path = root / "proj8" / ".gemini" / "settings.json"
        write_json(settings_path, {"mcpServers": {"graph": {"command": "graph-mcp"}}, "general": {"defaultApprovalMode": "default"}})

        sweep(source, root)

        settings = read_json(settings_path)
        assert settings["general"]["defaultApprovalMode"] == gemini_approval_mode(POLICY)
        assert settings["mcpServers"] == {"graph": {"command": "graph-mcp"}}

    def test_gemini_settings_without_an_approval_mode_are_untouched(self, source, root):
        settings_path = root / "proj9" / ".gemini" / "settings.json"
        original = json.dumps({"contextFileName": "GEMINI.md"}, indent=2) + "\n"
        write_text(settings_path, original)

        actions = sweep(source, root)

        assert settings_path.read_text() == original
        assert actions == []

    def test_a_legacy_opencode_permissions_array_becomes_permission(self, source, root):
        """OpenCode 1.4.2 refuses a config that carries "permissions" (Unrecognized key: "permissions"); its key is the "permission" object. The sweep used to do the reverse."""
        config_path = root / "proj10" / "opencode.json"
        write_json(config_path, {"permissions": [], "model": "provider/model"})

        sweep(source, root)

        settings = read_json(config_path)
        assert "permissions" not in settings
        assert settings["permission"] == opencode_permission(POLICY)
        assert settings["model"] == "provider/model"

    def test_the_same_opencode_permission_entries_in_another_order_are_rewritten(self, source, root):
        """OpenCode applies the last matching rule, so order is part of the policy and a reordered object must not compare equal to the rendered one."""
        config_path = root / "proj10b" / "opencode.json"
        rendered = opencode_permission(POLICY)
        reordered = dict(reversed(list(rendered.items())))
        reordered["bash"] = dict(reversed(list(rendered["bash"].items())))
        assert reordered == rendered  # equal as dicts, different as policy
        write_json(config_path, {"permission": reordered})

        actions = sweep(source, root)

        assert any("proj10b" in action for action in actions), actions
        assert json.dumps(read_json(config_path)["permission"]) == json.dumps(rendered)

    def test_an_opencode_jsonc_file_with_a_comment_and_a_permission_object_is_rewritten(self, source, root):
        config_path = root / "proj11" / "opencode.jsonc"
        write_text(config_path, '// generated by hand, please fix\n{\n  "permission": {"bash": "ask"},\n  "model": "provider/model"\n}\n')

        sweep(source, root)

        settings = read_json(config_path)
        assert "permissions" not in settings
        assert settings["permission"] == opencode_permission(POLICY)
        assert settings["model"] == "provider/model"

    def test_invalid_json_is_skipped_without_blocking_other_files(self, source, root):
        broken_path = root / "proj12a" / ".claude" / "settings.json"
        write_text(broken_path, "{ this is not valid json")
        valid_path = root / "proj12b" / ".claude" / "settings.json"
        write_json(valid_path, {"permissions": {"allow": ["Bash(old *)"]}})

        actions = sweep(source, root)

        skips = [action for action in actions if action.startswith("skip ")]
        assert len(skips) == 1
        assert short(broken_path) in skips[0]
        assert broken_path.read_text() == "{ this is not valid json"
        assert f"write {short(valid_path)}" in actions
        assert read_json(valid_path)["permissions"] == claude_project_permissions(POLICY)

    def test_a_dry_run_reports_actions_without_writing(self, source, root):
        settings_path = root / "proj13" / ".claude" / "settings.json"
        original = json.dumps({"permissions": {"allow": ["Bash(old *)"]}}, indent=2) + "\n"
        write_text(settings_path, original)

        actions = sweep(source, root, dry_run=True)

        assert actions == [f"write {short(settings_path)}"]
        assert settings_path.read_text() == original

    def test_a_second_sweep_over_a_rendered_tree_is_a_no_op(self, source, root):
        settings_path = root / "proj14" / ".claude" / "settings.json"
        write_json(settings_path, {"permissions": {"allow": ["Bash(old *)"]}})

        assert sweep(source, root) == [f"write {short(settings_path)}"]
        assert sweep(source, root) == []

    def test_the_runtimes_filter_limits_which_surfaces_are_touched(self, source, root):
        claude_path = root / "proj15" / ".claude" / "settings.json"
        write_json(claude_path, {"permissions": {"allow": ["Bash(old *)"]}})
        codex_path = root / "proj15" / ".codex" / "config.toml"
        original = 'approval_policy = "on-request"\n'
        write_text(codex_path, original)

        actions = sweep(source, root, runtimes={"claude"})

        assert actions == [f"write {short(claude_path)}"]
        assert codex_path.read_text() == original

    def test_a_missing_root_or_a_source_without_a_policy_yields_no_actions(self, source, root, tmp_path):
        assert sweep(source, root / "does-not-exist") == []

        write_json(root / "proj16" / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(old *)"]}})
        empty_source = tmp_path / "empty-source"
        empty_source.mkdir()
        assert sweep(empty_source, root) == []

    def test_candidates_find_all_five_surface_types_in_sorted_order(self, root):
        project = root / "proj17"
        paths = [
            project / ".claude" / "settings.json",
            project / ".codex" / "config.toml",
            project / ".gemini" / "settings.json",
            project / "opencode.json",
            root / "proj17b" / "opencode.jsonc",
        ]
        for path in paths:
            write_text(path, "{}\n")

        found = [path for _, path in candidates(root)]

        assert found == sorted(found)
        for path in paths:
            assert path in found


class TestScopedProject:
    """A project whose projects-root permissions.json carries a projectScope gets native configs for its exact checkout."""

    def _scope(self, source: Path, root: Path) -> tuple[Path, dict]:
        project = root / SCOPED
        (project / ".git").mkdir(parents=True)
        scoped = {
            **POLICY,
            "defaultMode": "default",
            "additionalDirectories": [],
            "runtimeDirectories": [],
            "allow": [f"Read({project}/**)", f"Edit({project}/**)"],
            "projectScope": {"root": str(project), "worktrees": []},
        }
        write_json(source / "projects-root" / SCOPED / "permissions.json", scoped)
        return project, scoped

    def test_it_creates_native_configs_and_preserves_unrelated_bytes(self, source, root):
        project, _ = self._scope(source, root)
        unrelated = root / "other/.claude/settings.json"
        write_json(unrelated, {"permissions": claude_project_permissions(POLICY)})
        before = unrelated.read_bytes()

        actions = sweep(source, root)

        assert len(actions) == 3
        assert unrelated.read_bytes() == before
        claude = read_json(project / ".claude/settings.json")
        assert claude["permissions"]["additionalDirectories"] == []
        assert claude["sandbox"]["enabled"]
        assert not claude["sandbox"]["allowUnsandboxedCommands"]
        assert "Read(**/.env)" in claude["permissions"]["deny"]
        codex = tomllib.loads((project / ".codex/config.toml").read_text())
        assert codex["default_permissions"] == SCOPED
        assert codex["approval_policy"] == "never"
        profile = codex["permissions"][SCOPED]
        assert profile["extends"] == ":read-only"
        assert "workspace_roots" not in profile
        assert profile["filesystem"][str(project)] == "write"
        assert not profile["network"]["enabled"]
        assert read_json(project / ".gemini/settings.json")["tools"]["sandbox"] == "docker"
        assert sweep(source, root) == []

    def test_it_preserves_other_config_keys_and_overrides_an_unsafe_head(self, source, root):
        project, _ = self._scope(source, root)
        config = project / ".codex/config.toml"
        write_text(
            config,
            'default_permissions = "other"\nsandbox_mode = "danger-full-access"\n[mcp_servers.fixture]\ncommand = "fixture"\n',
        )

        sweep(source, root)

        parsed = tomllib.loads(config.read_text())
        assert "sandbox_mode" not in parsed
        assert parsed["mcp_servers"]["fixture"]["command"] == "fixture"
        assert parsed["default_permissions"] == SCOPED

    def test_a_dry_run_does_not_create_outputs(self, source, root):
        project, _ = self._scope(source, root)

        assert len(sweep(source, root, dry_run=True)) == 3

        assert not (project / ".codex").exists()

    def test_it_rejects_shared_grants_and_a_root_that_is_not_the_named_checkout(self, source, root):
        _, scoped = self._scope(source, root)
        scope_file = source / "projects-root" / SCOPED / "permissions.json"
        scoped["additionalDirectories"] = ["~/projects"]
        write_json(scope_file, scoped)
        with pytest.raises(ValueError, match="shared roots"):
            sweep(source, root)

        scoped["additionalDirectories"] = []
        scoped["projectScope"]["root"] = str(root)
        write_json(scope_file, scoped)
        with pytest.raises(ValueError, match="named checkout"):
            sweep(source, root)

    def test_it_rejects_a_symlink_config_without_modifying_the_target(self, source, root):
        project, _ = self._scope(source, root)
        target = root / "fixture-settings.json"
        write_json(target, {})
        settings = project / ".claude/settings.json"
        settings.parent.mkdir()
        settings.symlink_to(target)
        before = target.read_bytes()

        with pytest.raises(ValueError, match="symlink permission target"):
            sweep(source, project)

        assert target.read_bytes() == before

    def test_a_targeted_sweep_does_not_visit_another_project(self, source, root):
        project, _ = self._scope(source, root)
        unrelated = root / "other/.claude/settings.json"
        write_json(unrelated, {"permissions": {"allow": ["fixture"]}})
        before = unrelated.read_bytes()

        sweep(source, project)

        assert unrelated.read_bytes() == before

    def test_it_uses_the_canonical_path_for_a_root_alias(self, source, root):
        project, _ = self._scope(source, root)

        sweep(source, project / "..")

        assert tomllib.loads((project / ".codex/config.toml").read_text())["default_permissions"] == SCOPED

    def test_a_project_permissions_file_without_a_scope_is_never_read_as_one(self, source, root):
        """A projects-root permissions.json that declares no projectScope is another renderer's file; even a malformed one must not stop the sweep."""
        write_text(source / "projects-root" / "plain" / "permissions.json", "{ not json")
        write_json(source / "projects-root" / "other" / "permissions.json", {"schemaVersion": 1})
        settings_path = root / "proj" / ".claude" / "settings.json"
        write_json(settings_path, {"permissions": {"allow": ["Bash(old *)"]}})

        assert sweep(source, root) == [f"write {short(settings_path)}"]


class TestPublicTargets:
    """A public target's permission files are its own: the sweep never rewrites them, and a list it cannot read stops the sweep before any file is touched."""

    STALE = {"permissions": {"allow": ["Bash(old *)"]}}

    def _list(self, source: Path, text: str) -> None:
        write_text(source / "projects-root" / "public-targets.json", text)

    def _project(self, root: Path, name: str) -> Path:
        path = root / name / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.STALE, indent=2) + "\n")
        return path

    @staticmethod
    def _git(*args: str) -> None:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(
            ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *args],
            check=True,
            capture_output=True,
            env=environment,
        )

    def test_a_declared_public_project_is_left_alone_and_the_private_one_is_rewritten(self, source, root):
        self._list(source, '{"targets": ["public-one"]}\n')
        public, private = self._project(root, "public-one"), self._project(root, "zeta-private")
        public_before = public.read_bytes()

        actions = sweep(source, root)

        assert public.read_bytes() == public_before
        assert json.loads(private.read_text())["permissions"] == claude_project_permissions(POLICY)
        assert actions == [
            f"skip {short(public.parent.parent)}: public repository, its permission files are its own",
            f"write {short(private)}",
        ]

    def test_a_linked_worktree_and_a_slug_matching_clone_of_a_public_repository_are_left_alone(self, source, root):
        self._list(source, '["public-one", "Fixture/Renamed"]\n')
        main = root / "public-one"
        self._git("init", "-q", str(main))
        self._git("-C", str(main), "commit", "-q", "--allow-empty", "-m", "base")
        worktree = root / "public-one-feature"
        self._git("-C", str(main), "worktree", "add", "-q", "-b", "feature", str(worktree))
        clone = root / "renamed-clone"
        self._git("init", "-q", str(clone))
        self._git("-C", str(clone), "remote", "add", "origin", "git@github.com:fixture/renamed.git")
        paths = []
        for project in (main, worktree, clone):
            settings = project / ".claude" / "settings.json"
            settings.parent.mkdir(exist_ok=True)
            settings.write_text(json.dumps(self.STALE, indent=2) + "\n")
            paths.append(settings)
        before = [path.read_bytes() for path in paths]
        private = self._project(root, "zeta-private")

        sweep(source, root)

        assert [path.read_bytes() for path in paths] == before
        assert json.loads(private.read_text())["permissions"] == claude_project_permissions(POLICY)

    def test_a_missing_list_declares_nothing_and_every_project_is_rewritten(self, source, root):
        first, second = self._project(root, "public-one"), self._project(root, "zeta-private")

        sweep(source, root)

        for path in (first, second):
            assert json.loads(path.read_text())["permissions"] == claude_project_permissions(POLICY)

    @pytest.mark.parametrize("broken", ["{not json", '"public-one"', '{"targets": "public-one"}', "[1]"])
    def test_an_unreadable_list_refuses_the_sweep_and_rewrites_nothing(self, source, root, broken):
        public, private = self._project(root, "public-one"), self._project(root, "zeta-private")
        before = [public.read_bytes(), private.read_bytes()]
        self._list(source, broken)

        for dry_run in (False, True):
            with pytest.raises(RuntimeError, match=r"refusing.*public-targets\.json.*no permission file was rewritten"):
                sweep(source, root, dry_run=dry_run)

        assert [public.read_bytes(), private.read_bytes()] == before


class TestProjectsDirectory:
    """The default root the sweep walks is $LLM_ROOT_PROJECTS_DIR when set, then stratarc.toml, then ~/projects, read when called."""

    def test_it_is_read_from_the_environment(self, source, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "elsewhere"))
        assert projects_dir(source) == tmp_path / "elsewhere"

    def test_it_defaults_to_projects_under_the_stratarc_home(self, source, stratarc_home, monkeypatch):
        monkeypatch.delenv("LLM_ROOT_PROJECTS_DIR", raising=False)
        assert projects_dir(source) == stratarc_home / "projects"

    def test_a_sweep_with_no_root_walks_the_projects_directory(self, source, tmp_path, monkeypatch):
        walked = tmp_path / "walked"
        monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(walked))
        settings_path = walked / "proj" / ".claude" / "settings.json"
        write_json(settings_path, {"permissions": {"allow": ["Bash(old *)"]}})

        assert sweep(source) == [f"write {short(settings_path)}"]


class TestShort:
    def test_the_home_prefix_becomes_a_tilde(self, stratarc_home):
        assert short(stratarc_home / "projects" / "demo") == "~/projects/demo"


class TestRefuseIfStale:
    """The sweep refuses to run when its source policy predates what is deployed."""

    @pytest.fixture
    def target(self, tmp_path):
        return tmp_path / "deployed"

    def _write_policy(self, source: Path, version: int) -> None:
        write_json(source / "permissions.json", {**POLICY, "policyVersion": version})

    def test_no_stamp_never_refuses(self, source, target):
        self._write_policy(source, 0)
        assert refuse_if_stale(source, target) is None

    def test_an_older_source_is_refused(self, source, target):
        deploy_guard.write_stamp(target, 5, Path("/checkouts/newer"))
        self._write_policy(source, 2)

        message = refuse_if_stale(source, target)

        assert message is not None
        assert "version 5" in message
        assert "version 2" in message

    def test_an_equal_or_newer_source_is_allowed(self, source, target):
        deploy_guard.write_stamp(target, 3, source)
        self._write_policy(source, 3)
        assert refuse_if_stale(source, target) is None
        self._write_policy(source, 4)
        assert refuse_if_stale(source, target) is None

    def test_the_default_target_is_the_claude_directory_under_the_home(self, source, stratarc_home):
        deploy_guard.write_stamp(stratarc_home / ".claude", 7, Path("/checkouts/newer"))
        self._write_policy(source, 1)
        assert "version 7" in refuse_if_stale(source)


class TestMain:
    def test_a_check_run_reports_and_exits_one_without_writing(self, source, root, capsys):
        settings_path = root / "proj" / ".claude" / "settings.json"
        original = json.dumps({"permissions": {"allow": ["Bash(old *)"]}}, indent=2) + "\n"
        write_text(settings_path, original)

        code = project_permissions.main(["--root", str(source), "--projects-root", str(root), "--check"])

        assert code == 1
        assert f"write {short(settings_path)}" in capsys.readouterr().out
        assert settings_path.read_text() == original

    def test_a_run_rewrites_then_reports_current(self, source, root, capsys):
        settings_path = root / "proj" / ".claude" / "settings.json"
        write_json(settings_path, {"permissions": {"allow": ["Bash(old *)"]}})
        arguments = ["--root", str(source), "--projects-root", str(root)]

        assert project_permissions.main(arguments) == 0
        assert project_permissions.main(arguments) == 0

        assert "sync-permissions: current" in capsys.readouterr().out
        assert read_json(settings_path)["permissions"] == claude_project_permissions(POLICY)

    def test_the_source_root_comes_from_the_environment_when_no_flag_is_given(self, source, root, monkeypatch, capsys):
        monkeypatch.setenv("STRATARC_SOURCE", str(source))
        monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(root))
        write_json(root / "proj" / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(old *)"]}})

        assert project_permissions.main(["--check"]) == 1

    def test_a_stale_source_exits_two_and_writes_nothing(self, source, root, stratarc_home, capsys):
        deploy_guard.write_stamp(stratarc_home / ".claude", 9, Path("/checkouts/newer"))
        settings_path = root / "proj" / ".claude" / "settings.json"
        original = json.dumps({"permissions": {"allow": ["Bash(old *)"]}}, indent=2) + "\n"
        write_text(settings_path, original)
        write_json(source / "permissions.json", {**POLICY, "policyVersion": 1})

        code = project_permissions.main(["--root", str(source), "--projects-root", str(root)])

        assert code == 2
        assert "refusing to write an older policy" in capsys.readouterr().err
        assert settings_path.read_text() == original
