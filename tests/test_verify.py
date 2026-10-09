"""Tests for the recursive write-back test.

Every home and source root is a temporary tree. A real sync deploys the synthetic source root into a temporary home, then verify compares what the adapters render with what that sync left.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from stratarc import changelog, projects, sync, verify
from tests.conftest import tree_snapshot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "verify" / "source"


@pytest.fixture(autouse=True)
def isolated(stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("STRATARC_ENVIRONMENT", "STRATARC_SOURCE", "LLM_ROOT_PROJECTS_DIR", "STRATARC_LOG"):
        monkeypatch.delenv(name, raising=False)
    return stratarc_home


@pytest.fixture
def deployed(tmp_path: Path, stratarc_home: Path) -> tuple[Path, Path]:
    """A source root and the home a real sync deployed it into: (root, target)."""
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    target = stratarc_home / ".claude"
    target.mkdir()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sync.main(["--root", str(root), "--only", "claude"])
    assert code == 0, err.getvalue()
    return root, target


def by_file(report: verify.Report) -> dict[str, dict]:
    return {entry["file"]: entry for entry in report.files if entry["area"] == "runtime"}


def test_a_fresh_deploy_verifies_and_exits_zero(deployed) -> None:
    root, target = deployed
    report = verify.run("all", root=root)
    assert report.exit_code == 0
    files = by_file(report)
    assert files, report.notes
    assert {e["status"] for e in files.values()} == {"verified"}
    assert "AGENTS.md" in files
    assert files["AGENTS.md"]["before_digest"] == files["AGENTS.md"]["after_digest"] != ""


def test_a_modified_deployed_file_is_drift_with_exit_six(deployed) -> None:
    root, target = deployed
    deployed_rule = target / "AGENTS.md"
    deployed_rule.write_text("edited by hand\n")
    report = verify.run("all", root=root)
    assert report.exit_code == 6
    entry = by_file(report)["AGENTS.md"]
    assert entry["status"] == "drift"
    assert entry["before_digest"] != entry["after_digest"]
    others = [e for f, e in by_file(report).items() if f != "AGENTS.md"]
    assert all(e["status"] == "verified" for e in others)


def test_a_deleted_deployed_file_is_drift(deployed) -> None:
    root, target = deployed
    (target / "AGENTS.md").unlink()
    report = verify.run("all", root=root)
    assert report.exit_code == 6
    entry = by_file(report)["AGENTS.md"]
    assert entry["status"] == "drift" and "missing" in entry["detail"]


def test_a_file_in_a_directory_the_render_does_not_manage_is_not_drift(deployed) -> None:
    """Drift is what a sync would change; an unmanaged file is `sync --prune` territory."""
    root, target = deployed
    (target / "agents").mkdir()
    (target / "agents" / "stray.md").write_text("not in the source\n")
    report = verify.run("all", root=root)
    assert report.exit_code == 0
    assert "agents/stray.md" not in by_file(report)


def test_verify_writes_nothing_to_the_deployed_tree_or_the_source(deployed, isolated: Path) -> None:
    root, target = deployed
    (target / "AGENTS.md").write_text("drifted\n")
    (target / "agents").mkdir()
    (target / "agents" / "stray.md").write_text("stray\n")
    before_target, before_source = tree_snapshot(target), tree_snapshot(root)
    verify.run("all", root=root)
    verify.run("all", root=root)
    assert tree_snapshot(target) == before_target
    assert tree_snapshot(root) == before_source
    outside = {p for p in isolated.iterdir() if p.name not in (".claude", ".stratarc")}
    assert not outside


def test_the_report_is_written_under_state_and_read_back(deployed, isolated: Path) -> None:
    root, _ = deployed
    report = verify.run("all", root=root)
    state = isolated / ".stratarc" / "state"
    assert (state / "verify-last.json").is_file()
    assert (state / "verify" / f"{report.id}.json").is_file()
    assert verify.last()["id"] == report.id
    assert verify.show(report.id)["result"] == "verified"
    assert verify.show() == verify.last()
    assert verify.show("ver-20000101000000-abc") is None
    with pytest.raises(verify.VerifyError):
        verify.show("../escape")


def test_with_no_report_last_is_none(isolated: Path) -> None:
    assert verify.last() is None


def test_each_file_is_recorded_in_the_log(deployed, monkeypatch: pytest.MonkeyPatch) -> None:
    root, target = deployed
    monkeypatch.setenv("STRATARC_LOG", "1")
    (target / "AGENTS.md").write_text("drifted\n")
    report = verify.run("all", root=root)
    rows = changelog.query({"source_ref": report.id}, limit=None)
    assert len(rows) == len([e for e in report.files if e["status"] != "skipped"])
    drifted = [r for r in rows if r["status"] == "drift"]
    assert [r["file"] for r in drifted] == ["AGENTS.md"]
    assert {r["status"] for r in rows} == {"verified", "drift"}
    assert "stratarc verify run" in changelog.human_log_path().read_text()


def test_a_change_scope_sets_the_status_of_that_change(deployed, monkeypatch: pytest.MonkeyPatch) -> None:
    root, target = deployed
    monkeypatch.setenv("STRATARC_LOG", "1")
    change_id = changelog.record({"command": "sync apply", "file": "AGENTS.md", "layer": "base", "status": "written"})
    verify.run(f"change:{change_id}", root=root)
    assert changelog.get(change_id)["status"] == "verified"
    (target / "AGENTS.md").write_text("drifted\n")
    report = verify.run(f"change:{change_id}", root=root)
    assert report.exit_code == 6
    assert changelog.get(change_id)["status"] == "drift"
    caused = changelog.effects(change_id)
    assert caused and all(c["cause_id"] == change_id for c in caused)
    with pytest.raises(verify.VerifyError):
        verify.run("change:chg-missing", root=root)


def test_a_project_scope_walks_into_the_project_and_restores_the_module(deployed, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = deployed
    seen: list[tuple[str, bool]] = []

    def fake_sync_project(name: str, dry: bool, cp=None) -> list[str]:
        seen.append((name, dry))
        return [f"copy /x/{name}/a.md -> /checkout/{name}/.claude/rules/a.md"]

    monkeypatch.setattr(projects, "sync_project", fake_sync_project)
    monkeypatch.setattr(verify, "_project_names", lambda r, cp: ["notes-cli", "other"])
    before = (projects.ROOT, projects._ROOT_CONFIGURED)
    report = verify.run("project:notes-cli", root=root)
    assert seen == [("notes-cli", True)]
    assert report.exit_code == 6
    assert {(e["area"], e["name"], e["file"], e["status"]) for e in report.files} == {("project", "notes-cli", "/checkout/notes-cli/.claude/rules/a.md", "drift")}
    assert (projects.ROOT, projects._ROOT_CONFIGURED) == before
    with pytest.raises(verify.VerifyError):
        verify.run("project:nope", root=root)


def test_scope_all_walks_into_every_managed_project(deployed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = deployed
    projects_dir = tmp_path / "projects"
    checkout = projects_dir / "active" / "notes-cli"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(projects_dir))
    source = root / "projects-root" / "notes-cli"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_text("# notes-cli\n", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sync.main(["--root", str(root)])
    assert code == 0, err.getvalue()
    assert (checkout / "AGENTS.md").is_file(), out.getvalue()
    clean = verify.run("all", root=root)
    assert {(e["area"], e["name"], e["status"]) for e in clean.files if e["area"] == "project"} == {("project", "notes-cli", "verified")}
    (checkout / "AGENTS.md").write_text("# edited by hand\n", encoding="utf-8")
    drifted = verify.run("all", root=root)
    assert drifted.exit_code == 6
    assert any(e["area"] == "project" and e["name"] == "notes-cli" and e["status"] == "drift" for e in drifted.files)


def test_a_skipped_project_is_not_drift(deployed, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = deployed
    monkeypatch.setattr(projects, "sync_project", lambda name, dry, cp=None: ["skip notes-cli: no checkout"])
    monkeypatch.setattr(verify, "_project_names", lambda r, cp: ["notes-cli"])
    report = verify.run("project:notes-cli", root=root)
    assert report.exit_code == 0
    assert report.counts["skipped"] == 1


def test_an_unknown_scope_is_refused(deployed) -> None:
    root, _ = deployed
    with pytest.raises(verify.VerifyError):
        verify.run("everything", root=root)
    with pytest.raises(verify.VerifyError):
        verify.run("project:", root=root)


def test_no_installed_runtime_is_a_note_not_drift(tmp_path: Path, isolated: Path) -> None:
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    report = verify.run("all", root=root)
    assert report.exit_code == 0
    assert any("no installed runtime" in n for n in report.notes)


def test_format_report_lists_drift_only_unless_verbose(deployed) -> None:
    root, target = deployed
    (target / "AGENTS.md").write_text("drifted\n")
    data = verify.run("all", root=root).to_dict()
    brief = verify.format_report(data)
    assert "drift" in brief and "AGENTS.md" in brief
    assert brief.count("\n") < verify.format_report(data, verbose=True).count("\n")
    json.dumps(data)
