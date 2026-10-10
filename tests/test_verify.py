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


def test_a_relative_destination_resolves_under_the_runtime_target(tmp_path: Path) -> None:
    named, unplaced = verify._drifted_paths(["copy commands/commit.md -> prompts/commit.md"], tmp_path)
    assert named == {"prompts/commit.md"}
    assert unplaced == []


RUNTIME_TARGETS = {"claude": ".claude", "codex": ".codex", "gemini": ".gemini", "cursor": ".cursor", "opencode": ".config/opencode"}


def _five_runtime_source(tmp_path: Path, home: Path) -> tuple[Path, dict[str, Path]]:
    """The verify fixture plus a command, an agent and a skill, with all five runtimes enabled under the temporary home."""
    root = tmp_path / "five"
    shutil.copytree(FIXTURE, root)
    (root / "commands").mkdir()
    (root / "commands" / "commit.md").write_text("---\ndescription: commit the work\n---\n\nCommit the work.\n", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents" / "helper.md").write_text("---\nname: helper\ndescription: a helper\ntools: Read\n---\n\nHelp.\n", encoding="utf-8")
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: a demo\n---\n\nDemo.\n", encoding="utf-8")
    (root / "hooks").mkdir()
    (root / "hooks" / "guard.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / "hooks" / "hooks.json").write_text('{"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/guard.sh"}]}]}\n', encoding="utf-8")
    (root / "rules" / "no-agent-attribution.md").write_text("# no-agent-attribution\n\n## binding\n\nKeep authorship human.\n", encoding="utf-8")
    (root / "rules" / "tiers.json").write_text('{"global": ["example.md", "no-agent-attribution.md"]}\n', encoding="utf-8")
    directories = json.dumps([f"~/{rel}" for rel in RUNTIME_TARGETS.values()])
    (root / "permissions.json").write_text('{"schemaVersion": 1, "policyVersion": 1, "allow": [], "deny": [], "ask": [], "runtimeDirectories": ' + directories + "}\n", encoding="utf-8")
    targets = {name: home / rel for name, rel in RUNTIME_TARGETS.items()}
    config = "".join(f'[runtimes.{name}]\nenabled = true\ntarget = "{target}"\n\n' for name, target in targets.items())
    (root / "stratarc.toml").write_text(config, encoding="utf-8")
    for target in targets.values():
        target.mkdir(parents=True)
    return root, targets


def _sync(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sync.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("runtime", sorted(RUNTIME_TARGETS))
def test_every_dry_run_action_line_is_placeable(runtime: str, tmp_path: Path, isolated: Path) -> None:
    """Every file a real run writes is one the dry-run lines or the render's owned set name, and a line naming no file is covered by the owned set."""
    from stratarc.staging import build_stage, cleanup_all
    from stratarc.control_plane import ControlPlane
    from stratarc.validate import bound_source
    import importlib

    root, targets = _five_runtime_source(tmp_path, isolated)
    target = targets[runtime]
    # a first claude-only sync writes the control plane's rows, so the staged set holds the hooks and rules every runtime reads
    assert _sync("--root", str(root), "--only", "claude")[0] == 0
    for path in sorted(targets["claude"].rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    mod = sync.load_runtimes(root=root)[runtime][0]
    adapter = importlib.import_module(mod)
    with bound_source(root):
        stage, _notes = build_stage(root, ControlPlane.load(root / "control-plane.md"), "global")
        try:
            before = tree_snapshot(target)
            actions = verify._sync_call(adapter, runtime, stage, target, root, dry_run=True)
            scratch, owned = verify._owned_files(adapter, runtime, stage, root, [])
            assert scratch is not None
            shutil.rmtree(scratch, ignore_errors=True)
            named, unplaced = verify._drifted_paths(actions, target, owned)
            assert tree_snapshot(target) == before, "a dry run wrote"
            verify._sync_call(adapter, runtime, stage, target, root, dry_run=False)
        finally:
            cleanup_all()
    after = tree_snapshot(target)
    changed = {rel for rel in after if before.get(rel) != after[rel]}
    assert actions and changed
    assert changed <= (named | owned), sorted(changed - (named | owned))
    assert named <= owned | set(before), sorted(named - owned - set(before))
    # a line that names no file stays unplaced here and is covered by the owned set during a rollback
    assert all(line in actions for line in unplaced)


def test_rollback_restores_every_runtime_tree_byte_for_byte(tmp_path: Path, isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, targets = _five_runtime_source(tmp_path, isolated)
    _c, _o, _e = _sync("--root", str(root))
    assert _c == 0, _e + _o
    (root / "commands" / "commit.md").write_text("---\ndescription: commit again\n---\n\nCommit again, changed.\n", encoding="utf-8")
    (root / "commands" / "extra.md").write_text("---\ndescription: new\n---\n\nNew.\n", encoding="utf-8")
    before = {name: tree_snapshot(target) for name, target in targets.items()}

    def drifted(scope="all", *, root=None, record=True, actor_kind="human", actor=""):
        report = verify.Report(id="ver-20260101000000-abc", ts="t", scope=scope, root=str(root))
        report.files.append({"area": "runtime", "name": "codex", "file": "x", "status": verify.DRIFTED, "before_digest": "", "after_digest": "", "detail": "", "target": ""})
        return report

    monkeypatch.setattr(verify, "run", drifted)
    code, _out, err = _sync("--root", str(root), "--rollback-on-drift")
    assert code == 6, err
    for name, target in targets.items():
        assert tree_snapshot(target) == before[name], name


def test_a_line_that_cannot_be_placed_refuses_the_rollback(tmp_path: Path, isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, targets = _five_runtime_source(tmp_path, isolated)
    assert _sync("--root", str(root))[0] == 0
    (root / "AGENTS.md").write_text((root / "AGENTS.md").read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

    def no_render(adapter, name, stage, root, notes):
        notes.append("no render")
        return None, set()

    def drifted(scope="all", *, root=None, record=True, actor_kind="human", actor=""):
        report = verify.Report(id="ver-20260101000000-abc", ts="t", scope=scope, root=str(root))
        report.files.append({"area": "runtime", "name": "codex", "file": "x", "status": verify.DRIFTED, "before_digest": "", "after_digest": "", "detail": "", "target": ""})
        return report

    monkeypatch.setattr(verify, "_owned_files", no_render)
    monkeypatch.setattr(verify, "run", drifted)
    code, _out, err = _sync("--root", str(root), "--rollback-on-drift")
    assert code == 5 and "msg-1119" in err, err


def test_format_report_lists_drift_only_unless_verbose(deployed) -> None:
    root, target = deployed
    (target / "AGENTS.md").write_text("drifted\n")
    data = verify.run("all", root=root).to_dict()
    brief = verify.format_report(data)
    assert "drift" in brief and "AGENTS.md" in brief
    assert brief.count("\n") < verify.format_report(data, verbose=True).count("\n")
    json.dumps(data)
