"""deploy_guard: the stamp, the version refusal, the branch refusal, the environment's ref and the deploy record."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentrc import deploy_guard

REPO_ROOT = Path(__file__).resolve().parent.parent

ENVIRONMENTS = {
    "version": 1,
    "environments": {
        "ci": {"ref": "main", "description": "fixture CI"},
        "workstation": {"ref": "stable", "description": "fixture workstation"},
    },
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _checkout(root: Path, manifest: dict | None = ENVIRONMENTS) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    if manifest is not None:
        (root / "components.json").write_text(json.dumps(manifest))
    (root / "f").write_text("1")
    _git(root, "add", "-A")
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
    return root


@pytest.fixture(autouse=True)
def no_selected_environment(monkeypatch):
    monkeypatch.delenv(deploy_guard.ENVIRONMENT_VARIABLE, raising=False)


class TestStampAndVersion:
    def test_no_stamp_means_version_zero_and_no_refusal(self, tmp_path):
        assert deploy_guard.read_stamp(tmp_path) is None
        assert deploy_guard.deployed_version(tmp_path) == 0
        assert deploy_guard.refusal(tmp_path, 0) is None
        assert deploy_guard.refusal(tmp_path, 3) is None

    def test_write_then_read_round_trips(self, tmp_path):
        target = tmp_path / "runtime"
        path = deploy_guard.write_stamp(target, 4, tmp_path)
        assert path.name == deploy_guard.stamp_name() == ".agentrc-deploy.json"
        stamp = deploy_guard.read_stamp(target)
        assert stamp["policyVersion"] == 4
        assert stamp["source"] == str(tmp_path)
        assert "written" in stamp
        assert deploy_guard.deployed_version(target) == 4

    def test_older_source_is_refused_and_names_both_versions(self, tmp_path):
        deploy_guard.write_stamp(tmp_path, 5, Path("/checkouts/newer"))
        message = deploy_guard.refusal(tmp_path, 4)
        assert message is not None
        assert "version 5" in message
        assert "version 4" in message
        assert "/checkouts/newer" in message

    def test_equal_or_newer_source_is_allowed(self, tmp_path):
        deploy_guard.write_stamp(tmp_path, 5, tmp_path)
        assert deploy_guard.refusal(tmp_path, 5) is None
        assert deploy_guard.refusal(tmp_path, 6) is None

    def test_unreadable_stamp_counts_as_absent(self, tmp_path):
        (tmp_path / deploy_guard.stamp_name()).write_text("not json")
        assert deploy_guard.read_stamp(tmp_path) is None
        assert deploy_guard.refusal(tmp_path, 1) is None
        (tmp_path / deploy_guard.stamp_name()).write_text(json.dumps({"policyVersion": "9"}))
        assert deploy_guard.deployed_version(tmp_path) == 0


class TestBranchGuard:
    def test_non_checkout_is_never_refused(self, tmp_path):
        assert deploy_guard.branch_refusal(tmp_path) is None
        assert deploy_guard.source_branch(tmp_path) is None
        assert deploy_guard.source_commit(tmp_path) is None

    def test_main_is_allowed_and_a_feature_branch_is_refused(self, tmp_path):
        root = _checkout(tmp_path / "root", manifest=None)
        assert deploy_guard.source_branch(root) == "main"
        assert deploy_guard.branch_refusal(root) is None
        _git(root, "switch", "-q", "-c", "feature")
        message = deploy_guard.branch_refusal(root)
        assert message is not None
        assert "branch feature" in message
        assert "--allow-branch" in message
        assert deploy_guard.branch_refusal(root, allowed=("feature",)) is None
        _git(root, "checkout", "-q", "--detach")
        assert "detached head" in deploy_guard.branch_refusal(root)


class TestEnvironmentRef:
    """Each declared environment names the ref it deploys from; a source that declares none has no branch guard."""

    def test_no_environment_selected_and_none_declared_means_no_guard(self, tmp_path):
        root = _checkout(tmp_path / "root", manifest=None)
        assert deploy_guard.selected_environment(root) is None
        assert deploy_guard.deploy_ref(root) is None

    def test_a_manifest_that_declares_no_environments_means_no_guard(self, tmp_path):
        root = _checkout(tmp_path / "root", manifest={"version": 1})
        assert deploy_guard.deploy_ref(root) is None

    def test_declared_environments_with_none_selected_deploy_main(self, tmp_path):
        root = _checkout(tmp_path / "root")
        assert deploy_guard.selected_environment(root) is None
        assert deploy_guard.deploy_ref(root) == "main"

    def test_the_checkout_setting_selects_an_environment_and_its_ref(self, tmp_path):
        root = _checkout(tmp_path / "root")
        assert deploy_guard.environment_config() == "agentrc.environment"
        _git(root, "config", deploy_guard.environment_config(), "workstation")
        assert deploy_guard.selected_environment(root) == "workstation"
        assert deploy_guard.deploy_ref(root) == "stable"

    def test_the_variable_overrides_the_checkout_setting(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path / "root")
        _git(root, "config", deploy_guard.environment_config(), "workstation")
        monkeypatch.setenv(deploy_guard.ENVIRONMENT_VARIABLE, "ci")
        assert deploy_guard.deploy_ref(root) == "main"

    def test_an_undeclared_environment_is_refused_by_name(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path / "root")
        monkeypatch.setenv(deploy_guard.ENVIRONMENT_VARIABLE, "laptop")
        with pytest.raises(deploy_guard.UnknownEnvironment) as caught:
            deploy_guard.deploy_ref(root)
        assert "laptop" in str(caught.value)
        assert "ci, workstation" in str(caught.value)

    def test_a_selected_environment_without_a_manifest_is_refused(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path / "root", manifest=None)
        monkeypatch.setenv(deploy_guard.ENVIRONMENT_VARIABLE, "workstation")
        with pytest.raises(deploy_guard.UnknownEnvironment):
            deploy_guard.deploy_ref(root)

    def test_a_selected_environment_none_declared_is_refused_when_other_manifest_keys_exist(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path / "root", manifest={"version": 1})
        monkeypatch.setenv(deploy_guard.ENVIRONMENT_VARIABLE, "workstation")
        with pytest.raises(deploy_guard.UnknownEnvironment, match="declared: none"):
            deploy_guard.deploy_ref(root)

    def test_main_prints_the_ref_and_fails_on_an_unknown_environment(self, tmp_path, monkeypatch, capsys):
        root = _checkout(tmp_path / "root")
        monkeypatch.setenv(deploy_guard.ENVIRONMENT_VARIABLE, "workstation")
        assert deploy_guard.main(["ref", "--root", str(root)]) == 0
        assert capsys.readouterr().out.strip() == "stable"
        monkeypatch.setenv(deploy_guard.ENVIRONMENT_VARIABLE, "laptop")
        assert deploy_guard.main(["ref", "--root", str(root)]) == 2
        assert "laptop" in capsys.readouterr().err

    def test_main_prints_nothing_and_exits_zero_when_there_is_no_guard(self, tmp_path, capsys):
        root = _checkout(tmp_path / "root", manifest=None)
        assert deploy_guard.main(["ref", "--root", str(root)]) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")

    def test_main_resolves_the_source_root_when_root_is_omitted(self, tmp_path, source_root, capsys):
        _checkout(source_root)
        assert deploy_guard.main(["ref"]) == 0
        assert capsys.readouterr().out.strip() == "main"

    def test_the_module_runs_as_a_command(self, tmp_path):
        root = _checkout(tmp_path / "root")
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), deploy_guard.ENVIRONMENT_VARIABLE: "workstation"}
        done = subprocess.run(
            [sys.executable, "-m", "agentrc.deploy_guard", "ref", "--root", str(root)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert (done.returncode, done.stdout.strip()) == (0, "stable")


class TestDeployRecord:
    """sync records the commit it last deployed successfully."""

    def test_no_record_reads_as_none(self, tmp_path):
        assert deploy_guard.read_deploy_record(tmp_path) is None

    def test_a_record_names_the_full_commit_branch_and_environment(self, tmp_path):
        home, root = tmp_path / "home", _checkout(tmp_path / "root")
        full = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        path = deploy_guard.write_deploy_record(home, root, "ci")
        assert path == deploy_guard.deploy_record_path(home)
        record = deploy_guard.read_deploy_record(home)
        assert record["commit"] == full
        assert record["branch"] == "main"
        assert record["environment"] == "ci"
        assert record["source"] == str(root)

    def test_a_redeploy_of_the_same_commit_keeps_its_first_deploy_time(self, tmp_path):
        # The soak starts when the commit was first deployed, and a redeploy or a restart never resets it.
        home, root = tmp_path / "home", _checkout(tmp_path / "root")
        deploy_guard.write_deploy_record(home, root, "ci")
        path = deploy_guard.deploy_record_path(home)
        record = json.loads(path.read_text())
        assert record["first_deployed"] == record["written"]
        record["first_deployed"] = record["written"] = "2026-09-01T00:00:00+00:00"
        path.write_text(json.dumps(record))
        deploy_guard.write_deploy_record(home, root, "ci")
        again = deploy_guard.read_deploy_record(home)
        assert again["first_deployed"] == "2026-09-01T00:00:00+00:00"
        assert again["written"] != "2026-09-01T00:00:00+00:00"

    def test_a_record_from_before_first_deployed_existed_keeps_its_written_time(self, tmp_path):
        home, root = tmp_path / "home", _checkout(tmp_path / "root")
        deploy_guard.write_deploy_record(home, root, "ci")
        path = deploy_guard.deploy_record_path(home)
        record = json.loads(path.read_text())
        del record["first_deployed"]
        record["written"] = "2026-09-01T00:00:00+00:00"
        path.write_text(json.dumps(record))
        deploy_guard.write_deploy_record(home, root, "ci")
        assert deploy_guard.read_deploy_record(home)["first_deployed"] == "2026-09-01T00:00:00+00:00"

    def test_a_new_commit_starts_a_new_first_deploy_time(self, tmp_path):
        home, root = tmp_path / "home", _checkout(tmp_path / "root")
        deploy_guard.write_deploy_record(home, root, "ci")
        path = deploy_guard.deploy_record_path(home)
        record = json.loads(path.read_text())
        record["first_deployed"] = "2026-09-01T00:00:00+00:00"
        path.write_text(json.dumps(record))
        (root / "components.json").write_text("{}\n")
        _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qam", "next")
        deploy_guard.write_deploy_record(home, root, "ci")
        after = deploy_guard.read_deploy_record(home)
        assert after["first_deployed"] == after["written"]

    def test_a_tree_outside_git_writes_no_record(self, tmp_path):
        home = tmp_path / "home"
        assert deploy_guard.write_deploy_record(home, tmp_path, None) is None
        assert deploy_guard.read_deploy_record(home) is None

    def test_assume_unchanged_source_cannot_record_head(self, tmp_path):
        home, root = tmp_path / "home", _checkout(tmp_path / "root")
        assert deploy_guard.write_deploy_record(home, root, "ci") is not None
        _git(root, "update-index", "--assume-unchanged", "components.json")
        (root / "components.json").write_text("changed")
        assert deploy_guard.source_is_clean(root) is False
        assert deploy_guard.write_deploy_record(home, root, "ci") is None
        assert deploy_guard.read_deploy_record(home) is None

    def test_an_unreadable_record_reads_as_none(self, tmp_path):
        path = deploy_guard.deploy_record_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        assert deploy_guard.read_deploy_record(tmp_path) is None
        path.write_text(json.dumps({"commit": 7}))
        assert deploy_guard.read_deploy_record(tmp_path) is None
