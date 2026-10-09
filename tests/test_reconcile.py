"""Tests for generated control-plane inventory and project snapshots."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib.resources import as_file
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentrc import reconcile
from agentrc.control_plane import ControlPlane
from agentrc.resources import data_dir

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "reconcile"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Where the per-project snapshot lands, relative to the checkout, under the default engine name.
SNAPSHOT = Path("agentrc-control-plane.md")
SNAPSHOT_DIR = Path(".docs")

IDENTITY = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false")


def git(*args: str) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", *IDENTITY, *args], check=True, capture_output=True, env=environment)


class Env:
    """The source root, the projects root and the held set one test points the engine at."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        base = tmp_path.resolve()
        self.root = base / "source-root"
        self.projects = base / "projects"
        self.point(self.root, self.projects)

    @property
    def control_plane(self) -> Path:
        return self.root / "control-plane.md"

    @property
    def text(self) -> str:
        return self.control_plane.read_text(encoding="utf-8")

    def point(self, root: Path, projects: Path | None = None) -> None:
        self.monkeypatch.setenv("AGENTRC_SOURCE", str(root))
        if projects is not None:
            self.monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(projects))

    def hold(self, value: str | None) -> None:
        if value is None:
            self.monkeypatch.delenv("LLM_ROOT_HELD_PROJECTS", raising=False)
        else:
            self.monkeypatch.setenv("LLM_ROOT_HELD_PROJECTS", value)

    def seed(self, name: str) -> Path:
        shutil.copytree(FIXTURES / name, self.root)
        return self.root

    def minimal(self, tiers: str = '{"global": [], "common": [], "project": []}\n') -> Path:
        """A source root with an instruction file, a rules directory and a tier manifest, nothing else."""
        (self.root / "rules").mkdir(parents=True)
        (self.root / "rules" / "tiers.json").write_text(tiers, encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        return self.root


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agentrc_home: Path) -> Env:
    for name in ("LLM_ROOT_HELD_PROJECTS", "LLM_ROOT_PROJECTS_DIR", "AGENTRC_SOURCE"):
        monkeypatch.delenv(name, raising=False)
    for key in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(key, raising=False)
    return Env(tmp_path, monkeypatch)


def origin_checkout(path: Path, slug: str, remote: str | None = None) -> None:
    git("init", "-q", str(path))
    git("-C", str(path), "remote", "add", "origin", remote or f"https://github.com/{slug}.git")


class TestProjectsDirectory:
    def test_it_is_read_from_the_environment_when_called(self, env, tmp_path):
        env.monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "elsewhere"))
        assert reconcile.projects_dir() == tmp_path / "elsewhere"

    def test_it_defaults_to_projects_under_the_agentrc_home(self, env, agentrc_home):
        env.monkeypatch.delenv("LLM_ROOT_PROJECTS_DIR")
        assert reconcile.projects_dir() == agentrc_home / "projects"

    def test_agentrc_toml_names_the_directory_that_holds_the_status_directories(self, env, tmp_path):
        env.root.mkdir(parents=True)
        (env.root / "agentrc.toml").write_text(f'projects_root = "{tmp_path / "configured"}"\n', encoding="utf-8")
        env.monkeypatch.delenv("LLM_ROOT_PROJECTS_DIR")
        assert reconcile.projects_dir() == tmp_path / "configured"


class TestSourceRootResolution:
    """The source root is --root, then AGENTRC_SOURCE, then agentrc.toml discovery from the current directory, then the current directory."""

    def _source(self, base: Path, name: str = "fixture-source") -> Path:
        root = base.resolve() / name
        (root / "rules").mkdir(parents=True)
        (root / "rules" / "tiers.json").write_text('{"global": [], "common": [], "project": []}\n', encoding="utf-8")
        (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        return root

    def _run(self, tmp_path: Path, env: Env, *args: str, **extra: str) -> subprocess.CompletedProcess:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key != "AGENTRC_SOURCE" and not key.startswith("GIT_")
        }
        environment.update(PYTHONPATH=str(REPO_ROOT), **extra)
        return subprocess.run(
            [sys.executable, "-m", "agentrc.reconcile", *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=environment,
        )

    def test_the_environment_names_the_root_and_the_control_plane(self, env, tmp_path):
        source = self._source(tmp_path)
        env.point(source)
        assert reconcile.root() == source
        assert reconcile.control_plane_path() == source / "control-plane.md"

    def test_with_no_override_the_nearest_agentrc_toml_names_the_root(self, env, tmp_path):
        source = self._source(tmp_path)
        (source / "agentrc.toml").write_text("", encoding="utf-8")
        nested = source / "rules"
        env.monkeypatch.delenv("AGENTRC_SOURCE")
        env.monkeypatch.chdir(nested)
        assert reconcile.root() == source

    def test_configure_root_beats_the_environment(self, env, tmp_path):
        flagged, ambient = self._source(tmp_path, "flagged"), self._source(tmp_path, "ambient")
        env.point(ambient)
        env.monkeypatch.setattr(reconcile, "_root_flag", None)
        reconcile.configure_root(flagged)
        assert reconcile.root() == flagged

    def test_main_restores_the_root_it_replaced(self, env, tmp_path):
        flagged, ambient = self._source(tmp_path, "flagged"), self._source(tmp_path, "ambient")
        env.point(ambient)
        assert reconcile.main(["--root", str(flagged), "--check"]) == 1
        assert reconcile.root() == ambient

    def test_a_run_with_the_root_flag_reconciles_that_tree_and_nothing_else(self, env, tmp_path):
        source = self._source(tmp_path)
        ambient = self._source(tmp_path, "ambient")
        result = self._run(tmp_path, env, "--root", str(source), "--check", AGENTRC_SOURCE=str(ambient))
        assert result.returncode == 1, result.stderr
        assert f"stale {source / 'control-plane.md'}" in result.stdout
        assert not (source / "control-plane.md").exists()
        assert str(ambient) not in result.stdout

    def test_a_run_under_agentrc_source_reconciles_that_tree(self, env, tmp_path):
        source = self._source(tmp_path)
        result = self._run(tmp_path, env, "--check", AGENTRC_SOURCE=str(source))
        assert result.returncode == 1, result.stderr
        assert f"stale {source / 'control-plane.md'}" in result.stdout

    def test_a_run_never_imports_the_source_root_as_code(self, env, tmp_path):
        """A decoy engine package inside the source root is not on the import path of a run."""
        source = self._source(tmp_path)
        (source / "agentrc").mkdir()
        (source / "agentrc" / "control_plane.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
        result = self._run(tmp_path, env, "--root", str(source), "--check")
        assert result.returncode == 1, result.stderr


class TestManifest:
    def test_it_discovers_a_safe_github_origin_from_a_checkout(self, env, tmp_path):
        checkout = tmp_path / "demo"
        origin_checkout(checkout, "fixture/demo")
        plane = SimpleNamespace(manifest={"demo": {}})
        with patch.object(reconcile, "checkout_records", return_value={"demo": ("active", "high", checkout)}):
            manifest = reconcile.project_manifest(plane, {})
        assert manifest["demo"]["origin"] == "fixture/demo"

    def test_it_preserves_an_existing_origin(self, env):
        plane = SimpleNamespace(manifest={"demo": {"origin": "fixture/demo"}})
        with (
            patch.object(reconcile, "checkout_records", return_value={"demo": ("active", "high", Path("/demo"))}),
            patch.object(reconcile, "checkout_origin", return_value="fixture/different-repository"),
        ):
            manifest = reconcile.project_manifest(plane, {})
        assert manifest["demo"]["origin"] == "fixture/demo"

    def test_checkout_origin_ignores_an_inherited_git_dir(self, env, tmp_path, monkeypatch):
        """A git hook can export GIT_DIR from the commit that triggered it. `git -C <checkout> config ...` silently ignores -C when GIT_DIR is set, so an inherited GIT_DIR makes every project's origin resolve to the hook's own repository."""
        hook_repo = tmp_path / "hook-repo"
        origin_checkout(hook_repo, "fixture/source")
        project = tmp_path / "other"
        origin_checkout(project, "fixture/other")
        monkeypatch.setenv("GIT_DIR", str(hook_repo / ".git"))
        assert reconcile.checkout_origin(project) == "fixture/other"


class TestHookDiscovery:
    def test_worktree_hooks_are_found_and_event_names_are_kebab_cased(self, env):
        hooks = env.root / "hooks"
        hooks.mkdir(parents=True)
        for name in ("worktree-create.sh", "worktree-remove.sh", "worktree-validate.sh"):
            (hooks / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (hooks / "hooks.json").write_text("{}\n", encoding="utf-8")
        (hooks / "claude-worktree-hooks.json").write_text(
            '{"WorktreeCreate":[{"hooks":[{"command":"$HOME/.claude/hooks/worktree-create.sh"}]}]}\n',
            encoding="utf-8",
        )
        events = reconcile.hook_events()
        assert events["WorktreeCreate"] == [("hook:worktree-create", hooks / "worktree-create.sh")]
        assert reconcile.kebab("WorktreeCreate") == "worktree-create"

    def test_the_packaged_agent_graph_manifest_is_read(self, env):
        hooks = env.root / "hooks"
        hooks.mkdir(parents=True)
        for name in ("agent-graph-session-start.sh", "agent-graph-pre-edit.sh"):
            (hooks / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (hooks / "hooks.json").write_text("{}\n", encoding="utf-8")
        with as_file(data_dir("hooks/claude-agent-graph-hooks.json")) as shipped:
            (hooks / "claude-agent-graph-hooks.json").write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")
        events = reconcile.hook_events()
        assert events["SessionStart"] == [("hook:agent-graph-session-start", hooks / "agent-graph-session-start.sh")]
        assert events["PreToolUse"] == [("hook:agent-graph-pre-edit", hooks / "agent-graph-pre-edit.sh")]


class TestProjectOptions:
    def test_generated_python_artifacts_are_ignored(self, env):
        hooks = env.root / "projects-root" / "demo" / "hooks"
        cache = hooks / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "hook.cpython-314.pyc").write_bytes(b"compiled")
        manifest = hooks / "hooks.json"
        manifest.write_text("{}\n", encoding="utf-8")
        options = dict(reconcile.project_options(reconcile.source_projects()))
        assert options["project:hooks"] == manifest

    def test_a_project_skills_directory_is_an_option(self, env):
        """projects-root/<name>/skills/ gets a project:skills row, so a project's own skills can be opted in like its agents or hooks."""
        skill = env.root / "projects-root" / "demo" / "skills" / "local-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# local-skill\n", encoding="utf-8")
        options = dict(reconcile.project_options(reconcile.source_projects()))
        assert options["project:skills"] == skill


class TestRuntimes:
    def test_a_private_adapter_module_is_not_a_runtime(self, env, tmp_path):
        """A leading underscore marks a shared helper, never a runtime; a test module is not one either."""
        adapters = tmp_path / "adapters"
        adapters.mkdir()
        declaration = 'RUNTIME = {"name": "claude", "target": ".claude", "hook_registry": None, "hook_events": []}\n'
        (adapters / "claude.py").write_text(declaration, encoding="utf-8")
        for name in ("__init__", "_common", "_inventory", "_components", "claude.test"):
            (adapters / f"{name}.py").write_text("\n", encoding="utf-8")
        assert [option for option, _path in reconcile.runtime_options(adapters)] == ["runtime:claude"]

    def test_the_packaged_adapters_are_the_runtime_rows_and_link_to_agentrc_toml(self, env):
        options = reconcile.runtime_options()
        names = [option for option, _path in options]
        assert names == sorted(names)
        assert {"runtime:claude", "runtime:codex", "runtime:gemini", "runtime:cursor", "runtime:opencode"} <= set(names)
        assert {path for _option, path in options} == {env.root / "agentrc.toml"}
        assert not any(name.startswith("runtime:_") for name in names)


class TestReconcile:
    def test_check_reports_drift_without_writing_then_converges(self, env):
        env.seed("source-root")
        checkout = env.projects / "active" / "demo"
        (checkout / ".git").mkdir(parents=True)
        env.control_plane.write_text("# control-plane\n", encoding="utf-8")

        changed, notes = reconcile.reconcile(check=True)
        assert changed
        assert str(env.control_plane) in notes
        assert env.text == "# control-plane\n"

        changed, notes = reconcile.reconcile()
        assert changed
        assert "| [rule:a-global-rule](rules/a-global-rule.md) | global | x | x |" in env.text
        assert (checkout / SNAPSHOT_DIR / SNAPSHOT).is_file()
        assert not (checkout / ".claude" / SNAPSHOT).exists()
        assert not (checkout / SNAPSHOT).exists()

        rendered, render_notes = reconcile.render(ControlPlane.load(env.control_plane), reconcile.load_rule_tiers())
        assert env.text == rendered
        assert render_notes == []
        changed, notes = reconcile.reconcile(check=True)
        assert not changed, notes
        assert notes == []

    def test_check_reports_drift_when_a_skill_source_is_deleted(self, env):
        """A row naming a skill whose source file is gone is drift, not a silent pass. render() builds every row from a live scan of skills/, never by carrying forward the previous file's rows, so a vanished source cannot survive a render; --check catches the mismatch and a live run self-heals it."""
        env.minimal()
        skill = env.root / "skills" / "disposable" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: disposable\n---\n", encoding="utf-8")

        reconcile.reconcile()
        assert "skill:disposable" in env.text

        skill.unlink()
        skill.parent.rmdir()

        changed, notes = reconcile.reconcile(check=True)
        assert changed, notes
        assert "skill:disposable" in env.text, "check must not write; the stale row stays on disk"

        reconcile.reconcile()
        assert "skill:disposable" not in env.text

    def test_a_reconcile_never_writes_into_a_vault(self, env):
        """Nothing is written into an unmanaged directory by default."""
        env.minimal()
        checkout = env.projects / "active" / "demo"
        (checkout / ".git").mkdir(parents=True)
        vault = env.projects / "active" / "notes-vault"
        (vault / ".obsidian").mkdir(parents=True)

        reconcile.reconcile()

        assert (checkout / SNAPSHOT_DIR / SNAPSHOT).is_file()
        assert not (vault / SNAPSHOT_DIR / SNAPSHOT).exists()
        assert not (vault / ".claude").exists()
        assert "| notes-vault | unmanaged |" in env.text


class TestSnapshots:
    def test_a_stale_dot_claude_copy_is_removed(self, env, tmp_path):
        checkout = tmp_path / "demo"
        stale = checkout / ".claude" / SNAPSHOT
        stale.parent.mkdir(parents=True)
        stale.write_text("stale\n", encoding="utf-8")

        changes = reconcile.refresh_snapshots({"demo": checkout}, "# control-plane\n", check=False)

        assert not stale.exists()
        assert (checkout / SNAPSHOT_DIR / SNAPSHOT).is_file()
        assert any("remove" in change and str(stale) in change for change in changes)

    def test_a_root_copy_moves_into_dot_docs(self, env, tmp_path):
        checkout = tmp_path / "demo"
        stale = checkout / SNAPSHOT
        checkout.mkdir()
        stale.write_text("stale\n", encoding="utf-8")

        changes = reconcile.refresh_snapshots({"demo": checkout}, "# control-plane\n", check=False)

        assert not stale.exists()
        assert (checkout / SNAPSHOT_DIR / SNAPSHOT).is_file()
        assert any("remove" in change and str(stale) in change for change in changes)

    def test_check_reports_the_stale_copy_without_removing_it(self, env, tmp_path):
        checkout = tmp_path / "demo"
        stale = checkout / ".claude" / SNAPSHOT
        stale.parent.mkdir(parents=True)
        stale.write_text("stale\n", encoding="utf-8")

        changes = reconcile.refresh_snapshots({"demo": checkout}, "# control-plane\n", check=True)

        assert stale.is_file()
        assert not (checkout / SNAPSHOT_DIR / SNAPSHOT).exists()
        assert any("remove" in change and str(stale) in change for change in changes)

    def test_the_header_names_the_source_root_and_never_a_machine_path(self, env, tmp_path):
        checkout = tmp_path / "demo"
        checkout.mkdir()

        reconcile.refresh_snapshots({"demo": checkout}, "# control-plane\n", check=False)

        text = (checkout / SNAPSHOT_DIR / SNAPSHOT).read_text(encoding="utf-8")
        assert text.startswith("# agentrc-control-plane\n")
        assert "The canonical source is the source root's `control-plane.md`." in text
        assert str(Path.home()) not in text
        assert "~/" not in text
        assert text.endswith("# control-plane\n")

    def test_the_engine_name_names_the_snapshot_and_its_header(self, env, tmp_path):
        env.root.mkdir(parents=True)
        (env.root / "agentrc.toml").write_text('name = "acme"\n', encoding="utf-8")
        checkout = tmp_path / "demo"
        checkout.mkdir()
        stale = checkout / "agentrc-control-plane.md"
        stale.write_text("not ours\n", encoding="utf-8")

        reconcile.refresh_snapshots({"demo": checkout}, "# control-plane\n", check=False)

        text = (checkout / SNAPSHOT_DIR / "acme-control-plane.md").read_text(encoding="utf-8")
        assert text.startswith("# acme-control-plane\n")
        assert not (checkout / SNAPSHOT_DIR / SNAPSHOT).exists()
        assert stale.read_text(encoding="utf-8") == "not ours\n"


class TestPublicTargets:
    def _fixture(self, env: Env, targets: str = '{"targets": ["public-one"]}\n') -> None:
        env.minimal()
        (env.root / "projects-root").mkdir()
        (env.root / "projects-root" / "public-targets.json").write_text(targets, encoding="utf-8")
        for name in ("public-one", "demo"):
            origin_checkout(env.projects / "active" / name, f"fixture/{name}")

    def test_a_public_target_has_no_row_no_column_no_snapshot_and_no_note(self, env):
        """A project named in projects-root/public-targets.json is a public repository that receives nothing from this source root. The project beside it is unchanged."""
        self._fixture(env)

        records = reconcile.checkout_records()
        found = reconcile.active_checkout_paths()
        _changed, notes = reconcile.reconcile()

        assert sorted(records) == ["demo"]
        assert sorted(found) == ["demo"]
        assert "public-one" not in env.text
        assert "| demo | active | normal | base | fixture/demo |" in env.text
        assert "| option | global | demo |" in env.text
        assert not any("public-one" in note for note in notes), notes
        assert (env.projects / "active" / "demo" / SNAPSHOT_DIR / SNAPSHOT).is_file()
        public = env.projects / "active" / "public-one"
        assert sorted(path.name for path in public.iterdir()) == [".git"]

    @pytest.mark.parametrize("held", [None, "present", "demo"])
    def test_a_committed_row_for_a_public_target_is_dropped(self, env, held):
        """A row that landed before the project was declared public, or that a scoped machine would otherwise carry forward, does not survive."""
        self._fixture(env)
        env.control_plane.write_text(
            "# control-plane\n\n## projects\n\n| project | status | tier | template | origin |\n|---|---|---|---|---|\n"
            "| public-one | active | normal | base | fixture/public-one |\n",
            encoding="utf-8",
        )
        env.hold(held)
        reconcile.reconcile()
        assert "public-one" not in env.text

    def test_a_public_target_in_a_tier_directory_or_another_status_is_skipped_too(self, env):
        self._fixture(env, '{"targets": ["public-one", "archived-public", "tiered-public"]}\n')
        for relative in ("active/normal-trust/tiered-public", "archived/archived-public", "active/normal-trust/kept"):
            git("init", "-q", str(env.projects / relative))

        records = reconcile.checkout_records()

        assert sorted(records) == ["demo", "kept"]

    def test_a_linked_worktree_of_a_public_target_is_skipped_under_its_own_name(self, env):
        """The worktree's directory name is not in the list; its main worktree's is. Judging only the toplevel's own name would register it and write a snapshot into the published tree."""
        self._fixture(env)
        main = env.projects / "active" / "public-one"
        git("-C", str(main), "commit", "-q", "--allow-empty", "-m", "base")
        worktree = env.projects / "active" / "public-one-feature"
        git("-C", str(main), "worktree", "add", "-q", "-b", "feature", str(worktree))
        assert (worktree / ".git").is_file()

        records = reconcile.checkout_records()
        found = reconcile.active_checkout_paths()
        reconcile.reconcile()

        assert sorted(records) == ["demo"]
        assert sorted(found) == ["demo"]
        assert "public-one-feature" not in env.text
        assert not (worktree / SNAPSHOT_DIR).exists()

    def test_a_checkout_whose_origin_slug_is_listed_is_skipped_whatever_it_is_called(self, env):
        self._fixture(env, '{"targets": ["public-one", "Fixture/Renamed"]}\n')
        clone = env.projects / "active" / "renamed-clone"
        origin_checkout(clone, "fixture/renamed", "git@github.com:fixture/renamed.git")

        records = reconcile.checkout_records()
        found = reconcile.active_checkout_paths()
        reconcile.reconcile()

        assert sorted(records) == ["demo"]
        assert sorted(found) == ["demo"]
        assert "renamed-clone" not in env.text
        assert sorted(path.name for path in clone.iterdir()) == [".git"]

    def test_a_missing_file_declares_nothing(self, env):
        self._fixture(env)
        (env.root / "projects-root" / "public-targets.json").unlink()

        assert sorted(reconcile.checkout_records()) == ["demo", "public-one"]

    def test_a_bare_list_reads_the_same_as_the_targets_object(self, env):
        self._fixture(env, '["public-one"]\n')

        assert reconcile.public_targets() == frozenset({"public-one"})
        assert sorted(reconcile.checkout_records()) == ["demo"]

    @pytest.mark.parametrize("broken", ["{not json", '"public-one"', '{"targets": "public-one"}', '{"targets": [1]}', "[1]"])
    def test_an_unreadable_file_refuses_and_writes_nothing(self, env, broken, capsys):
        """Reading a broken list as empty would register the public checkout and write a snapshot into it, the one thing the file prevents."""
        self._fixture(env)
        env.control_plane.write_text("# control-plane\n", encoding="utf-8")
        (env.root / "projects-root" / "public-targets.json").write_text(broken, encoding="utf-8")

        code = reconcile.main([])

        assert code == 2
        assert "public-targets.json" in capsys.readouterr().err
        assert env.text == "# control-plane\n"
        assert not (env.projects / "active" / "public-one" / SNAPSHOT_DIR / SNAPSHOT).exists()
        assert not (env.projects / "active" / "demo" / SNAPSHOT_DIR / SNAPSHOT).exists()


class TestWorktreeDiscovery:
    def _primary(self, env: Env) -> Path:
        primary = env.projects / "active" / "primary-source"
        git("init", "-q", str(primary))
        git("-C", str(primary), "remote", "add", "origin", "https://github.com/fixture/primary-source.git")
        git("-C", str(primary), "commit", "-q", "--allow-empty", "-m", "fixture")
        return primary

    def test_discovery_from_a_linked_worktree_excludes_the_primary_checkout(self, env, tmp_path):
        """A commit made from a linked worktree runs this module with the source root set to the worktree, not the primary checkout. Discovery must still exclude the primary checkout, and every other project's origin must remain its own."""
        primary = self._primary(env)
        linked = tmp_path / "_worktrees" / "primary-source-lane-b"
        git("-C", str(primary), "worktree", "add", "-q", str(linked), "-b", "fixture-lane")
        other = env.projects / "active" / "other-project"
        origin_checkout(other, "fixture/other-project")
        env.point(linked.resolve())

        found = reconcile.active_checkout_paths()
        manifest = reconcile.project_manifest(SimpleNamespace(manifest={}), {})

        assert "primary-source" not in found
        assert "primary-source" not in manifest
        assert manifest["other-project"]["origin"] == "fixture/other-project"

    def _linked(self, env: Env, tmp_path: Path) -> tuple[Path, Path]:
        """A primary checkout under active/, a linked worktree of it outside active/, and one other active project carrying a snapshot rendered from the primary checkout's merged inventory."""
        primary = self._primary(env)
        linked = (tmp_path / "_worktrees" / "primary-source" / "lane").resolve()
        git("-C", str(primary), "worktree", "add", "-q", str(linked), "-b", "lane")
        (linked / "rules").mkdir()
        (linked / "rules" / "branch-only-rule.md").write_text("# branch-only-rule\n", encoding="utf-8")
        (linked / "agents").mkdir()
        (linked / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        demo = env.projects / "active" / "demo"
        git("init", "-q", str(demo))
        (demo / SNAPSHOT_DIR).mkdir()
        (demo / SNAPSHOT_DIR / SNAPSHOT).write_text("live snapshot from main\n", encoding="utf-8")
        env.point(linked)
        return linked, demo

    def test_a_linked_worktree_writes_only_its_own_control_plane(self, env, tmp_path):
        """A branch's unmerged inventory must never reach a live project snapshot."""
        linked, demo = self._linked(env, tmp_path)

        changed, notes = reconcile.reconcile()

        assert changed
        assert "rule:branch-only-rule" in (linked / "control-plane.md").read_text(encoding="utf-8")
        assert (demo / SNAPSHOT_DIR / SNAPSHOT).read_text(encoding="utf-8") == "live snapshot from main\n"
        assert not any(str(demo) in note for note in notes), notes

    def test_a_linked_worktree_check_ignores_live_snapshots(self, env, tmp_path):
        """--check from a linked worktree compares only that worktree's control-plane.md: a live snapshot rendered from main differs from any inventory-changing branch by construction."""
        _linked, demo = self._linked(env, tmp_path)

        reconcile.reconcile()
        (demo / SNAPSHOT_DIR / SNAPSHOT).write_text("live snapshot from main\n", encoding="utf-8")
        changed, notes = reconcile.reconcile(check=True)

        assert not changed, notes
        assert notes == []

    def test_a_linked_worktree_run_says_what_it_left_alone(self, env, tmp_path, capsys):
        self._linked(env, tmp_path)

        code = reconcile.main([])

        assert code == 0
        assert "project snapshots are left to the primary checkout" in capsys.readouterr().out

    def test_the_primary_checkout_still_refreshes_live_snapshots(self, env, tmp_path):
        """The counterexample: run from the primary checkout itself, the reconciler keeps refreshing every active project snapshot."""
        _linked, demo = self._linked(env, tmp_path)
        primary = env.projects / "active" / "primary-source"
        (primary / "rules").mkdir()
        (primary / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        env.point(primary.resolve())

        reconcile.reconcile()

        assert (demo / SNAPSHOT_DIR / SNAPSHOT).read_text(encoding="utf-8") != "live snapshot from main\n"


class TestVaults:
    def test_a_non_git_vault_is_registered_as_unmanaged(self, env):
        """A fixture active root with one git checkout and one vault; the manifest carries both, the vault as unmanaged."""
        git("init", "-q", str(env.projects / "active" / "demo"))
        (env.projects / "active" / "notes-vault" / ".obsidian").mkdir(parents=True)

        manifest = reconcile.project_manifest(SimpleNamespace(manifest={}), {})

        assert manifest["demo"]["status"] == "active"
        assert manifest["notes-vault"]["status"] == "unmanaged"
        assert manifest["notes-vault"]["origin"] == "-"

    def test_a_tier_directory_with_git_children_is_not_treated_as_a_vault(self, env):
        """A legacy tier directory that still holds real git checkouts keeps being scanned as a tier, even if it also carries a README."""
        tier = env.projects / "active" / "normal-trust"
        tier.mkdir(parents=True)
        (tier / "README.md").write_text("# tier\n", encoding="utf-8")
        git("init", "-q", str(tier / "demo"))

        records = reconcile.checkout_records()

        assert records["demo"][0] == "active"
        assert "normal-trust" not in records

    def test_render_columns_gate_unmanaged_projects_on_a_projects_root_entry(self, env):
        """Enrollment and output remain opt-in; an unmanaged project gets a control-plane column only once a projects-root/ entry for it exists."""
        vault = env.projects / "active" / "notes-vault"
        (vault / ".obsidian").mkdir(parents=True)
        manifest = {"notes-vault": {"status": "unmanaged", "tier": "-"}}

        without_entry = reconcile.render_columns(manifest, {})
        with_entry = reconcile.render_columns(manifest, {"notes-vault": vault})

        assert without_entry == ["global"]
        assert "notes-vault" in with_entry


class TestRuleTiers:
    """rules/tiers.json drives the rules table's default opt-ins, its re-set of a cleared global cell, and its tier column."""

    def _seed(self, env: Env) -> Path:
        env.seed("source-root")
        checkout = env.projects / "active" / "demo"
        (checkout / ".git").mkdir(parents=True)
        return checkout

    def test_a_new_rule_gets_default_cells_by_tier(self, env):
        self._seed(env)
        env.control_plane.write_text("# control-plane\n", encoding="utf-8")

        reconcile.reconcile()

        assert "| [rule:a-global-rule](rules/a-global-rule.md) | global | x | x |" in env.text
        assert "| [rule:a-common-rule](rules/a-common-rule.md) | common | x | x |" in env.text
        assert "| [rule:a-project-rule](rules/a-project-rule.md) | project |  |  |" in env.text

    def test_a_cleared_global_cell_is_reset_and_reported(self, env):
        self._seed(env)
        env.control_plane.write_text(
            "\n".join(
                [
                    "# control-plane",
                    "",
                    "## rules",
                    "",
                    "| option | tier | global | demo |",
                    "|---|---|---|---|",
                    "| [rule:a-global-rule](rules/a-global-rule.md) | global | | x |",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        changed, notes = reconcile.reconcile()

        assert changed
        assert any("re-set cleared global rule:a-global-rule in global" in note for note in notes), notes
        assert "| [rule:a-global-rule](rules/a-global-rule.md) | global | x | x |" in env.text

    def test_the_first_reconciliation_preserves_every_existing_x_including_project_tier(self, env):
        """On the first reconciliation after the manifest lands every existing x is preserved: an old-format document (no tier column, every rule opted in everywhere) keeps every x, including for a project-tier rule whose brand-new default would otherwise be opted out."""
        self._seed(env)
        env.control_plane.write_text(
            "\n".join(
                [
                    "# control-plane",
                    "",
                    "## rules",
                    "",
                    "| option | global | demo |",
                    "|---|---|---|",
                    "| [rule:a-global-rule](rules/a-global-rule.md) | x | x |",
                    "| [rule:a-common-rule](rules/a-common-rule.md) | x | x |",
                    "| [rule:a-project-rule](rules/a-project-rule.md) | x | x |",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        reconcile.reconcile()

        assert "| [rule:a-global-rule](rules/a-global-rule.md) | global | x | x |" in env.text
        assert "| [rule:a-common-rule](rules/a-common-rule.md) | common | x | x |" in env.text
        assert "| [rule:a-project-rule](rules/a-project-rule.md) | project | x | x |" in env.text


class TestComponents:
    def _seed(self, env: Env) -> None:
        env.seed("source-root")
        (env.projects / "active" / "demo" / ".git").mkdir(parents=True)

    def test_declared_components_get_rows_that_default_off(self, env):
        """Each wanted MCP server and plugin in components.json becomes a row, off in the global column and in every project until it is selected. An unwanted entry and a hosted connector get no row, and a surviving x is kept."""
        self._seed(env)
        (env.root / "components.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "mcp_servers": [
                        {"name": "alpha", "runtimes": ["claude"], "owner": "fixture", "wanted": True, "command": "npx"},
                        {"name": "chosen", "runtimes": ["claude"], "owner": "fixture", "wanted": True, "command": "npx"},
                        {"name": "gone", "runtimes": ["claude"], "owner": "fixture", "wanted": False, "command": "x"},
                        {"name": "account", "runtimes": ["claude"], "owner": "fixture", "wanted": True, "hosted": True},
                    ],
                    "plugins": [
                        {"name": "helpers", "runtimes": ["claude"], "owner": "fixture", "wanted": True, "marketplace": "fixture-marketplace"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        env.control_plane.write_text(
            "# control-plane\n\n## mcp-servers\n\n| option | global | demo |\n|---|---|---|\n| [mcp:chosen](components.json) | x |  |\n",
            encoding="utf-8",
        )

        reconcile.reconcile()

        assert "## mcp-servers" in env.text
        assert "| [mcp:alpha](components.json) |  |  |" in env.text
        assert "| [mcp:chosen](components.json) | x |  |" in env.text
        assert "## plugins" in env.text
        assert "| [plugin:helpers](components.json) |  |  |" in env.text
        assert "mcp:gone" not in env.text
        assert "mcp:account" not in env.text

    @pytest.mark.parametrize("broken", ["{not json", "[]"])
    def test_an_unreadable_manifest_refuses_and_keeps_every_selection(self, env, broken, capsys):
        """A components.json that cannot be read was treated as empty, so reconcile dropped every component row and a repaired manifest brought them back off, losing the selections. It now refuses and writes nothing."""
        self._seed(env)
        manifest = env.root / "components.json"
        manifest.write_text(
            json.dumps({"version": 1, "mcp_servers": [{"name": "chosen", "runtimes": ["claude"], "owner": "fixture", "wanted": True, "command": "npx"}]}),
            encoding="utf-8",
        )
        reconcile.reconcile()
        text = env.text.replace("| [mcp:chosen](components.json) |  |", "| [mcp:chosen](components.json) | x |")
        env.control_plane.write_text(text, encoding="utf-8")
        manifest.write_text(broken, encoding="utf-8")

        code = reconcile.main([])

        assert code == 2
        assert "components.json" in capsys.readouterr().err
        assert env.text == text

    def test_a_manifest_with_no_components_adds_no_tables(self, env):
        self._seed(env)
        (env.root / "components.json").write_text('{"version": 1, "mcp_servers": [], "plugins": []}\n', encoding="utf-8")
        env.control_plane.write_text("# control-plane\n", encoding="utf-8")

        reconcile.reconcile()

        assert "## mcp-servers" not in env.text
        assert "## plugins" not in env.text


class TestMachineScopedProjects:
    """A machine that holds fewer checkouts than the one that last wrote control-plane.md (a container holding a subset of a developer machine's projects) must not rewrite the rows and columns of projects it does not hold. It declares its held set in LLM_ROOT_HELD_PROJECTS; with nothing declared a machine holds every project."""

    # The project-tier rule's row: global, away, away-with-source, here.
    PROJECT_RULE = "| [rule:a-project-rule](rules/a-project-rule.md) | project |  |  |  |  |"
    PROJECT_RULE_IN_AWAY = "| [rule:a-project-rule](rules/a-project-rule.md) | project |  | x |  |  |"

    def _seed(self, env: Env) -> None:
        env.seed("held-source-root")
        (env.projects / "active" / "here" / ".git").mkdir(parents=True)
        origin_checkout(env.projects / "active" / "away", "fixture/away")
        origin_checkout(env.projects / "active" / "away-with-source", "fixture/away-with-source")
        origin_checkout(env.projects / "archived" / "old", "fixture/old")

    def _committed(self, env: Env) -> str:
        """The full machine's control-plane.md, with one hand opt-in in the away column that no default would produce."""
        env.hold(None)
        reconcile.reconcile()
        text = env.text
        assert self.PROJECT_RULE in text
        text = text.replace(self.PROJECT_RULE, self.PROJECT_RULE_IN_AWAY)
        env.control_plane.write_text(text, encoding="utf-8")
        reconcile.reconcile()
        assert env.text == text
        return text

    def _leave(self, env: Env, *relatives: str) -> None:
        for relative in relatives:
            shutil.rmtree(env.projects / relative)

    @pytest.mark.parametrize("held", [None, "", "all", " ALL "])
    def test_all_empty_and_unset_each_hold_every_project(self, env, held):
        self._seed(env)
        self._committed(env)
        self._leave(env, "active/away")

        env.hold(held)
        reconcile.reconcile()

        assert "| away |" not in env.text

    def test_the_present_keyword_ignores_case(self, env):
        self._seed(env)
        self._committed(env)
        self._leave(env, "active/away", "active/away-with-source", "archived/old")
        (env.projects / "active" / "fresh" / ".git").mkdir(parents=True)

        env.hold("Present")
        reconcile.reconcile()

        assert "| fresh | active | normal | base | - |" in env.text
        assert "| away | active | normal | base | fixture/away |" in env.text

    def test_a_committed_active_row_without_a_column_stays_columnless_on_a_scoped_machine(self, env):
        """The full machine keeps away-with-source's row (its projects-root source survives) but drops its column once the checkout is gone; a scoped machine must not bring the column back."""
        self._seed(env)
        self._committed(env)
        self._leave(env, "active/away-with-source")
        env.hold(None)
        reconcile.reconcile()
        committed = env.text
        assert "| option | global | away | here |" in committed

        env.hold("here")
        reconcile.reconcile()

        assert env.text == committed

    def test_a_scoped_machine_keeps_the_committed_status_and_tier_of_a_held_project(self, env):
        """The container clones flat under active/; a developer machine's layout is what records status and tier, so a scoped machine does not rewrite them."""
        self._seed(env)
        committed = self._committed(env)
        shutil.move(env.projects / "archived" / "old", env.projects / "active" / "old")

        env.hold("present")
        reconcile.reconcile()

        assert env.text == committed

    def test_a_project_with_only_a_source_keeps_its_recorded_origin(self, env):
        self._seed(env)
        self._committed(env)
        self._leave(env, "active/away-with-source")

        env.hold(None)
        reconcile.reconcile()

        assert "| away-with-source | active | normal | base | fixture/away-with-source |" in env.text

    def test_a_machine_holding_present_checkouts_leaves_other_projects_alone(self, env):
        self._seed(env)
        committed = self._committed(env)
        assert "| away | active | normal | base | fixture/away |" in committed
        assert "| away-with-source | active | normal | base | fixture/away-with-source |" in committed
        assert "| old | archived | low | base | fixture/old |" in committed
        self._leave(env, "active/away", "active/away-with-source", "archived/old")

        env.hold("present")
        changed, notes = reconcile.reconcile(check=True)
        assert not changed, notes
        reconcile.reconcile()

        assert env.text == committed

    def test_a_new_rule_on_a_scoped_machine_gets_default_cells_in_kept_columns(self, env):
        self._seed(env)
        self._committed(env)
        self._leave(env, "active/away", "active/away-with-source", "archived/old")
        (env.root / "rules" / "b-common-rule.md").write_text("# b-common-rule\n", encoding="utf-8")
        (env.root / "rules" / "tiers.json").write_text(
            '{"global": [], "common": ["a-common-rule.md", "b-common-rule.md"], "project": ["a-project-rule.md"]}\n',
            encoding="utf-8",
        )

        env.hold("present")
        reconcile.reconcile()

        assert "| option | tier | global | away | away-with-source | here |" in env.text
        assert "| [rule:b-common-rule](rules/b-common-rule.md) | common | x | x | x | x |" in env.text
        assert self.PROJECT_RULE_IN_AWAY in env.text

    def test_a_machine_holding_every_project_still_removes_one_whose_checkout_is_gone(self, env):
        self._seed(env)
        self._committed(env)
        self._leave(env, "active/away", "archived/old")

        env.hold(None)
        reconcile.reconcile()

        assert "| away |" not in env.text
        assert "| old |" not in env.text
        assert "| option | global | away-with-source | here |" in env.text

    def test_a_projects_root_removal_lands_on_the_machine_that_holds_the_project(self, env):
        """A scoped machine cannot see whether another machine still holds a checkout, so it keeps the row; the holder removes it."""
        self._seed(env)
        self._committed(env)
        self._leave(env, "active/away", "active/away-with-source", "archived/old")
        shutil.rmtree(env.root / "projects-root" / "away-with-source")

        env.hold("present")
        reconcile.reconcile()
        assert "| away-with-source | active |" in env.text

        env.hold("here,away-with-source")
        reconcile.reconcile()

        assert "away-with-source" not in env.text
        assert "| away | active | normal | base | fixture/away |" in env.text
        assert "| option | global | away | here |" in env.text

    def test_a_declared_list_ignores_a_checkout_it_does_not_name(self, env):
        self._seed(env)
        committed = self._committed(env)
        (env.projects / "active" / "stray" / ".git").mkdir(parents=True)

        env.hold("here")
        reconcile.reconcile()

        assert env.text == committed
