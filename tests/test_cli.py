from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stratarc import __version__, cli, messages
from stratarc.cli import PASSTHROUGH, TEMPLATE, main
from stratarc.resources import data_dir
from conftest import REPO_ROOT, tree_snapshot


def _template_root() -> Path:
    # Inside a checkout the Traversable is a filesystem path.
    return Path(str(data_dir(TEMPLATE)))


class Recorder:
    """Stands in for an engine module's main: records its argv and returns a chosen status."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.calls: list[list[str]] = []

    def __call__(self, argv=None) -> int:
        self.calls.append(list(argv or []))
        return self.status


@pytest.fixture
def ready(stratarc_home: Path, source_root: Path) -> Path:
    """A writable home and an existing source root, both read through the environment."""
    return source_root


def patch_main(monkeypatch: pytest.MonkeyPatch, module: str, status: int = 0) -> Recorder:
    recorder = Recorder(status)
    monkeypatch.setattr(f"stratarc.{module}.main", recorder)
    return recorder


def test_the_stubs_are_gone() -> None:
    assert not hasattr(cli, "STUBS")


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("init", "doctor", "sync", "check", "diff", "prune", "reconcile", *PASSTHROUGH):
        assert re.search(rf"\b{re.escape(name)}\b", out), name
    assert "--projects-root" in out


def test_unknown_command_exits_2():
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


def test_python_dash_m_entry(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "stratarc", "--root", str(tmp_path / "absent"), "diff"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "msg-1001" in result.stderr


# ----- init -----


def test_init_copies_template_tree(tmp_path: Path):
    template = _template_root()
    assert template.is_dir(), f"template directory missing: {template}"
    target = tmp_path / "source"

    assert main(["init", str(target)]) == 0

    expected = tree_snapshot(template)
    assert expected, "template tree is empty"
    assert tree_snapshot(target) == expected


def test_init_into_existing_empty_dir(tmp_path: Path):
    template = _template_root()
    assert template.is_dir(), f"template directory missing: {template}"
    assert main(["init", str(tmp_path)]) == 0
    assert tree_snapshot(tmp_path) == tree_snapshot(template)


def test_init_refuses_non_empty_target(tmp_path: Path, capsys):
    (tmp_path / "keep.txt").write_text("existing")

    assert main(["init", str(tmp_path)]) == 4
    err = capsys.readouterr().err
    assert "msg-1005" in err and "not empty" in err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]


def test_init_refuses_a_file(tmp_path: Path, capsys):
    target = tmp_path / "file"
    target.write_text("x")

    assert main(["init", str(target)]) == 2
    assert "msg-1006" in capsys.readouterr().err


# ----- the engine commands forward to their modules -----


def test_sync_forwards_its_flags(ready, monkeypatch):
    recorder = patch_main(monkeypatch, "sync")

    assert main(["sync", "--only", "claude", "--allow-branch", "a", "--allow-branch", "b"]) == 0
    assert recorder.calls == [["--only", "claude", "--allow-branch", "a", "--allow-branch", "b"]]


def test_sync_dry_run_and_list_forward(ready, monkeypatch):
    recorder = patch_main(monkeypatch, "sync")

    assert main(["sync", "--dry-run"]) == 0
    assert main(["sync", "--list"]) == 0
    assert recorder.calls == [["--dry-run"], ["--list"]]


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        (["check"], ["--check"]),
        (["diff"], ["--diff"]),
        (["prune"], ["--prune"]),
        (["prune", "--dry-run"], ["--prune", "--dry-run"]),
        (["check", "--only", "codex"], ["--check", "--only", "codex"]),
    ],
)
def test_sync_family_argv(ready, monkeypatch, command, argv):
    recorder = patch_main(monkeypatch, "sync")

    assert main(command) == 0
    assert recorder.calls == [argv]


def test_global_flags_are_accepted_after_the_command(ready, monkeypatch, tmp_path):
    recorder = patch_main(monkeypatch, "sync")

    assert main(["diff", "--home", str(tmp_path), "--json"]) == 0
    assert recorder.calls == [["--diff"]]


def test_reconcile_forwards_check_and_root(ready, monkeypatch):
    recorder = patch_main(monkeypatch, "reconcile")

    assert main(["reconcile"]) == 0
    assert main(["--root", str(ready), "reconcile", "--check"]) == 0
    assert recorder.calls == [[], ["--check", "--root", str(ready)]]


@pytest.mark.parametrize(
    ("module", "command"),
    [
        ("validate", ["validate", "--strict", "--checks", "generic"]),
        ("projects", ["projects", "--only", "notes-cli", "--check"]),
        ("gen_rules_digest", ["gen-rules-digest", "--check", "a.md", "b.md"]),
        ("components", ["components", "--list"]),
    ],
)
def test_passthrough_forwards_everything_after_the_name(ready, monkeypatch, module, command):
    recorder = patch_main(monkeypatch, module)

    assert main(command) == 0
    assert recorder.calls == [command[1:]]


def test_passthrough_help_reaches_the_module(capsys):
    assert main(["validate", "--help"]) == 0
    assert "stratarc validate" in capsys.readouterr().out


# ----- exit statuses -----


@pytest.mark.parametrize(
    ("command", "module", "raw", "expected"),
    [
        (["sync"], "sync", 0, 0),
        (["sync"], "sync", 1, 1),
        (["sync"], "sync", 2, 2),
        (["check"], "sync", 1, 6),
        (["check"], "sync", 2, 2),
        (["diff"], "sync", 0, 0),
        (["reconcile"], "reconcile", 2, 2),
        (["reconcile", "--check"], "reconcile", 1, 6),
        (["projects", "--verify"], "projects", 1, 6),
        (["projects"], "projects", 1, 1),
        (["gen-rules-digest", "--check", "x.md"], "gen_rules_digest", 1, 6),
        (["gen-rules-digest", "--print"], "gen_rules_digest", 1, 1),
        (["validate", "--strict"], "validate", 1, 1),
        (["sync"], "sync", 77, 1),
    ],
)
def test_exit_status_mapping(ready, monkeypatch, command, module, raw, expected):
    patch_main(monkeypatch, module, raw)

    assert main(command) == expected


def test_a_module_that_exits_through_argparse_returns_its_status(ready, monkeypatch):
    def exits(_argv=None):
        raise SystemExit(2)

    monkeypatch.setattr("stratarc.validate.main", exits)

    assert main(["validate", "--bogus"]) == 2


def test_interrupt_exits_130(ready, monkeypatch, capsys):
    def interrupted(_argv=None):
        raise KeyboardInterrupt

    monkeypatch.setattr("stratarc.sync.main", interrupted)

    assert main(["sync"]) == 130


def test_unexpected_error_exits_1_and_hides_the_traceback(ready, monkeypatch, capsys):
    def broken(_argv=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("stratarc.sync.main", broken)

    assert main(["diff"]) == 1
    err = capsys.readouterr().err
    assert "msg-1008" in err and "boom" in err and "Traceback" not in err


def test_debug_prints_the_traceback(ready, monkeypatch, capsys):
    def broken(_argv=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("stratarc.sync.main", broken)

    assert main(["--debug", "diff"]) == 1
    assert "Traceback" in capsys.readouterr().err


# ----- refusals the command line makes itself -----


def test_unknown_source_root_exits_2(stratarc_home, tmp_path, capsys):
    assert main(["--root", str(tmp_path / "absent"), "sync"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error msg-1001")
    assert "stratarc init" in err


def test_invalid_config_exits_2(stratarc_home, source_root, capsys):
    (source_root / "stratarc.toml").write_text("runtimes = [", encoding="utf-8")

    assert main(["check"]) == 2
    assert "msg-1002" in capsys.readouterr().err


def test_unknown_runtime_exits_2(ready, monkeypatch, capsys):
    recorder = patch_main(monkeypatch, "sync")

    assert main(["sync", "--only", "nope"]) == 2
    err = capsys.readouterr().err
    assert "msg-1004" in err and "claude" in err
    assert recorder.calls == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_home_exits_3_for_a_writing_command(ready, stratarc_home, monkeypatch, capsys):
    recorder = patch_main(monkeypatch, "sync")
    stratarc_home.chmod(0o500)
    try:
        assert main(["sync"]) == 3
        assert "msg-1003" in capsys.readouterr().err
        assert main(["diff"]) == 0
    finally:
        stratarc_home.chmod(0o700)
    assert recorder.calls == [["--diff"]]


# ----- global flags -----


def test_flags_set_the_environment_for_the_command_only(tmp_path, monkeypatch):
    for name in ("STRATARC_SOURCE", "STRATARC_HOME", "LLM_ROOT_PROJECTS_DIR", "STRATARC_GITHUB_OWNER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "source"
    root.mkdir()
    seen: dict[str, str | None] = {}

    def capture(_argv=None) -> int:
        for name in ("STRATARC_SOURCE", "STRATARC_HOME", "LLM_ROOT_PROJECTS_DIR", "STRATARC_GITHUB_OWNER"):
            seen[name] = os.environ.get(name)
        return 0

    monkeypatch.setattr("stratarc.sync.main", capture)
    other = tmp_path / "other-home"
    other.mkdir()

    assert main(["--root", str(root), "--home", str(other), "--projects-root", "~/work", "--owner", "me", "diff"]) == 0

    assert seen == {
        "STRATARC_SOURCE": str(root.resolve()),
        "STRATARC_HOME": str(other.resolve()),
        "LLM_ROOT_PROJECTS_DIR": str(Path("~/work").expanduser()),
        "STRATARC_GITHUB_OWNER": "me",
    }
    assert not [name for name in seen if name in os.environ]


def test_importing_the_cli_reads_no_environment(monkeypatch):
    monkeypatch.setenv("STRATARC_HOME", "/nonexistent")
    code = "import stratarc.cli, stratarc.paths as p; print(p.home())"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "STRATARC_HOME": "/first"},
    )
    assert result.stdout.strip() == "/first"


# ----- the JSON envelope -----


def test_json_success_envelope(ready, monkeypatch, capsys):
    class Printing(Recorder):
        def __call__(self, argv=None) -> int:
            print("hello")
            print("warn", file=sys.stderr)
            return super().__call__(argv)

    monkeypatch.setattr("stratarc.sync.main", Printing())

    assert main(["--json", "diff"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body == {
        "ok": True,
        "data": {"command": "diff", "exit": 0, "stdout": "hello\n", "stderr": "warn\n"},
        "error": None,
    }


def test_json_drift_envelope(ready, monkeypatch, capsys):
    patch_main(monkeypatch, "sync", 1)

    assert main(["--json", "check"]) == 6
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["error"]["code"] == "drift"
    assert set(body["error"]) == {"code", "message", "param", "hint"}


def test_json_error_envelope_from_the_catalog(stratarc_home, tmp_path, capsys):
    assert main(["--json", "--root", str(tmp_path / "absent"), "check"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["data"] is None
    assert body["error"]["code"] == "msg-1001"
    assert body["error"]["param"] == "root"
    assert body["error"]["hint"]


# ----- doctor -----


def test_doctor_reports_the_install(stratarc_home, tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    (root / "stratarc.toml").write_text('[runtimes.claude]\nenabled = true\ntarget = "~/.claude"\n\n[runtimes.codex]\nenabled = false\n', encoding="utf-8")
    (stratarc_home / ".claude").mkdir()

    assert main(["--root", str(root), "doctor"]) == 0

    out = capsys.readouterr().out
    assert sys.version.split()[0] in out
    assert f"stratarc      {__version__}" in out
    assert str(stratarc_home) in out and "writable" in out
    assert "from --root" in out
    assert re.search(r"adapters\s+5 registered", out)
    assert re.search(r"start time\s+[\d.]+ ms", out)
    assert re.search(r"claude\s+enabled\s+\S+ \(present\)", out)
    assert re.search(r"codex\s+disabled\s+\S+ \(absent\)", out)
    assert out.rstrip().endswith("ok")


def test_doctor_json_envelope(stratarc_home, source_root, capsys):
    assert main(["--json", "doctor"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and body["error"] is None
    data = body["data"]
    assert data["python"] == sys.version.split()[0]
    assert data["stratarc"] == __version__
    assert data["home"] == {"path": str(stratarc_home), "exists": True, "writable": True}
    assert data["source_root"]["path"] == str(source_root)
    assert data["source_root"]["resolved_from"] == "STRATARC_SOURCE"
    assert data["adapters"] == len(data["runtimes"]) == 5
    assert isinstance(data["start_ms"], float)


def test_doctor_names_a_bad_config(stratarc_home, source_root, capsys):
    (source_root / "stratarc.toml").write_text("name = 3", encoding="utf-8")

    assert main(["--json", "doctor"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["error"]["code"] == "msg-1002"
    assert body["data"]["runtimes"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_doctor_reports_an_unwritable_home(stratarc_home, source_root, capsys):
    stratarc_home.chmod(0o500)
    try:
        assert main(["doctor"]) == 3
    finally:
        stratarc_home.chmod(0o700)
    out = capsys.readouterr().out
    assert "problem msg-1003" in out and "not writable" in out


# ----- the reference page -----


def test_cli_reference_documents_every_command_option_and_message():
    text = (REPO_ROOT / "docs" / "reference" / "cli.md").read_text(encoding="utf-8")
    parser = cli.build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    options = {o for a in parser._actions for o in a.option_strings}
    for name, sub in subparsers.choices.items():
        assert f"\n## {name}\n" in text, f"cli.md has no section for {name}"
        options |= {o for a in sub._actions for o in a.option_strings}
    for option in sorted(options - {"-h", "--help"}):
        assert f"`{option}" in text, f"cli.md does not mention {option}"
    for message_id in messages.CATALOG:
        assert f"`{message_id}`" in text, f"cli.md does not list {message_id}"


# ----- the message catalog -----


def test_every_message_is_well_formed():
    assert messages.CATALOG
    for message_id, message in messages.CATALOG.items():
        assert re.fullmatch(r"msg-\d{4}", message_id)
        assert message.id == message_id
        assert message.exit in {1, 2, 3, 4, 5, 6}
        fields = {name: "x" for text in (message.problem, message.recovery) for name in re.findall(r"{(\w+)}", text)}
        problem, recovery = message.problem.format(**fields), message.recovery.format(**fields)
        assert problem.endswith(("x", ".")) and recovery.endswith(".")
        assert "\n" not in problem + recovery


def test_message_ids_are_unique():
    ids = [m.id for m in messages._MESSAGES]
    assert len(ids) == len(set(ids))


def test_cli_error_formats_its_values():
    error = messages.CliError("msg-1004", param="only", name="zed", known="a, b")
    assert error.id == "msg-1004" and error.exit == 2 and error.param == "only"
    assert error.problem == "The runtime zed is not known."
    assert error.recovery == "Use one of: a, b."
