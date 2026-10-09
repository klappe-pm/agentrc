"""Tests for the sync orchestrator: failure propagation, the deploy guard, the diff mode, the --check reverse pass and --prune.

Every tree here is a temporary fixture and every home is a temporary directory reached through STRATARC_HOME. The steps a full run delegates (the reconciler, the project delivery, the permission sweep) are replaced where a test is about the orchestration rather than about them; the end to end tests at the bottom run them for real.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from stratarc import sync
from stratarc.adapters._common import runtime_registry
from tests.conftest import REPO_ROOT, tree_snapshot

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SYNC_SOURCE = FIXTURES / "sync" / "source"
FOREIGN = FIXTURES / "foreign"
REGISTRY_FIXTURE = FIXTURES / "adapters" / "registry"


@pytest.fixture(autouse=True)
def isolated_environment(stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test reads the home through STRATARC_HOME and starts with no environment selected."""
    for name in ("STRATARC_ENVIRONMENT", "STRATARC_SOURCE", "LLM_ROOT_PROJECTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    return stratarc_home


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def commit(root: Path, message: str = "fixture") -> None:
    git(root, "add", "-A")
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", message)


def init_checkout(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")


def fixture_source(tmp_path: Path, name: str = "source") -> Path:
    """A copy of the small synthetic source root under tests/fixtures/sync."""
    root = tmp_path / name
    shutil.copytree(SYNC_SOURCE, root)
    return root


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sync.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# regenerate_derived
# ---------------------------------------------------------------------------


class TestRegenerateDerived:
    def stub_digest(self, monkeypatch: pytest.MonkeyPatch, code: int = 0, out: str = "refreshed", err: str = "") -> list[list[str]]:
        from stratarc import gen_rules_digest

        calls: list[list[str]] = []

        def main(argv):
            calls.append(list(argv))
            print(out, end="")
            print(err, end="", file=sys.stderr)
            return code

        monkeypatch.setattr(gen_rules_digest, "main", main)
        return calls

    def test_failure_is_reported_to_the_caller(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.stub_digest(monkeypatch, code=1, out="", err="digest is invalid")
        with pytest.raises(RuntimeError, match="digest is invalid"):
            sync.regenerate_derived(tmp_path)

    def test_success_returns_a_human_readable_result(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.stub_digest(monkeypatch)
        assert sync.regenerate_derived(tmp_path) == ["rules digest: refreshed"]

    def test_a_silent_failure_names_the_exit_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.stub_digest(monkeypatch, code=3, out="")
        with pytest.raises(RuntimeError, match="exit 3"):
            sync.regenerate_derived(tmp_path)

    def test_a_system_exit_from_the_digest_is_a_failure_not_a_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from stratarc import gen_rules_digest

        def main(argv):
            raise SystemExit(2)

        monkeypatch.setattr(gen_rules_digest, "main", main)
        with pytest.raises(RuntimeError, match="exit 2"):
            sync.regenerate_derived(tmp_path)

    def test_does_not_name_a_missing_claude_md(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh clone carries no local CLAUDE.md when it is gitignored; naming it as an embed target would fail the run there."""
        calls = self.stub_digest(monkeypatch)
        sync.regenerate_derived(tmp_path)
        assert str(tmp_path / "CLAUDE.md") not in calls[0]

    def test_embeds_the_roots_own_claude_codex_gemini_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            (tmp_path / name).write_text("text\n", encoding="utf-8")
        calls = self.stub_digest(monkeypatch)
        # The siblings are rendered after the embed, so a stubbed embed leaves them plain text.
        sync.regenerate_derived(root=tmp_path)
        targets = calls[0]
        assert str(tmp_path / "AGENTS.md") in targets
        assert str(tmp_path / "CLAUDE.md") in targets
        assert str(tmp_path / "GEMINI.md") in targets
        assert str(tmp_path / "CODEX.md") not in targets
        assert targets[targets.index("--rules-root") + 1] == str(tmp_path / "rules")

    def test_a_fresh_root_with_none_of_the_three_embeds_only_agents_md(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        calls = self.stub_digest(monkeypatch)
        sync.regenerate_derived(root=tmp_path)
        targets = calls[0]
        assert str(tmp_path / "AGENTS.md") in targets
        for name in ("CLAUDE.md", "CODEX.md", "GEMINI.md"):
            assert str(tmp_path / name) not in targets

    def test_agents_md_keeps_the_token_and_the_siblings_get_the_roots_path(self, tmp_path: Path) -> None:
        root = fixture_source(tmp_path)
        shutil.copy(root / "AGENTS.md", root / "CLAUDE.md")
        assert sync.regenerate_derived(root)[0].startswith("rules digest:")
        agents = (root / "AGENTS.md").read_text(encoding="utf-8")
        claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert "${STRATARC_SOURCE}" in agents
        assert "${STRATARC_SOURCE}" not in claude
        assert str(root) in claude

    def test_a_second_run_changes_nothing(self, tmp_path: Path) -> None:
        root = fixture_source(tmp_path)
        sync.regenerate_derived(root)
        first = tree_snapshot(root)
        sync.regenerate_derived(root)
        assert tree_snapshot(root) == first

    def test_a_root_whose_agents_md_has_no_markers_is_a_failure(self, tmp_path: Path) -> None:
        root = fixture_source(tmp_path)
        (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="RULES-DIGEST"):
            sync.regenerate_derived(root)


# ---------------------------------------------------------------------------
# the validation gate
# ---------------------------------------------------------------------------


class TestRefuseOnInvalidSource:
    """A deliberately invalid tree refuses deployment before any target write; a valid tree lets it proceed."""

    def test_a_leading_paths_line_refuses_and_reports(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "style.md").write_text('paths:\n  - "**/*.sh"\n\n# style\n\nBody.\n')
        refusal = sync.refuse_on_invalid_source(tmp_path)
        captured = capsys.readouterr()
        assert refusal not in (None, 0)
        assert "refusing to deploy" in captured.err
        assert "rule-leading-paths-line" in captured.out

    def test_a_clean_rule_proceeds(self, tmp_path: Path) -> None:
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "style.md").write_text("# style\n\n## binding\n\nBody.\n")
        assert sync.refuse_on_invalid_source(tmp_path) is None

    def test_the_gate_always_asks_for_the_generic_checks_under_strict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(sync.validate, "main", lambda argv: seen.append(list(argv)) or 0)
        # Whatever the root carries, the gate does not change the class of checks it asks for.
        for name in ("loadouts", "models"):
            (tmp_path / name).mkdir()
        (tmp_path / "PLAN.md").write_text("# plan\n")
        assert sync.refuse_on_invalid_source(tmp_path) is None
        assert seen == [["--root", str(tmp_path), "--strict", "--checks", "generic"]]

    def test_a_source_root_with_an_example_layout_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "example.md").write_text("# example\n\n## binding\n\nBody.\n")
        (tmp_path / "projects-root" / "example").mkdir(parents=True)
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "reviewer.md").write_text("---\nname: reviewer\ndescription: reviews.\ntools: Read\n---\n\n# reviewer\n")
        refusal = sync.refuse_on_invalid_source(tmp_path)
        captured = capsys.readouterr()
        assert refusal is None, captured.err + captured.out

    def test_the_private_checks_do_not_gate_a_deploy(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "scripts" / "private").mkdir(parents=True)
        (tmp_path / "scripts" / "private" / "validate_checks.py").write_text(
            "from stratarc.validate import finding\n\n\ndef check_always(root):\n    return [finding('error', 'always', 'x', 'fails every run')]\n\n\nVALIDATE_CHECKS = [check_always]\n"
        )
        assert sync.refuse_on_invalid_source(tmp_path) is None
        assert "always" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# a full run with the delegated steps replaced
# ---------------------------------------------------------------------------


class Steps:
    """The recorded calls of one stubbed run."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.seen: dict = {}

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]


def stub_steps(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    runtime: str | None = "fixture",
    missing_adapter: bool = False,
    permission_status: int = 0,
    regenerate=None,
    adapter_sync=None,
) -> Steps:
    """Replace everything a run delegates, so one test checks the orchestration and nothing else.

    git stays real, so the deploy guard and the deploy record read the fixture checkout.
    """
    from stratarc import deploy_guard, permissions, project_permissions, staging
    from stratarc.adapters import _components
    from stratarc.control_plane import ControlPlane

    steps = Steps()
    runtimes: dict = {}
    if runtime is not None:
        module = "s01_missing_adapter" if missing_adapter else "s01_fixture_adapter"
        fixture_adapter = types.ModuleType("s01_fixture_adapter")
        fixture_adapter.sync = adapter_sync or (lambda source, target, dry_run=False: [])
        monkeypatch.setitem(sys.modules, "s01_fixture_adapter", fixture_adapter)
        runtimes = {runtime: (module, home / f".{runtime}")}

    def record(name):
        def run(root, *args, **kwargs):
            steps.calls.append((name, root, *args))
            steps.seen.setdefault("source_variable", os.environ.get("STRATARC_SOURCE"))
            return 0

        return run

    def build_stage(root, cp, column):
        steps.seen["stage_root"] = root
        return root, []

    def load_plane(path=None):
        steps.seen["control_plane"] = path
        return types.SimpleNamespace(rows=[])

    def load_policy(root):
        steps.seen["permissions_root"] = root
        return {}

    def sweep(root, live, *, dry_run):
        steps.calls.append(("sweep", root, dry_run))
        return permission_status, []

    monkeypatch.setattr(sync, "load_runtimes", lambda *a, **k: dict(runtimes))
    monkeypatch.setattr(sync, "detected", lambda runtimes_=None: dict(runtimes))
    monkeypatch.setattr(sync, "refuse_on_invalid_source", lambda root: None)
    monkeypatch.setattr(sync, "plugin_ingest", lambda: None)
    monkeypatch.setattr(sync, "run_reconcile", record("reconcile"))
    monkeypatch.setattr(sync, "run_projects", record("projects"))
    monkeypatch.setattr(sync, "run_permission_sweep", sweep)
    monkeypatch.setattr(sync, "regenerate_derived", regenerate or (lambda root=None: []))
    monkeypatch.setattr(sync, "unmanaged_runtime_targets", lambda policy, live: {})
    monkeypatch.setattr(sync, "unmapped_hook_events", lambda stage, name, maps=None: set())
    monkeypatch.setattr(sync, "reverse_pass", lambda *a, **k: {})
    monkeypatch.setattr(sync, "print_reverse_findings", lambda findings: False)
    monkeypatch.setattr(ControlPlane, "load", staticmethod(load_plane))
    monkeypatch.setattr(permissions, "load", load_policy)
    monkeypatch.setattr(permissions, "policy_version", lambda policy: 1)
    monkeypatch.setattr(staging, "build_stage", build_stage)
    monkeypatch.setattr(staging, "cleanup_all", lambda: None)
    monkeypatch.setattr(_components, "notes", lambda stage, name: [])
    return steps


class TestSourceRoot:
    """The source root is --root, then STRATARC_SOURCE, then stratarc.toml, then the current directory; every step sees the resolved root."""

    def committed_source(self, tmp_path: Path) -> Path:
        root = (tmp_path / "source").resolve()
        (root / "rules").mkdir(parents=True)
        (root / "rules" / "example.md").write_text("# example\n\n## binding\n\nBody.\n")
        (root / "AGENTS.md").write_text("# agents\n")
        init_checkout(root)
        commit(root)
        return root

    def assert_staged_from(self, steps: Steps, root: Path) -> None:
        assert steps.seen["stage_root"] == root
        assert steps.seen["control_plane"] == root / "control-plane.md"
        assert steps.seen["permissions_root"] == root
        assert [call[1] for call in steps.calls] == [root, root, root]
        assert steps.seen["source_variable"] == str(root)

    def test_the_flag_stages_from_that_tree(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self.committed_source(tmp_path)
        steps = stub_steps(monkeypatch, stratarc_home)
        code, _out, err = run_main("--root", str(root))
        assert code == 0, err
        self.assert_staged_from(steps, root)

    def test_the_environment_variable_stages_from_that_tree(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self.committed_source(tmp_path)
        monkeypatch.setenv("STRATARC_SOURCE", str(root))
        steps = stub_steps(monkeypatch, stratarc_home)
        code, _out, err = run_main()
        assert code == 0, err
        self.assert_staged_from(steps, root)

    def test_the_nearest_stratarc_toml_names_the_root_when_nothing_else_does(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self.committed_source(tmp_path)
        (root / "stratarc.toml").write_text('name = "stratarc"\n')
        commit(root, "configure")
        (root / "rules" / "nested").mkdir()
        monkeypatch.chdir(root / "rules" / "nested")
        steps = stub_steps(monkeypatch, stratarc_home)
        code, _out, err = run_main()
        assert code == 0, err
        self.assert_staged_from(steps, root)

    def test_the_flag_wins_over_the_environment(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self.committed_source(tmp_path)
        monkeypatch.setenv("STRATARC_SOURCE", str(tmp_path / "elsewhere"))
        steps = stub_steps(monkeypatch, stratarc_home)
        assert run_main("--root", str(root))[0] == 0
        self.assert_staged_from(steps, root)

    def test_the_variable_is_restored_after_a_run(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self.committed_source(tmp_path)
        stub_steps(monkeypatch, stratarc_home)
        monkeypatch.delenv("STRATARC_SOURCE", raising=False)
        run_main("--root", str(root))
        assert "STRATARC_SOURCE" not in os.environ
        monkeypatch.setenv("STRATARC_SOURCE", "/previous/value")
        run_main("--root", str(root))
        assert os.environ["STRATARC_SOURCE"] == "/previous/value"

    def test_the_child_environment_names_the_resolved_root(self, tmp_path: Path) -> None:
        env = sync.child_environment(tmp_path)
        assert env["STRATARC_SOURCE"] == str(tmp_path)
        others = lambda mapping: {k: v for k, v in mapping.items() if k not in ("STRATARC_SOURCE", "PYTHONPATH")}  # noqa: E731
        assert others(env) == others(os.environ)

    def test_the_child_runs_the_engine_that_is_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PYTHONPATH", "/existing/path")
        first, _, rest = sync.child_environment(tmp_path)["PYTHONPATH"].partition(os.pathsep)
        assert Path(first) == REPO_ROOT
        assert rest == "/existing/path"
        child = subprocess.run(
            [sys.executable, "-c", "import stratarc, sys; print(stratarc.__file__)"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=sync.child_environment(tmp_path),
        )
        assert Path(child.stdout.strip()).is_relative_to(REPO_ROOT)


class TestMainRefusesInvalidSourceFirst:
    """An invalid tree is refused before any target write, and the reconciler, which writes control-plane.md, is a target write."""

    def test_main_returns_the_refusal_and_nothing_else_runs(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "source"
        (root / "rules").mkdir(parents=True)
        (root / "rules" / "style.md").write_text('paths:\n  - "**/*.sh"\n\n# style\n\nBody.\n')
        steps = stub_steps(monkeypatch, stratarc_home)
        monkeypatch.setattr(sync, "refuse_on_invalid_source", _real_refuse)
        code, _out, err = run_main("--root", str(root))
        assert code != 0
        assert "refusing to deploy" in err
        assert steps.calls == []

    def test_the_gate_also_runs_for_check_and_diff(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "source"
        (root / "rules").mkdir(parents=True)
        (root / "rules" / "style.md").write_text('paths:\n  - "**/*.sh"\n\n# style\n\nBody.\n')
        steps = stub_steps(monkeypatch, stratarc_home)
        monkeypatch.setattr(sync, "refuse_on_invalid_source", _real_refuse)
        for flag in ("--check", "--diff"):
            code, _out, err = run_main("--root", str(root), flag)
            assert code != 0 and "refusing to deploy" in err
        assert steps.calls == []


_real_refuse = sync.refuse_on_invalid_source


class TestMainTreatsARefusedRenderAsAFailure:
    """A component render that refuses (a config the adapter cannot safely rewrite) must fail the run, name the runtime and leave the target unstamped, while the other runtimes still sync."""

    def test_a_refused_render_exits_2_and_is_not_stamped(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from stratarc import deploy_guard
        from stratarc.adapters._components import RenderRefused

        def refuse(source, target, dry_run=False):
            raise RenderRefused("config.toml defines mcp_servers.demo in a shape this adapter cannot replace; edit it by hand")

        refusing = types.ModuleType("wi_refusing_adapter")
        refusing.sync = refuse
        working = types.ModuleType("wi_working_adapter")
        working.sync = lambda source, target, dry_run=False: []
        monkeypatch.setitem(sys.modules, "wi_refusing_adapter", refusing)
        monkeypatch.setitem(sys.modules, "wi_working_adapter", working)
        stamped: list[str] = []
        stub_steps(monkeypatch, stratarc_home, runtime=None)
        live = {"refusing": ("wi_refusing_adapter", tmp_path / ".refusing"), "working": ("wi_working_adapter", tmp_path / ".working")}
        monkeypatch.setattr(sync, "load_runtimes", lambda *a, **k: dict(live))
        monkeypatch.setattr(sync, "detected", lambda runtimes_=None: dict(live))
        monkeypatch.setattr(deploy_guard, "write_stamp", lambda target, *a, **k: stamped.append(Path(target).name))
        root = tmp_path / "source"
        root.mkdir()
        code, out, err = run_main("--root", str(root))
        assert code == 2
        assert "sync: refusing: config.toml defines mcp_servers.demo" in err
        assert "refusing" not in stamped
        assert ".working" in stamped
        assert "refusing (" not in out


# ---------------------------------------------------------------------------
# the deploy guard
# ---------------------------------------------------------------------------


class TestDeployGuard:
    """With environments declared, a checkout deploys the ref of its environment and records the commit it deployed; with none declared there is no branch guard at all."""

    MANIFEST = {
        "version": 1,
        "environments": {
            "container": {"ref": "main", "description": "fixture container"},
            "workstation": {"ref": "stable", "description": "fixture workstation"},
        },
    }

    def checkout(self, root: Path, manifest: dict | None = None) -> str:
        init_checkout(root)
        (root / "components.json").write_text(json.dumps(self.MANIFEST if manifest is None else manifest))
        commit(root)
        git(root, "branch", "stable")
        remote = root.parent / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
        git(root, "remote", "add", "origin", str(remote))
        git(root, "push", "-q", "origin", "stable")
        return git(root, "rev-parse", "HEAD")

    def run_deploy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        root: Path,
        home: Path,
        environment: str | None,
        branch: str | None,
        *argv: str,
        permission_status: int = 0,
        derived_change: bool = False,
        derived_commit: bool = False,
        missing_adapter: bool = False,
    ) -> tuple[int, str]:
        if branch:
            subprocess.run(["git", "-C", str(root), "switch", "-q", branch], check=True, capture_output=True)
        if environment:
            monkeypatch.setenv("STRATARC_ENVIRONMENT", environment)
        else:
            monkeypatch.delenv("STRATARC_ENVIRONMENT", raising=False)

        def regenerate(root_=None):
            if derived_change or derived_commit:
                (root / "components.json").write_text(json.dumps({**self.MANIFEST, "extra": "generated"}))
            if derived_commit:
                commit(root, "generated")
            return []

        stub_steps(monkeypatch, home, permission_status=permission_status, regenerate=regenerate, missing_adapter=missing_adapter)
        code, _out, err = run_main("--root", str(root), *argv)
        return code, err

    def record(self, home: Path, root: Path) -> dict | None:
        from stratarc import deploy_guard

        return deploy_guard.read_deploy_record(home, root)

    @pytest.fixture
    def layout(self, tmp_path: Path, stratarc_home: Path) -> tuple[Path, Path]:
        root = tmp_path / "root"
        root.mkdir()
        return root, stratarc_home

    def test_the_workstation_refuses_main_and_records_nothing(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", "main")
        assert code == 2
        assert "branch main" in err
        assert "deploys from stable only" in err
        assert self.record(home, root) is None

    def test_the_workstation_deploys_stable_and_records_the_commit(self, layout, monkeypatch) -> None:
        root, home = layout
        head = self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", "stable")
        assert code == 0, err
        record = self.record(home, root)
        assert (record["commit"], record["branch"], record["environment"]) == (head, "stable", "workstation")

    def test_the_workstation_can_explicitly_deploy_another_branch(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        git(root, "switch", "-q", "-c", "feature")
        (root / "feature-change").write_text("explicit deployment\n")
        commit(root, "feature")
        head = git(root, "rev-parse", "HEAD")
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", "feature", "--allow-branch", "feature")
        assert code == 0, err
        record = self.record(home, root)
        assert (record["commit"], record["branch"], record["environment"]) == (head, "feature", "workstation")

    def test_allow_branch_is_repeatable(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        git(root, "switch", "-q", "-c", "second")
        code, err = self.run_deploy(monkeypatch, root, home, "container", "second", "--allow-branch", "other", "--allow-branch", "second")
        assert code == 0, err

    def test_an_off_ref_branch_is_refused_without_the_flag(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        git(root, "switch", "-q", "-c", "feature")
        code, err = self.run_deploy(monkeypatch, root, home, "container", "feature")
        assert code == 2
        assert "branch feature" in err and "--allow-branch" in err

    def test_the_workstation_refuses_a_non_checkout_source(self, layout, monkeypatch) -> None:
        root, home = layout
        (root / "components.json").write_text(json.dumps(self.MANIFEST))
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", None)
        assert code == 2, err
        assert "cannot verify origin/stable" in err
        assert self.record(home, root) is None

    def test_the_workstation_refuses_a_local_stable_commit_not_on_origin(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        git(root, "switch", "-q", "stable")
        (root / "local-change").write_text("not promoted\n")
        commit(root, "local")
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", "stable")
        assert code == 2, err
        assert "origin/stable" in err
        assert self.record(home, root) is None

    def test_the_workstation_refuses_uncommitted_source_changes(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        git(root, "switch", "-q", "stable")
        (root / "components.json").write_text(json.dumps({**self.MANIFEST, "extra": "not promoted"}))
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", "stable")
        assert code == 2, err
        assert "local changes" in err
        assert self.record(home, root) is None

    def test_the_workstation_refuses_source_changes_made_during_sync(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", "stable", derived_change=True)
        assert code == 2, err
        assert "local changes" in err
        assert self.record(home, root) is None

    def test_the_workstation_refuses_a_local_commit_made_during_sync(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "workstation", "stable", derived_commit=True)
        assert code == 2, err
        assert "origin/stable" in err
        assert self.record(home, root) is None

    def test_the_container_deploys_main_and_refuses_stable(self, layout, monkeypatch) -> None:
        root, home = layout
        head = self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "container", "main")
        assert code == 0, err
        assert self.record(home, root)["commit"] == head
        code, err = self.run_deploy(monkeypatch, root, home, "container", "stable")
        assert code == 2
        assert "deploys from main only" in err

    def test_declared_environments_with_none_selected_deploy_main(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, None, "main")
        assert code == 0, err
        code, err = self.run_deploy(monkeypatch, root, home, None, "stable")
        assert code == 2 and "deploys from main only" in err

    def test_a_dirty_full_deploy_does_not_leave_a_commit_baseline(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        assert self.run_deploy(monkeypatch, root, home, "container", "main")[0] == 0
        assert self.record(home, root) is not None
        (root / "components.json").write_text(json.dumps({**self.MANIFEST, "extra": "deployed"}))
        code, err = self.run_deploy(monkeypatch, root, home, "container", "main")
        assert code == 0, err
        assert self.record(home, root) is None

    def test_a_generated_source_change_does_not_record_head(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "container", "main", derived_change=True)
        assert code == 0, err
        assert self.record(home, root) is None

    def test_an_undeclared_environment_is_refused_before_anything_runs(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "laptop", "main")
        assert code == 2
        assert "laptop" in err

    @pytest.mark.parametrize("argv", [("--check",), ("--diff",), ("--only", "fixture")])
    def test_check_diff_and_only_record_nothing(self, layout, monkeypatch, argv) -> None:
        root, home = layout
        self.checkout(root)
        code, err = self.run_deploy(monkeypatch, root, home, "container", "main", *argv)
        assert code == 0, err
        assert self.record(home, root) is None

    def test_a_failed_deploy_leaves_the_last_record(self, layout, monkeypatch) -> None:
        root, home = layout
        head = self.checkout(root)
        assert self.run_deploy(monkeypatch, root, home, "container", "main")[0] == 0
        (root / "g").write_text("2")
        commit(root, "next")
        code, _err = self.run_deploy(monkeypatch, root, home, "container", "main", permission_status=2)
        assert code == 2
        assert self.record(home, root)["commit"] == head

    def test_a_missing_adapter_does_not_advance_the_deploy_record(self, layout, monkeypatch) -> None:
        root, home = layout
        head = self.checkout(root)
        assert self.run_deploy(monkeypatch, root, home, "container", "main")[0] == 0
        (root / "g").write_text("2")
        commit(root, "next")
        code, err = self.run_deploy(monkeypatch, root, home, "container", "main", missing_adapter=True)
        assert code == 2, err
        assert "sync: fixture: adapter missing (s01_missing_adapter); skipped" in err
        assert self.record(home, root)["commit"] == head

    # No environments declared: the public default is no branch guard.

    def test_without_declared_environments_any_branch_deploys(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root, manifest={"version": 1})
        git(root, "switch", "-q", "-c", "feature")
        (root / "change").write_text("work\n")
        commit(root, "work")
        head = git(root, "rev-parse", "HEAD")
        code, err = self.run_deploy(monkeypatch, root, home, None, "feature")
        assert code == 0, err
        assert self.record(home, root)["commit"] == head

    def test_without_declared_environments_a_non_git_source_deploys(self, layout, monkeypatch) -> None:
        root, home = layout
        (root / "components.json").write_text('{"version": 1}')
        code, err = self.run_deploy(monkeypatch, root, home, None, None)
        assert code == 0, err
        assert self.record(home, root) is None

    def test_without_declared_environments_an_off_main_branch_is_not_refused(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root, manifest={"version": 1})
        git(root, "switch", "-q", "-c", "detached-work")
        code, err = self.run_deploy(monkeypatch, root, home, None, "detached-work", "--allow-branch", "unrelated")
        assert code == 0, err

    def test_a_selected_environment_is_an_error_when_none_are_declared(self, layout, monkeypatch) -> None:
        root, home = layout
        self.checkout(root, manifest={"version": 1})
        code, err = self.run_deploy(monkeypatch, root, home, "container", "main")
        assert code == 2
        assert "container" in err


# ---------------------------------------------------------------------------
# the runtime registry
# ---------------------------------------------------------------------------


class TestHookEventMaps:
    def test_every_known_runtime_declares_session_start(self) -> None:
        for name in sync.load_runtimes():
            assert "SessionStart" in sync.hook_event_maps().get(name, set()), name

    def test_unmapped_hook_events_reports_no_gap_for_session_start(self, tmp_path: Path) -> None:
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text(
            json.dumps({"SessionStart": [{"hooks": [{"type": "command", "command": "$HOME/.claude/hooks/session-start.sh", "timeout": 5}]}]})
        )
        for name in sync.load_runtimes():
            assert sync.unmapped_hook_events(tmp_path, name) == set(), name

    def test_the_packaged_hooks_registry_leaves_no_runtime_a_gap(self) -> None:
        """Every adapter declares each event of the packaged guard registry that it delivers or deliberately drops."""
        from stratarc.resources import data_dir

        # The package data directory has the layout of a stage: hooks/hooks.json under it.
        stage = Path(str(data_dir("")))
        assert (stage / "hooks" / "hooks.json").is_file()
        for name in sync.load_runtimes():
            assert sync.unmapped_hook_events(stage, name) == set(), name

    def test_an_event_a_runtime_does_not_declare_is_a_gap(self, tmp_path: Path) -> None:
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"NotAnEvent": []}))
        assert sync.unmapped_hook_events(tmp_path, "claude") == {"NotAnEvent"}


class TestRuntimeRegistry:
    """The sync reads the adapters' runtime registry instead of keeping its own tables."""

    def test_runtimes_hook_maps_and_registries_are_the_registry(self, stratarc_home: Path) -> None:
        registry = runtime_registry()
        assert sync.load_runtimes() == {name: (r.module, r.target(stratarc_home)) for name, r in registry.items()}
        assert sync.hook_event_maps() == {name: set(r.hook_events) for name, r in registry.items()}
        assert sync.hook_registries() == {name: r.hook_registry for name, r in registry.items() if r.hook_registry}

    def test_modules_are_dotted_and_importable(self) -> None:
        for name, (module, _target) in sync.load_runtimes().items():
            assert module == f"stratarc.adapters.{name}"
            importlib.import_module(module)

    def test_the_home_is_read_when_called_not_at_import(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        other = tmp_path / "other-home"
        monkeypatch.setenv("STRATARC_HOME", str(other))
        assert sync.load_runtimes()["claude"][1] == other / ".claude"
        assert sync.short(other / ".claude") == "~/.claude"

    def test_a_sixth_adapter_is_accepted_without_other_edits(self, tmp_path: Path) -> None:
        adapters = REPO_ROOT / "stratarc" / "adapters"
        fixture = tmp_path / "adapters"
        fixture.mkdir()
        for path in adapters.glob("*.py"):
            (fixture / path.name).write_text(path.read_text())
        (fixture / "sixth.py").write_text((REGISTRY_FIXTURE / "sixth.py").read_text())
        home = tmp_path / "home"
        runtimes = sync.load_runtimes(fixture, home)
        maps = sync.hook_event_maps(fixture)
        stage = tmp_path / "stage"
        (stage / "hooks").mkdir(parents=True)
        (stage / "hooks" / "hooks.json").write_text(json.dumps({event: [] for event in ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "SessionStart")}))
        assert runtimes["sixth"] == ("stratarc.adapters.sixth", home / ".sixth")
        assert len(runtimes) == 6
        assert sync.unmapped_hook_events(stage, "sixth", maps) == set()


class TestRuntimeConfiguration:
    """A [runtimes.<name>] table in stratarc.toml turns a runtime off or moves its target."""

    def plain(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = fixture_source(tmp_path)
        for name in (".claude", ".codex"):
            (stratarc_home / name).mkdir()
        return root

    def test_a_target_in_the_table_replaces_the_default(self, tmp_path: Path, stratarc_home: Path) -> None:
        root = fixture_source(tmp_path)
        (root / "stratarc.toml").write_text('[runtimes.claude]\nenabled = true\ntarget = "~/elsewhere/claude"\n')
        runtimes = sync.load_runtimes(root=root)
        assert runtimes["claude"][1] == stratarc_home / "elsewhere" / "claude"
        assert runtimes["codex"][1] == stratarc_home / ".codex"

    def test_a_disabled_runtime_is_not_synced(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self.plain(tmp_path, stratarc_home, monkeypatch)
        (root / "stratarc.toml").write_text("[runtimes.codex]\nenabled = false\n")
        called: list[str] = []
        stub_steps(monkeypatch, stratarc_home, runtime=None)
        live = {name: (f"stub_{name}", stratarc_home / f".{name}") for name in ("claude", "codex")}
        for name in live:
            adapter = types.ModuleType(f"stub_{name}")
            adapter.sync = lambda source, target, dry_run=False, environment=None, _name=name: called.append(_name) or []
            monkeypatch.setitem(sys.modules, f"stub_{name}", adapter)
        monkeypatch.setattr(sync, "load_runtimes", lambda *a, **k: dict(live))
        monkeypatch.setattr(sync, "detected", lambda runtimes_=None: dict(live))
        code, _out, err = run_main("--root", str(root))
        assert code == 0, err
        assert called == ["claude"]

    def test_a_malformed_table_is_a_refusal(self, tmp_path: Path, stratarc_home: Path) -> None:
        root = fixture_source(tmp_path)
        (root / "stratarc.toml").write_text('[runtimes.claude]\nenabled = "yes"\n')
        code, _out, err = run_main("--root", str(root), "--list")
        assert code == 2
        assert "stratarc.toml" in err


class TestRunMainOptions:
    def test_list_names_every_runtime_and_whether_it_is_installed(self, stratarc_home: Path, tmp_path: Path) -> None:
        (stratarc_home / ".claude").mkdir()
        code, out, _err = run_main("--root", str(tmp_path), "--list")
        assert code == 0
        lines = {line.split()[0]: line for line in out.splitlines()}
        assert set(lines) == set(runtime_registry())
        assert lines["claude"].endswith("installed")
        assert lines["codex"].endswith("absent")
        assert "~/.claude" in lines["claude"]

    def test_an_unknown_runtime_is_refused(self, tmp_path: Path) -> None:
        code, _out, err = run_main("--root", str(tmp_path), "--only", "nowhere")
        assert code == 2
        assert "unknown runtime nowhere" in err

    def test_check_and_diff_are_mutually_exclusive(self, tmp_path: Path) -> None:
        code, _out, err = run_main("--root", str(tmp_path), "--check", "--diff")
        assert code == 2 and "mutually exclusive" in err

    def test_dry_run_alone_is_the_diff_and_conflicts_with_check(self, tmp_path: Path) -> None:
        code, _out, err = run_main("--root", str(tmp_path), "--check", "--dry-run")
        assert code == 2 and "mutually exclusive" in err

    def test_diff_cannot_be_combined_with_prune(self, tmp_path: Path) -> None:
        code, _out, err = run_main("--root", str(tmp_path), "--diff", "--prune")
        assert code == 2 and "--prune --dry-run" in err


# ---------------------------------------------------------------------------
# the reverse pass
# ---------------------------------------------------------------------------


def build_stage(root: Path) -> Path:
    """A minimal but complete source tree for the reverse pass.

    Also writes rules/retired.json at root itself (not the stage): that policy file governs runtime rule directories exactly as it governs project checkouts, and staging never copies it into a per-column stage.
    """
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "rules" / "retired.json").write_text(json.dumps({"retired": ["retired-rule.md"]}) + "\n")
    stage = root / "stage"
    (stage / "rules").mkdir(parents=True)
    (stage / "rules" / "a-rule.md").write_text("# a-rule\n\nKeep it.\n")
    (stage / "hooks").mkdir()
    (stage / "hooks" / "guard.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (stage / "agents").mkdir()
    (stage / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\nReview it.\n")
    (stage / "commands").mkdir()
    (stage / "commands" / "deploy.md").write_text("---\ndescription: Deploy\n---\nDeploy.\n")
    (stage / "skills" / "kept-skill").mkdir(parents=True)
    (stage / "skills" / "kept-skill" / "SKILL.md").write_text("---\nname: kept-skill\n---\nBody.\n")
    (stage / "skills" / "sync-exclude.json").write_text(json.dumps({"exclude": {"excluded-skill": "host-adapted"}}) + "\n")
    return stage


def by_kind(findings: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for finding in findings:
        grouped.setdefault(finding.kind, []).append(finding)
    return grouped


def live_for(name: str, target: Path) -> dict:
    return {name: (f"stratarc.adapters.{name}", target)}


class TestReversePassPerAdapter:
    """A fixture runtime per adapter: planted directly in the target, never through sync(), is the content a real machine accumulates over time."""

    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.fixture
    def stage(self, root: Path) -> Path:
        return build_stage(root)

    def hook_fixture(self, root: Path, target: Path, marker: str) -> dict:
        """Foreign, dangling and mislocated hook registrations.

        marker is this target's own managed-tree marker, for example ".claude"; the mislocated command deliberately uses another one so it reads as registered from the wrong runtime.
        """
        wrong = ".codex" if marker != ".codex" else ".gemini"
        return {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": f"if [ -x '{root}/.vendor/agent-hooks/tool.sh' ]; then '{root}/.vendor/agent-hooks/tool.sh'; fi"}]},
                {"hooks": [{"type": "command", "command": str(target / "hooks" / "missing-script.sh")}]},
                {"hooks": [{"type": "command", "command": str(root / wrong / "hooks" / "guard.sh")}]},
            ]
        }

    def assert_common_hook_findings(self, findings: list) -> None:
        grouped = by_kind(findings)
        assert any("agent-hooks/tool.sh" in f.command for f in grouped.get("foreign_hook_group", []))
        assert any("missing-script.sh" in f.command for f in grouped.get("dangling", []))
        assert any("guard.sh" in f.command for f in grouped.get("mislocated", []))

    def test_claude(self, root: Path, stage: Path) -> None:
        target = root / ".claude"
        (target / "rules").mkdir(parents=True)
        (target / "rules" / "stale-rule.md").write_text("stale\n")
        (target / "rules" / "retired-rule.md").write_text("retired\n")
        (target / "agents").mkdir()
        (target / "agents" / "stale-agent.md").write_text("stale\n")
        (target / "skills" / "excluded-skill").mkdir(parents=True)
        (target / "skills" / "excluded-skill" / "SKILL.md").write_text("kept\n")
        (target / "settings.json").write_text(json.dumps({"hooks": self.hook_fixture(root, target, ".claude")}) + "\n")
        findings = sync.reverse_pass(root, stage, live_for("claude", target))["claude"]
        grouped = by_kind(findings)
        assert "stale-rule.md" in [Path(f.path).name for f in grouped["orphan"]]
        assert "retired-rule.md" in [Path(f.path).name for f in grouped["retired"]]
        assert any(Path(f.path).name == "stale-agent.md" for f in grouped["orphan"])
        assert "excluded-skill" in [Path(f.path).name for f in grouped["excluded"]]
        self.assert_common_hook_findings(findings)

    def test_codex(self, root: Path, stage: Path) -> None:
        target = root / ".codex"
        (target / "agents").mkdir(parents=True)
        (target / "agents" / "retired-agent.toml").write_text('name = "x"\n')
        (target / "skills" / "excluded-skill").mkdir(parents=True)
        (target / "skills" / "excluded-skill" / "SKILL.md").write_text("kept\n")
        (target / "hooks.json").write_text(json.dumps({"hooks": self.hook_fixture(root, target, ".codex")}) + "\n")
        findings = sync.reverse_pass(root, stage, live_for("codex", target))["codex"]
        grouped = by_kind(findings)
        assert any(Path(f.path).name == "retired-agent.toml" for f in grouped.get("orphan", []))
        assert "excluded-skill" in [Path(f.path).name for f in grouped["excluded"]]
        self.assert_common_hook_findings(findings)

    def test_codex_system_skills_are_never_an_orphan(self, root: Path, stage: Path) -> None:
        """Codex installs its bundled system skills under skills/.system and reinstalls them itself, so --prune must never see them as an orphan."""
        target = root / ".codex"
        (target / "skills" / ".system" / "skill-creator").mkdir(parents=True)
        (target / "skills" / ".system" / "skill-creator" / "SKILL.md").write_text("bundled\n")
        grouped = by_kind(sync.reverse_pass(root, stage, live_for("codex", target))["codex"])
        assert ".system" not in [Path(f.path).name for f in grouped.get("orphan", [])]
        assert ".system" in [Path(f.path).name for f in grouped["excluded"]]

    def test_gemini(self, root: Path, stage: Path) -> None:
        target = root / ".gemini"
        (target / "commands").mkdir(parents=True)
        (target / "commands" / "stale.toml").write_text('description = "x"\n')
        (target / "skills" / "excluded-skill").mkdir(parents=True)
        (target / "skills" / "excluded-skill" / "SKILL.md").write_text("kept\n")
        (target / "settings.json").write_text(json.dumps({"hooks": self.hook_fixture(root, target, ".gemini")}) + "\n")
        findings = sync.reverse_pass(root, stage, live_for("gemini", target))["gemini"]
        grouped = by_kind(findings)
        assert any(Path(f.path).name == "stale.toml" for f in grouped.get("orphan", []))
        assert "excluded-skill" in [Path(f.path).name for f in grouped["excluded"]]
        self.assert_common_hook_findings(findings)

    def test_cursor(self, root: Path, stage: Path) -> None:
        target = root / ".cursor"
        (target / "rules").mkdir(parents=True)
        (target / "rules" / "stale-rule.md").write_text("stale\n")
        (target / "hooks.json").write_text(json.dumps({"version": 1, "hooks": self.hook_fixture(root, target, ".cursor")}) + "\n")
        findings = sync.reverse_pass(root, stage, live_for("cursor", target))["cursor"]
        assert "stale-rule.md" in [Path(f.path).name for f in by_kind(findings)["orphan"]]
        self.assert_common_hook_findings(findings)

    def flat(self, fixture: dict) -> dict:
        """The same registrations as Cursor's flat entries, the only shape Cursor honors."""
        return {event: [{"command": hook["command"]} for group in groups for hook in group["hooks"]] for event, groups in fixture.items()}

    def test_cursor_flat_entries(self, root: Path, stage: Path) -> None:
        target = root / ".cursor"
        target.mkdir(parents=True)
        (target / "hooks.json").write_text(json.dumps({"version": 1, "hooks": self.flat(self.hook_fixture(root, target, ".cursor"))}) + "\n")
        self.assert_common_hook_findings(sync.reverse_pass(root, stage, live_for("cursor", target))["cursor"])

    def test_a_dangling_flat_entry_is_removed_and_its_neighbours_kept(self, root: Path, stage: Path) -> None:
        target = root / ".cursor"
        target.mkdir(parents=True)
        registry = target / "hooks.json"
        registry.write_text(json.dumps({"version": 1, "hooks": self.flat(self.hook_fixture(root, target, ".cursor"))}) + "\n")
        dangling = [f for f in sync.reverse_pass(root, stage, live_for("cursor", target))["cursor"] if f.kind == "dangling"]
        assert len(dangling) == 1
        sync._remove_dangling_registrations(registry, dangling)
        data = json.loads(registry.read_text())
        commands = [entry["command"] for entry in data["hooks"]["PreToolUse"]]
        assert len(commands) == 2
        assert not any("missing-script.sh" in c for c in commands)
        assert data["version"] == 1

    def test_opencode(self, root: Path, stage: Path) -> None:
        """OpenCode has no JSON hook registry: only the directory side applies."""
        target = root / ".config" / "opencode"
        (target / "rules").mkdir(parents=True)
        (target / "rules" / "stale-rule.md").write_text("stale\n")
        (target / "rules" / "retired-rule.md").write_text("retired\n")
        (target / "agents").mkdir()
        (target / "agents" / "stale-agent.md").write_text("stale\n")
        (target / "skill" / "excluded-skill").mkdir(parents=True)
        (target / "skill" / "excluded-skill" / "SKILL.md").write_text("kept\n")
        grouped = by_kind(sync.reverse_pass(root, stage, live_for("opencode", target))["opencode"])
        assert "stale-rule.md" in [Path(f.path).name for f in grouped["orphan"]]
        assert "retired-rule.md" in [Path(f.path).name for f in grouped["retired"]]
        assert any(Path(f.path).name == "stale-agent.md" for f in grouped["orphan"])
        assert "excluded-skill" in [Path(f.path).name for f in grouped["excluded"]]
        for absent in ("dangling", "mislocated", "foreign_hook_group"):
            assert absent not in grouped

    def test_an_adapter_that_cannot_be_imported_yields_no_findings(self, root: Path, stage: Path) -> None:
        live = {"ghost": ("stratarc.adapters.ghost", root / ".ghost")}
        assert sync.reverse_pass(root, stage, live) == {"ghost": []}


class TestPrintReverseFindings:
    def run(self, findings: dict) -> tuple[bool, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bad = sync.print_reverse_findings(findings)
        return bad, out.getvalue()

    def test_orphan_retired_and_dangling_fail_the_check(self) -> None:
        bad, printed = self.run(
            {"claude": [sync.Finding("claude", "orphan", "orphan line"), sync.Finding("claude", "retired", "retired line"), sync.Finding("claude", "dangling", "dangling line")]}
        )
        assert bad
        assert "orphan: orphan line" in printed
        assert "retired: retired line" in printed
        assert "dangling registration: dangling line" in printed

    def test_excluded_and_foreign_are_informational_only(self) -> None:
        bad, printed = self.run(
            {"codex": [sync.Finding("codex", "excluded", "excluded line"), sync.Finding("codex", "foreign_hook_group", "foreign line", event="PreToolUse")]}
        )
        assert not bad
        assert "excluded: excluded line" in printed
        assert "foreign hook group: foreign line" in printed

    def test_mislocated_is_informational_only(self) -> None:
        bad, _printed = self.run({"claude": [sync.Finding("claude", "mislocated", "x")]})
        assert not bad

    def test_undeclared_unreadable_and_missing_alone_do_not_fail(self) -> None:
        bad, printed = self.run(
            {
                "claude": [
                    sync.Finding("claude", "undeclared", "mcp server stray"),
                    sync.Finding("claude", "unreadable", "plugin x: why"),
                    sync.Finding("claude", "declared", "mcp server docs-server"),
                ],
                "machine": [sync.Finding("machine", "missing", "dependency y")],
            }
        )
        assert not bad
        assert "undeclared: mcp server stray" in printed
        assert "unreadable: plugin x: why" in printed
        assert "missing dependency: dependency y" in printed
        assert "docs-server" not in printed
        assert "claude: inventory: 1 declared, 1 undeclared, 0 returned, 1 unreadable" in printed


# ---------------------------------------------------------------------------
# --prune
# ---------------------------------------------------------------------------


class TestRunPrune:
    """--prune deletes orphan, retired and dangling; nothing else."""

    @pytest.fixture
    def layout(self, tmp_path: Path, stratarc_home: Path):
        # run_prune builds its own stage from the root through the control plane and staging, gated by control-plane.md; with none present that stage is empty, so everything present in the target reads as an orphan. That is what these fixtures want: the deletion mechanics, not the classification the per-adapter tests already cover.
        root = tmp_path / "source"
        (root / "rules").mkdir(parents=True)
        (root / "rules" / "retired.json").write_text(json.dumps({"retired": ["retired-rule.md"]}) + "\n")
        target = stratarc_home / ".claude"
        (target / "rules").mkdir(parents=True)
        (target / "rules" / "stale-rule.md").write_text("stale\n")
        (target / "rules" / "retired-rule.md").write_text("retired\n")
        # A mixed group: one dangling hook and one live foreign hook, to prove the surviving hook and the group's position are kept.
        hooks = {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": str(target / "hooks" / "missing-script.sh")},
                        {"type": "command", "command": f"if [ -x '{root}/.vendor/agent-hooks/tool.sh' ]; then '{root}/.vendor/agent-hooks/tool.sh'; fi"},
                    ]
                }
            ]
        }
        (target / "settings.json").write_text(json.dumps({"hooks": hooks, "theme": "dark"}) + "\n")
        return root, target, live_for("claude", target), stratarc_home

    def prune(self, root: Path, live: dict, *, dry_run: bool) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sync.run_prune(root, live, dry_run=dry_run)
        return code, out.getvalue()

    def test_dry_run_names_orphan_retired_and_dangling_only(self, layout) -> None:
        root, target, live, home = layout
        code, printed = self.prune(root, live, dry_run=True)
        assert code == 0
        assert "would delete claude: orphan:" in printed
        assert "would delete claude: retired:" in printed
        assert "would delete claude: dangling:" in printed
        assert "excluded" not in printed and "foreign" not in printed
        assert (target / "rules" / "stale-rule.md").exists()
        assert (target / "rules" / "retired-rule.md").exists()
        assert not (home / ".agent-hooks").exists()

    def test_real_prune_deletes_orphan_retired_and_dangling_and_survivors_remain(self, layout) -> None:
        root, target, live, home = layout
        code, _printed = self.prune(root, live, dry_run=False)
        assert code == 0
        assert not (target / "rules" / "stale-rule.md").exists()
        assert not (target / "rules" / "retired-rule.md").exists()
        settings = json.loads((target / "settings.json").read_text())
        commands = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert not any("missing-script.sh" in c for c in commands)
        assert any("agent-hooks/tool.sh" in c for c in commands)
        assert settings["theme"] == "dark"
        telemetry_files = list((home / ".agent-hooks" / "telemetry").glob("prune-*.txt"))
        assert len(telemetry_files) == 1
        text = telemetry_files[0].read_text()
        for name in ("stale-rule.md", "retired-rule.md", "missing-script.sh"):
            assert name in text

    def test_a_second_prune_the_same_day_keeps_the_first_recovery_list(self, layout) -> None:
        root, target, live, home = layout
        self.prune(root, live, dry_run=False)
        (target / "rules" / "second-stale-rule.md").write_text("stale\n")
        self.prune(root, live, dry_run=False)
        telemetry_files = list((home / ".agent-hooks" / "telemetry").glob("prune-*.txt"))
        assert len(telemetry_files) == 1
        text = telemetry_files[0].read_text()
        assert any(line.endswith("/rules/stale-rule.md") for line in text.splitlines()), text
        for name in ("retired-rule.md", "missing-script.sh", "second-stale-rule.md"):
            assert name in text

    def test_nothing_to_delete_is_reported_and_writes_no_telemetry(self, tmp_path: Path, stratarc_home: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        (root / "control-plane.md").write_text("")
        target = stratarc_home / ".claude"
        target.mkdir(parents=True)
        code, printed = self.prune(root, live_for("claude", target), dry_run=False)
        assert code == 0
        assert "nothing to delete" in printed
        assert not (stratarc_home / ".agent-hooks").exists()

    def test_the_main_entry_point_reaches_prune_with_dry_run(self, layout, monkeypatch: pytest.MonkeyPatch) -> None:
        root, target, live, home = layout
        monkeypatch.setattr(sync, "load_runtimes", lambda *a, **k: dict(live))
        code, out, _err = run_main("--root", str(root), "--prune", "--dry-run")
        assert code == 0
        assert "would delete claude: orphan:" in out
        assert (target / "rules" / "stale-rule.md").exists()


# ---------------------------------------------------------------------------
# the external inventory
# ---------------------------------------------------------------------------


class TestExternalInventory:
    """A fixture home with one item of each state: declared items are silent, undeclared ones are informational, wanted: false ones are returned and fail the check naming the tool that installs them, an unreadable location is unreadable, and a literal secret is reported by file and key only."""

    VENDOR = "vendor-tool"

    @pytest.fixture
    def layout(self, tmp_path: Path, stratarc_home: Path, monkeypatch: pytest.MonkeyPatch):
        from stratarc import staging

        root = tmp_path / "source"
        root.mkdir()
        home = stratarc_home
        stage = build_stage(root)
        target = home / ".claude"
        literal = "sk-" + "b" * 40
        command = f"if [ -x '{root}/.vendor/agent-hooks/tool.sh' ]; then '{root}/.vendor/agent-hooks/tool.sh'; fi"
        (root / "components.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "mcp_servers": [
                        {"name": "docs-server", "runtimes": ["claude"], "owner": "stratarc", "wanted": True, "command": "npx"},
                        {"name": "old-server", "runtimes": ["claude"], "owner": "old-installer", "wanted": False, "command": "x"},
                    ],
                    "foreign_hooks": [
                        {"name": "vendor", "runtimes": ["claude"], "owner": self.VENDOR, "wanted": False, "match": "/.vendor/"},
                        {"name": "graft", "runtimes": ["claude"], "owner": "graft", "wanted": True, "match": "/.graft/"},
                    ],
                    "third_party_skills": [
                        {"name": "bundled-skill", "runtimes": ["claude"], "owner": "bundle-installer", "wanted": False, "installer": "bundle installer"},
                        {"name": "wanted-skill", "runtimes": ["claude"], "owner": "someone", "wanted": True, "installer": "someone"},
                    ],
                    "services": [
                        {
                            "name": "sample-daemon",
                            "runtimes": ["claude"],
                            "owner": "Packager",
                            "wanted": False,
                            "kind": "launchd",
                            "label": "com.example.daemon",
                            "match": "com.example.daemon",
                            "remove": "packager services stop daemon",
                        }
                    ],
                    "dependencies": [
                        {
                            "name": "never-installed-tool",
                            "runtimes": ["claude"],
                            "owner": "stratarc",
                            "wanted": True,
                            "command": "never-installed-tool-fixture",
                            "install": "packager install never-installed-tool",
                            "version": "1.0.0",
                            "image": {"method": "apt", "package": "never-installed-tool"},
                        }
                    ],
                }
            )
        )
        for skill in ("bundled-skill", "wanted-skill"):
            (target / "skills" / skill).mkdir(parents=True)
            (target / "skills" / skill / "SKILL.md").write_text("x\n")
        (target / "settings.json").write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "hooks": {
                        "PreToolUse": [
                            {"hooks": [{"type": "command", "command": command}]},
                            {"hooks": [{"type": "command", "command": f"'{root}/.graft/hook.sh'"}]},
                            {"hooks": [{"type": "command", "command": f"'{root}/.other/hook.sh'"}]},
                        ]
                    },
                }
            )
            + "\n"
        )
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "docs-server": {"command": "npx"},
                        "stray": {"command": "stray"},
                        "old-server": {"command": "x"},
                        "leaky": {"command": "y", "env": {"API_KEY": literal, "SAFE": "secret://vault/key"}},
                    }
                }
            )
        )
        (target / "plugins").mkdir(parents=True)
        (target / "plugins" / "installed_plugins.json").write_text("{broken")
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        with (agents / "com.example.daemon.plist").open("wb") as handle:
            plistlib.dump({"Label": "com.example.daemon", "ProgramArguments": ["/usr/local/bin/daemon"], "EnvironmentVariables": {"DAEMON_KEY": literal}}, handle)
        monkeypatch.setattr(staging, "build_stage", lambda *a, **k: (stage, []))
        return types.SimpleNamespace(root=root, home=home, stage=stage, target=target, literal=literal, command=command, live=live_for("claude", target))

    def findings(self, layout) -> dict:
        return sync.reverse_pass(layout.root, layout.stage, layout.live, home=layout.home)

    def labels(self, findings: dict, kind: str) -> list[str]:
        return [f.label for items in findings.values() for f in items if f.kind == kind]

    def test_every_item_is_declared_undeclared_returned_or_unreadable(self, layout) -> None:
        findings = self.findings(layout)
        declared = " ".join(self.labels(findings, "declared"))
        assert "docs-server" in declared and "wanted-skill" in declared and ".graft/hook.sh" in declared
        undeclared = " ".join(self.labels(findings, "undeclared"))
        assert "stray" in undeclared and "docs-server" not in undeclared
        returned = " ".join(self.labels(findings, "returned"))
        for expected in ("old-server", "installed by old-installer", "bundled-skill", "installed by bundle-installer", "com.example.daemon", "packager services stop daemon"):
            assert expected in returned, expected
        unreadable = " ".join(self.labels(findings, "unreadable"))
        assert "installed_plugins.json" in unreadable and "connector" in unreadable
        assert "never-installed-tool" in " ".join(self.labels(findings, "missing"))
        foreign = " ".join(self.labels(findings, "foreign_hook_group"))
        assert ".other/hook.sh" in foreign and ".graft/hook.sh" not in foreign
        orphans = " ".join(self.labels(findings, "orphan"))
        assert "bundled-skill" not in orphans and "wanted-skill" not in orphans

    def test_a_re_registered_pruned_group_fails_the_check_and_names_its_tool(self, layout) -> None:
        findings = self.findings(layout)
        returned = [f for f in findings["claude"] if f.kind == "returned" and f.command == layout.command]
        assert len(returned) == 1
        assert f"installed by {self.VENDOR}" in returned[0].label
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bad = sync.print_reverse_findings(findings)
        assert bad
        assert f"returned: foreign hook group PreToolUse: {layout.command} (installed by {self.VENDOR})" in out.getvalue()

    def test_literal_secrets_are_reported_by_file_and_key_and_never_by_value(self, layout) -> None:
        findings = self.findings(layout)
        secrets = self.labels(findings, "literal_secret")
        assert any(".claude.json" in s and "mcpServers.leaky.env.API_KEY" in s for s in secrets)
        assert any("com.example.daemon.plist" in s and "EnvironmentVariables.DAEMON_KEY" in s for s in secrets)
        assert not any("SAFE" in s for s in secrets)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bad = sync.print_reverse_findings(findings)
        assert bad
        assert "literal secret:" in out.getvalue()
        assert layout.literal not in out.getvalue()
        assert layout.literal not in repr(findings)

    def prune(self, layout, *, dry_run: bool) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sync.run_prune(layout.root, layout.live, dry_run=dry_run)
        return code, out.getvalue()

    def test_prune_removes_returned_skill_and_hook_group_and_nothing_else_external(self, layout) -> None:
        code, printed = self.prune(layout, dry_run=False)
        assert code == 0
        assert not (layout.target / "skills" / "bundled-skill").exists()
        assert (layout.target / "skills" / "wanted-skill").exists()
        settings = json.loads((layout.target / "settings.json").read_text())
        commands = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert layout.command not in commands
        assert any(".graft/" in c for c in commands) and any(".other/" in c for c in commands)
        assert settings["theme"] == "dark"
        # A returned MCP server lives in a runtime-native file this source root does not own: it is named with its owner, never edited.
        assert "old-server" in (layout.home / ".claude.json").read_text()
        assert "not pruned" in printed and "installed by old-installer" in printed
        telemetry = next((layout.home / ".agent-hooks" / "telemetry").glob("prune-*.txt")).read_text()
        assert "bundled-skill" in telemetry and ".vendor/agent-hooks/tool.sh" in telemetry
        # The tool puts the group back: the next check fails and names it.
        settings["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": layout.command}]})
        (layout.target / "settings.json").write_text(json.dumps(settings) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert sync.print_reverse_findings(self.findings(layout))
        assert f"(installed by {self.VENDOR})" in out.getvalue()

    def test_prune_dry_run_names_returned_items(self, layout) -> None:
        _code, printed = self.prune(layout, dry_run=True)
        assert "would delete claude: returned:" in printed
        assert "bundled-skill" in printed and ".vendor/agent-hooks/tool.sh" in printed
        assert (layout.target / "skills" / "bundled-skill").exists()

    def test_the_manifest_outranks_sync_exclude_for_a_skill(self, layout) -> None:
        """A skill in skills/sync-exclude.json that components.json declares wanted: false is returned and pruned, not excluded."""
        manifest = json.loads((layout.root / "components.json").read_text())
        manifest["third_party_skills"].append({"name": "excluded-skill", "runtimes": ["claude"], "owner": "other-installer", "wanted": False, "installer": "other-installer"})
        (layout.root / "components.json").write_text(json.dumps(manifest))
        (layout.target / "skills" / "excluded-skill").mkdir(parents=True)
        (layout.target / "skills" / "excluded-skill" / "SKILL.md").write_text("x\n")
        findings = self.findings(layout)
        returned = " ".join(self.labels(findings, "returned"))
        assert "excluded-skill" in returned and "installed by other-installer" in returned
        assert "excluded-skill" not in " ".join(self.labels(findings, "excluded"))
        assert "excluded-skill" in self.prune(layout, dry_run=True)[1]

    def test_prune_refuses_when_the_manifest_cannot_be_read(self, layout) -> None:
        """Without the manifest a wanted skill reads as an orphan, so a prune would delete it. Refuse instead."""
        (layout.root / "components.json").write_text("{nope")
        code, printed = self.prune(layout, dry_run=False)
        assert code == 2
        assert "components.json" in printed
        assert (layout.target / "skills" / "wanted-skill").exists()
        assert not (layout.home / ".agent-hooks").exists()

    def test_prune_refuses_an_invalid_manifest(self, layout) -> None:
        """"wanted": "true" is not a boolean, and a prune must not read it as an unwanted skill and delete it."""
        manifest = json.loads((layout.root / "components.json").read_text())
        for entry in manifest["third_party_skills"]:
            if entry["name"] == "wanted-skill":
                entry["wanted"] = "true"
        (layout.root / "components.json").write_text(json.dumps(manifest))
        code, printed = self.prune(layout, dry_run=True)
        assert code == 2
        assert "wanted must be true or false" in printed
        assert "would delete" not in printed

    def test_an_unreadable_manifest_is_reported(self, layout) -> None:
        (layout.root / "components.json").write_text("{nope")
        assert any("components.json" in label for label in self.labels(self.findings(layout), "unreadable"))


# ---------------------------------------------------------------------------
# declared foreign tools, from synthetic captures
# ---------------------------------------------------------------------------


class OneSkillPlane:
    """One skill selected and nothing else.

    An adapter declares its skills directory owned only when the stage has skills to put there, and the reverse pass scans a directory only when an adapter owns it. A stage with no skills would leave every skill directory on the machine unscanned, which is not the state the real global column produces.
    """

    def __init__(self, name: str = "kept-skill") -> None:
        self._name = name

    def enabled(self, column: str, item: str) -> bool:
        return False

    def enabled_ids(self, column: str, prefix: str) -> set[str]:
        return {self._name} if prefix == "skill:" else set()


class TestForeignToolsAreDeclared:
    """The reverse pass on a fixture home reports a foreign tool's hooks and skills as declared, and a tool declared unwanted as returned.

    Reads the synthetic manifest and captured registrations under tests/fixtures/foreign, stages them the way staging does, registers them through the real adapters, and classifies what is there. The skill directories are planted in the target, which is where the tool's own installer puts them.
    """

    RUNTIMES = ("claude", "codex", "cursor", "gemini")
    SKILLS_SEEN_BY_THE_REVERSE_PASS = ("claude", "codex", "gemini")
    SKILLS = ("reviewbot-review", "reviewbot-fix")

    @pytest.fixture
    def layout(self, tmp_path: Path, stratarc_home: Path):
        from stratarc import staging

        root = tmp_path / "source"
        shutil.copytree(FOREIGN, root)
        (root / "skills" / "kept-skill").mkdir(parents=True)
        (root / "skills" / "kept-skill" / "SKILL.md").write_text("---\nname: kept-skill\n---\nBody.\n")
        stage, _notes = staging.build_stage(root, OneSkillPlane(), "global")
        live = {name: (runtime.module, runtime.target(stratarc_home)) for name, runtime in runtime_registry().items() if name in self.RUNTIMES}
        yield types.SimpleNamespace(root=root, stage=stage, live=live, home=stratarc_home)
        staging.cleanup_all()

    def findings(self, layout) -> dict:
        for module, target in layout.live.values():
            target.mkdir(parents=True, exist_ok=True)
            importlib.import_module(module).sync(layout.stage, target)
            for skill in self.SKILLS:
                directory = target / "skills" / skill
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "SKILL.md").write_text("installed by the tool\n")
        return sync.reverse_pass(layout.root, layout.stage, layout.live, home=layout.home)

    def test_the_agent_hook_is_declared_in_every_runtime_that_registers_it(self, layout) -> None:
        findings = self.findings(layout)
        for runtime in self.RUNTIMES:
            declared = [f.label for f in findings[runtime] if f.kind == "declared" and "reviewbot hook run" in f.label]
            assert len(declared) == 3, (runtime, declared)
            assert all(label.startswith("foreign hook group") for label in declared)

    def test_every_skill_directory_is_declared_where_the_reverse_pass_looks(self, layout) -> None:
        findings = self.findings(layout)
        for runtime in self.SKILLS_SEEN_BY_THE_REVERSE_PASS:
            declared = [f.label for f in findings[runtime] if f.kind == "declared" and f.label.startswith("skill ")]
            assert len(declared) == len(self.SKILLS), (runtime, declared)
            for skill in self.SKILLS:
                assert any(label.endswith(skill) for label in declared), skill

    def test_nothing_the_tool_installs_is_an_orphan_or_a_returned_component(self, layout) -> None:
        # The point of declaring them: --check stays green and --prune, which deletes an orphan and a returned component, leaves them alone.
        for runtime, items in self.findings(layout).items():
            for item in items:
                if "reviewbot" in item.label:
                    assert item.kind == "declared", f"{runtime}: {item.kind}: {item.label}"

    def test_a_missing_registration_is_restored_and_a_second_sync_writes_nothing(self, layout) -> None:
        claude = importlib.import_module(layout.live["claude"][0])
        target = layout.live["claude"][1]
        target.mkdir(parents=True, exist_ok=True)
        claude.sync(layout.stage, target)
        settings = json.loads((target / "settings.json").read_text())
        registered = [event for event, groups in settings["hooks"].items() for group in groups if any("reviewbot hook run" in hook.get("command", "") for hook in group["hooks"])]
        assert sorted(registered) == ["PostToolUse", "PreToolUse", "Stop"]
        assert claude.sync(layout.stage, target, dry_run=True) == []
        del settings["hooks"]["Stop"]
        (target / "settings.json").write_text(json.dumps(settings) + "\n")
        assert claude.sync(layout.stage, target, dry_run=True) != []
        claude.sync(layout.stage, target)
        assert claude.sync(layout.stage, target, dry_run=True) == []

    def test_a_tool_declared_unwanted_is_returned_not_an_orphan(self, layout) -> None:
        runtime = runtime_registry()["claude"]
        target = layout.live["claude"][1]
        target.mkdir(parents=True, exist_ok=True)
        importlib.import_module(runtime.module).sync(layout.stage, target)
        directory = target / "skills" / "leftover-canvas"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("installed by the canvas tool\n")
        findings = sync.reverse_pass(layout.root, layout.stage, layout.live, home=layout.home)
        matches = [f for f in findings["claude"] if "leftover-canvas" in f.label]
        assert len(matches) == 1, matches
        assert matches[0].kind == "returned"
        assert "canvas-tool" in matches[0].label

    def test_cursor_keeps_no_skills_directory_so_its_copies_are_never_seen(self, layout) -> None:
        # Recorded, not fixed: giving Cursor a per-item skill OwnedDir would make every other directory under ~/.cursor/skills an orphan that --prune deletes, which is a change to Cursor's ownership.
        from stratarc.adapters import cursor

        owned = {directory.kind for directory in cursor.owned_outputs(layout.stage, layout.home / ".cursor")}
        assert "skill" not in owned


# ---------------------------------------------------------------------------
# --check and --diff end to end
# ---------------------------------------------------------------------------


class TestDiff:
    """--diff (and --dry-run alone) stages the source, runs every adapter in check mode, prints what would change, writes nothing anywhere and exits 0."""

    @pytest.fixture
    def layout(self, tmp_path: Path, stratarc_home: Path):
        source = fixture_source(tmp_path)
        (stratarc_home / ".claude").mkdir()
        return source, stratarc_home

    @pytest.mark.parametrize("flags", [("--diff",), ("--dry-run",)])
    def test_a_diff_writes_nothing_and_exits_zero(self, layout, flags) -> None:
        source, home = layout
        source_before, home_before = tree_snapshot(source), tree_snapshot(home)
        code, out, err = run_main("--root", str(source), *flags)
        assert code == 0, out + err
        assert "sync: claude (~/.claude) would:" in out
        assert "render permissions.json" in out
        assert tree_snapshot(source) == source_before
        assert tree_snapshot(home) == home_before

    def test_a_diff_of_a_deployed_tree_reports_it_current(self, layout) -> None:
        source, home = layout
        code, out, err = run_main("--root", str(source))
        assert code == 0, out + err
        assert tree_snapshot(home / ".claude")
        source_before, home_before = tree_snapshot(source), tree_snapshot(home)
        code, out, err = run_main("--root", str(source), "--diff")
        assert code == 0, out + err
        assert "sync: claude: current" in out
        assert "would:" not in out
        assert tree_snapshot(source) == source_before and tree_snapshot(home) == home_before

    def test_a_diff_reports_a_drifted_file_and_still_exits_zero(self, layout) -> None:
        source, home = layout
        assert run_main("--root", str(source))[0] == 0
        (home / ".claude" / "AGENTS.md").write_text("edited by hand\n")
        home_before = tree_snapshot(home)
        code, out, _err = run_main("--root", str(source), "--diff")
        assert code == 0
        assert "sync: claude (~/.claude) would:" in out
        assert tree_snapshot(home) == home_before

    def test_check_reports_the_same_drift_as_a_failure(self, layout) -> None:
        source, home = layout
        assert run_main("--root", str(source))[0] == 0
        (home / ".claude" / "AGENTS.md").write_text("edited by hand\n")
        source_before, home_before = tree_snapshot(source), tree_snapshot(home)
        code, out, _err = run_main("--root", str(source), "--check")
        assert code == 1
        assert "sync: claude (~/.claude) would:" in out
        assert tree_snapshot(source) == source_before and tree_snapshot(home) == home_before

    def test_check_on_a_source_whose_control_plane_is_stale_fails_before_comparing(self, layout) -> None:
        source, home = layout
        source_before, home_before = tree_snapshot(source), tree_snapshot(home)
        code, out, _err = run_main("--root", str(source), "--check")
        assert code == 1
        assert "sync: claude" not in out
        assert tree_snapshot(source) == source_before and tree_snapshot(home) == home_before

    def test_a_clean_deploy_passes_check(self, layout) -> None:
        source, _home = layout
        assert run_main("--root", str(source))[0] == 0
        code, out, err = run_main("--root", str(source), "--check")
        assert code == 0, out + err

    def test_a_diff_still_refuses_an_invalid_source(self, layout) -> None:
        source, home = layout
        (source / "rules" / "bad.md").write_text('paths:\n  - "x"\n\n# bad\n')
        home_before = tree_snapshot(home)
        code, _out, err = run_main("--root", str(source), "--diff")
        assert code != 0 and "refusing to deploy" in err
        assert tree_snapshot(home) == home_before

    def test_a_diff_with_only_names_just_that_runtime(self, layout) -> None:
        source, home = layout
        (home / ".codex").mkdir()
        code, out, _err = run_main("--root", str(source), "--diff", "--only", "claude")
        assert code == 0
        assert "sync: claude" in out and "sync: codex" not in out

    def test_a_diff_does_not_stop_when_the_reconciler_reports_drift(self, layout, monkeypatch: pytest.MonkeyPatch) -> None:
        source, _home = layout
        monkeypatch.setattr(sync, "run_reconcile", lambda root, check: 1)
        monkeypatch.setattr(sync, "run_projects", lambda root, check: 1)
        code, out, _err = run_main("--root", str(source), "--diff")
        assert code == 0
        assert "sync: claude (~/.claude) would:" in out

    def test_a_diff_surfaces_a_failure_that_is_not_drift(self, layout, monkeypatch: pytest.MonkeyPatch) -> None:
        source, _home = layout
        monkeypatch.setattr(sync, "run_reconcile", lambda root, check: 2)
        assert run_main("--root", str(source), "--diff")[0] == 2

    def test_the_diff_runs_every_delegated_step_read_only(self, layout, monkeypatch: pytest.MonkeyPatch) -> None:
        source, _home = layout
        seen: list[tuple] = []
        monkeypatch.setattr(sync, "run_reconcile", lambda root, check: seen.append(("reconcile", check)) or 0)
        monkeypatch.setattr(sync, "run_projects", lambda root, check: seen.append(("projects", check)) or 0)
        monkeypatch.setattr(sync, "run_permission_sweep", lambda root, live, *, dry_run: seen.append(("sweep", dry_run)) or (0, []))
        monkeypatch.setattr(sync, "regenerate_derived", lambda root=None: seen.append(("regenerate",)) or [])
        assert run_main("--root", str(source), "--diff")[0] == 0
        assert seen == [("reconcile", True), ("projects", True), ("sweep", True)]


# ---------------------------------------------------------------------------
# a whole run against a redirected home
# ---------------------------------------------------------------------------


class TestSyncRunTouchesOnlyTheRedirectedHome:
    """With STRATARC_HOME naming a fixture home and HOME naming a decoy, a whole `python -m stratarc.sync` run (the children included) reads and writes under the fixture and nothing under HOME. The apply run is the discriminating one: a sync that ignored STRATARC_HOME would write runtime output into the decoy and leave the fixture home empty."""

    @pytest.fixture
    def layout(self, tmp_path: Path):
        base = (tmp_path / "run").resolve()
        base.mkdir()
        source, home, decoy = base / "source", base / "fixture-home", base / "decoy-home"
        shutil.copytree(SYNC_SOURCE, source)
        home.mkdir()
        environment = {key: value for key, value in os.environ.items() if key not in {"STRATARC_SOURCE", "LLM_ROOT_PROJECTS_DIR", "STRATARC_ENVIRONMENT"} and not key.startswith("GIT_")}
        environment.update(HOME=str(decoy), STRATARC_HOME=str(home), PYTHONPATH=str(REPO_ROOT))
        # The decoy stands for the real home: a runtime directory and a projects tree that a stray default would walk or rewrite.
        (decoy / ".claude").mkdir(parents=True)
        (decoy / ".claude" / "settings.json").write_text('{"decoy": true}\n')
        (decoy / "projects" / "active" / "demo" / ".claude").mkdir(parents=True)
        (decoy / "projects" / "active" / "demo" / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(old *)"]}}\n')
        # A runtime counts as installed when its target directory exists.
        (home / ".claude").mkdir()
        return base, source, home, decoy, environment

    def run(self, base: Path, source: Path, environment: dict, *flags: str):
        return subprocess.run(
            [sys.executable, "-m", "stratarc.sync", *flags, "--root", str(source)],
            capture_output=True,
            text=True,
            cwd=base,
            env=environment,
            timeout=120,
        )

    def test_a_check_run_never_touches_the_decoy_home(self, layout) -> None:
        base, source, home, decoy, environment = layout
        decoy_before, home_before = tree_snapshot(decoy), tree_snapshot(home)
        result = self.run(base, source, environment, "--check")
        assert result.returncode == 1, result.stdout + result.stderr
        assert tree_snapshot(decoy) == decoy_before
        assert tree_snapshot(home) == home_before
        assert {path.name for path in base.iterdir()} == {"source", "fixture-home", "decoy-home"}

    def test_a_diff_run_never_touches_either_home(self, layout) -> None:
        base, source, home, decoy, environment = layout
        decoy_before, home_before, source_before = tree_snapshot(decoy), tree_snapshot(home), tree_snapshot(source)
        result = self.run(base, source, environment, "--diff")
        assert result.returncode == 0, result.stdout + result.stderr
        assert tree_snapshot(decoy) == decoy_before
        assert tree_snapshot(home) == home_before
        assert tree_snapshot(source) == source_before

    def test_an_apply_run_deploys_under_the_fixture_home_and_not_the_decoy(self, layout) -> None:
        base, source, home, decoy, environment = layout
        decoy_before = tree_snapshot(decoy)
        result = self.run(base, source, environment)
        assert result.returncode == 0, result.stdout + result.stderr
        # The redirect is proved: runtime output and the deploy stamp landed under the fixture home.
        assert [path for path in (home / ".claude").rglob("*") if path.is_file()], result.stdout + result.stderr
        assert (home / ".claude" / ".stratarc-deploy.json").is_file()
        # The decoy, which a sync ignoring STRATARC_HOME would have deployed into, is byte-identical.
        assert tree_snapshot(decoy) == decoy_before
        assert {path.name for path in base.iterdir()} == {"source", "fixture-home", "decoy-home"}
        # A second run finds nothing to do.
        again = self.run(base, source, environment, "--check")
        assert again.returncode == 0, again.stdout + again.stderr

    def test_the_module_runs_as_a_script_entry_point(self, layout) -> None:
        base, source, _home, _decoy, environment = layout
        result = self.run(base, source, environment, "--list")
        assert result.returncode == 0
        assert "claude" in result.stdout
