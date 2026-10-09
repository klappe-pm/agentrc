"""Tests for the `log` and `verify` command resources: human output, the JSON envelope and the exit codes."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from stratarc import changelog, log_cmd, sync

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "verify" / "source"


@pytest.fixture(autouse=True)
def isolated(stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("STRATARC_LOG", "STRATARC_ENVIRONMENT", "STRATARC_SOURCE", "LLM_ROOT_PROJECTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    return stratarc_home


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = log_cmd.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def envelope(*argv: str) -> tuple[int, dict]:
    code, out, _ = run(*argv, "--json")
    return code, json.loads(out)


@pytest.fixture
def deployed(tmp_path: Path, stratarc_home: Path) -> tuple[Path, Path]:
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    target = stratarc_home / ".claude"
    target.mkdir()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert sync.main(["--root", str(root), "--only", "claude"]) == 0
    return root, target


def test_enable_and_disable_toggle_the_log() -> None:
    code, out, _ = run("log", "enable")
    assert code == 0 and "enabled" in out and changelog.is_enabled()
    assert (changelog.logs_dir()).is_dir()
    code, out, _ = run("log", "disable")
    assert code == 0 and not changelog.is_enabled()


def test_enable_reports_an_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATARC_LOG", "0")
    code, out, _ = run("log", "enable")
    assert code == 0 and "STRATARC_LOG" in out


def test_show_lists_and_filters_and_the_envelope_is_stable() -> None:
    changelog.record({"command": "sync", "file": "a.json", "status": "failed", "projects": ["notes-cli"]}, enabled=True)
    changelog.record({"command": "sync", "file": "b.json", "projects": ["other"]}, enabled=True)
    code, out, _ = run("log", "show", "--project", "notes-cli")
    assert code == 0 and "a.json" in out and "b.json" not in out
    code, body = envelope("log", "show", "--status", "failed")
    assert code == 0 and body["ok"] is True and body["error"] is None
    assert [c["file"] for c in body["data"]["changes"]] == ["a.json"]
    assert body["data"]["enabled"] is False


def test_show_with_nothing_recorded_says_so() -> None:
    assert run("log", "show")[1].strip() == "no changes recorded"


def test_tail_prints_the_human_log() -> None:
    change_id = changelog.record({"command": "sync", "file": "a.json"})
    code, out, _ = run("log", "tail", "-n", "5")
    assert code == 0 and change_id in out
    assert envelope("log", "tail")[1]["data"]["lines"]


def test_explain_prints_the_story_and_an_unknown_id_exits_two() -> None:
    change_id = changelog.record({"command": "sync apply", "layer": "base"}, enabled=True)
    code, out, _ = run("log", "explain", change_id)
    assert code == 0 and "the base layer" in out
    code, body = envelope("log", "explain", change_id)
    assert body["data"]["change"]["id"] == change_id and body["data"]["story"]
    code, out, err = run("log", "explain", "chg-missing")
    assert code == 2 and "no change chg-missing" in err
    code, body = envelope("log", "explain", "chg-missing")
    assert code == 2 and body["ok"] is False and body["error"]["code"] == "invalid-input"
    assert set(body["error"]) == {"code", "message", "param", "hint"}


def test_export_to_stdout_and_to_a_file(tmp_path: Path) -> None:
    changelog.record({"command": "sync", "file": "a.json"}, enabled=True)
    code, out, _ = run("log", "export", "--format", "csv")
    assert code == 0 and out.startswith("id,ts")
    target = tmp_path / "out.jsonl"
    code, out, _ = run("log", "export", "--output", str(target))
    assert code == 0 and json.loads(target.read_text().splitlines()[0])["target"]["file"] == "a.json"


def test_prune_dry_run_then_real() -> None:
    changelog.record({"ts": "2020-01-01T00:00:00Z", "command": "old"}, enabled=True)
    code, out, _ = run("log", "prune", "--before", "2021-01-01", "--dry-run")
    assert code == 0 and "would remove 1" in out
    code, out, _ = run("log", "prune", "--before", "2021-01-01")
    assert code == 0 and "removed 1" in out
    assert run("log", "prune", "--before", "soon")[0] == 2


def test_verify_run_exits_zero_then_six_on_drift(deployed, tmp_path: Path) -> None:
    root, target = deployed
    code, out, _ = run("verify", "run", "--root", str(root))
    assert code == 0 and "result: verified" in out
    (target / "AGENTS.md").write_text("drifted\n")
    code, out, _ = run("verify", "run", "--root", str(root))
    assert code == 6 and "drift" in out and "AGENTS.md" in out
    code, body = envelope("verify", "run", "--root", str(root))
    assert code == 6 and body["ok"] is False
    assert body["data"]["result"] == "drift" and body["data"]["exit"] == 6
    assert body["error"]["code"] == "drift"


def test_verify_last_and_show_read_the_report(deployed) -> None:
    root, _ = deployed
    assert run("verify", "last")[0] == 5
    code, body = envelope("verify", "last")
    assert code == 5 and body["error"]["code"] == "unavailable"
    run("verify", "run", "--root", str(root))
    code, out, _ = run("verify", "last")
    assert code == 0 and "result: verified" in out
    report_id = envelope("verify", "last")[1]["data"]["id"]
    assert run("verify", "show", report_id, "--verbose")[0] == 0
    assert run("verify", "show", "ver-20000101000000-abc")[0] == 5
    assert run("verify", "show", "bad id")[0] == 2


def test_verify_with_a_bad_scope_exits_two(deployed) -> None:
    root, _ = deployed
    code, out, err = run("verify", "run", "--scope", "everything", "--root", str(root))
    assert code == 2 and "not understood" in err


def test_the_resource_is_required() -> None:
    assert run()[0] == 2
    assert run("nothing")[0] == 2
