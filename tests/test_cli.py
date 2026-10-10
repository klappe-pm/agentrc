from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stratarc import __version__, cli, messages, paths
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


def test_flags_restore_the_values_the_environment_already_held(tmp_path, monkeypatch):
    held = {"STRATARC_SOURCE": "/held/source", "STRATARC_HOME": "/held/home", "LLM_ROOT_PROJECTS_DIR": "/held/projects", "STRATARC_GITHUB_OWNER": "held"}
    for name, value in held.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "source"
    root.mkdir()
    other = tmp_path / "other-home"
    other.mkdir()
    seen: dict[str, str | None] = {}

    def capture(_argv=None) -> int:
        seen["owner"] = os.environ.get("STRATARC_GITHUB_OWNER")
        return 0

    monkeypatch.setattr("stratarc.sync.main", capture)

    assert main(["--root", str(root), "--home", str(other), "--projects-root", str(tmp_path), "--owner", "me", "diff"]) == 0

    assert seen["owner"] == "me"
    assert {name: os.environ.get(name) for name in held} == held


def test_the_environment_source_beats_the_registered_source(stratarc_home, source_root, tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    registered = tmp_path / "registered-source"

    assert main(["source", "init", str(registered), "--name", "main", "--use"]) == 0
    capsys.readouterr()
    assert main(["--json", "source", "show"]) == 0

    assert json.loads(capsys.readouterr().out)["data"]["root"] == str(source_root.resolve())


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


def test_every_module_code_resolves_to_a_catalog_message():
    assert set(messages.CODE_MESSAGES.values()) <= set(messages.CATALOG)
    wanted = {
        "list-mode-missing", "mode-invalid", "type-mismatch", "parse-error", "unknown-key", "unknown-project",
        "unknown-agent", "unknown-account", "unknown-runtime", "source-root-missing", "project-required",
        "newer-schema", "provider-unreachable", "adapter-unsupported", "adapter-outdated", "database-locked",
        "drift", "home-unwritable", "backup-missing", "settings-malformed",
        "range-invalid", "manifest-invalid", "manifest-unreadable", "manifest-missing", "adapter-unknown",
        "adapter-exists", "deprecation-reason", "deprecation-date", "deprecation-unknown", "runtime-version",
        "provider-invalid", "provider-name", "provider-unknown", "provider-unreadable", "provider-exists",
        "model-unknown", "invalid-input", "unavailable",
        "invalid-edit", "no-editor", "needs-yes", "source-not-found", "invalid-name", "invalid-value", "invalid-path",
        "invalid-config", "project-exists", "account-exists", "source-exists", "path-exists", "not-reconciled",
        "editor-failed", "ui-extra-missing", "conflict",
        "agent-exists", "relay-cycle", "relay-depth", "relay-invalid", "flag-invalid",
    }
    assert wanted == set(messages.CODE_MESSAGES)


def test_every_module_code_is_raised_by_a_module_and_every_message_has_two_sentences():
    source = "".join(path.read_text(encoding="utf-8") for path in (REPO_ROOT / "stratarc").glob("*.py"))
    for code in messages.CODE_MESSAGES:
        assert f'"{code}"' in source, code
    for message in messages.CATALOG.values():
        assert message.problem.endswith((".", "}")) and message.recovery.endswith("."), message.id
    ids = sorted(messages.CATALOG)
    assert ids == sorted(set(ids)) and ids[-1] == "msg-1160"


def test_a_conflict_code_keeps_its_exit_status():
    for code in ("adapter-exists", "provider-exists", "project-exists", "account-exists", "agent-exists", "source-exists", "path-exists", "conflict"):
        assert messages.from_code(code, "x").exit == messages.CONFLICT, code
    assert messages.from_code("not-reconciled", "x").exit == messages.UNAVAILABLE
    assert messages.from_code("ui-extra-missing", "x").exit == messages.UNAVAILABLE
    assert messages.from_code("editor-failed", "x").exit == messages.FAILURE
    assert messages.from_code("unavailable", "x").exit == messages.UNAVAILABLE
    assert messages.from_code("invalid-input", "x").exit == messages.INVALID_INPUT


def test_from_code_fills_the_detail_and_ignores_an_unknown_code():
    error = messages.from_code("unknown-key", 'No layer sets "x".', param="x")
    assert error is not None and error.id == "msg-1105" and error.param == "x"
    assert error.problem == 'No layer sets the key: No layer sets "x".'
    assert messages.from_code("no-such-code", "x") is None


# ----- the resource commands forward to their modules -----

LAYERS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "layers" / "source"


@pytest.mark.parametrize(
    ("module", "command", "forwarded"),
    [
        ("config_cmd", ["config", "get", "a.b", "--project", "p"], ["get", "a.b", "--project", "p"]),
        ("config_cmd", ["config", "explain", "--tree", "--project", "p"], ["explain", "--tree", "--project", "p"]),
        ("log_cmd", ["log", "show", "--limit", "3"], ["log", "show", "--limit", "3"]),
        ("log_cmd", ["log", "tail"], ["log", "tail"]),
        ("log_cmd", ["verify", "last"], ["verify", "last"]),
        ("providers", ["provider", "list"], ["list"]),
        ("providers", ["provider", "show", "p1"], ["show", "p1"]),
        ("registry", ["adapter", "status"], ["status"]),
        ("registry", ["adapter", "show", "claude"], ["show", "claude"]),
    ],
)
def test_resource_commands_forward_their_arguments(ready, monkeypatch, module, command, forwarded):
    recorder = patch_main(monkeypatch, module)

    assert main(command) == 0
    assert recorder.calls == [forwarded]


def test_the_json_flag_reaches_the_module(ready, monkeypatch):
    recorder = patch_main(monkeypatch, "config_cmd")

    assert main(["--json", "config", "list"]) == 0
    assert recorder.calls == [["list", "--json"]]


def test_global_flags_before_the_resource_set_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("STRATARC_SOURCE", raising=False)
    seen: dict[str, str | None] = {}

    def capture(_argv=None) -> int:
        seen["source"] = os.environ.get("STRATARC_SOURCE")
        return 0

    monkeypatch.setattr("stratarc.config_cmd.main", capture)
    assert main(["--root", str(tmp_path), "config", "list"]) == 0
    assert seen["source"] == str(tmp_path.resolve())
    assert "STRATARC_SOURCE" not in os.environ


@pytest.mark.parametrize(("raw", "expected"), [(0, 0), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (1, 1), (77, 1)])
def test_resource_exit_statuses_map(ready, monkeypatch, raw, expected):
    patch_main(monkeypatch, "providers", raw)

    assert main(["provider", "list"]) == expected


def fake_module(error: dict | None, status: int, *, text_error: str = ""):
    def run(_argv=None) -> int:
        if text_error:
            print(text_error, file=sys.stderr, end="")
        else:
            print(json.dumps({"ok": error is None, "data": None, "error": error}))
        return status

    return run


def test_a_module_code_is_shown_as_its_catalog_message_in_json(ready, monkeypatch, capsys):
    body = {"code": "unknown-key", "message": 'No layer sets "x".', "param": "x", "hint": "module hint"}
    monkeypatch.setattr("stratarc.config_cmd.main", fake_module(body, 2))

    assert main(["--json", "config", "get", "x"]) == 2

    error = json.loads(capsys.readouterr().out)["error"]
    assert error == {
        "code": "msg-1105",
        "message": 'No layer sets the key: No layer sets "x".',
        "param": "x",
        "hint": messages.CATALOG["msg-1105"].recovery,
    }


def test_a_module_code_is_shown_as_its_catalog_message_in_text(ready, monkeypatch, capsys):
    monkeypatch.setattr("stratarc.providers.main", fake_module(None, 5, text_error="error provider-unreachable  The provider p did not answer.\n  Check the endpoint.\n"))

    assert main(["provider", "test", "p"]) == 5

    err = capsys.readouterr().err
    assert err.startswith("error msg-1113  A provider did not answer its test: The provider p did not answer.\n")
    assert "Check the endpoint." not in err and messages.CATALOG["msg-1113"].recovery in err


def test_a_code_without_a_catalog_entry_is_shown_as_the_module_wrote_it(ready, monkeypatch, capsys):
    body = {"code": "module-private-code", "message": "It exists.", "param": "name", "hint": "Remove it."}
    monkeypatch.setattr("stratarc.providers.main", fake_module(body, 4))

    assert main(["--json", "provider", "add", "p", "--endpoint", "https://x.example"]) == 4

    assert json.loads(capsys.readouterr().out)["error"] == body


def test_a_drift_exit_from_verify_prints_the_catalog_message(ready, monkeypatch, capsys):
    patch_main(monkeypatch, "log_cmd", 6)

    assert main(["verify", "run"]) == 6
    assert "error msg-1117" in capsys.readouterr().err


def test_a_locked_database_that_escapes_a_module_is_msg_1116(ready, monkeypatch, capsys):
    from stratarc import changelog

    def locked(_argv=None) -> int:
        raise changelog.DatabaseLocked("database is locked")

    monkeypatch.setattr("stratarc.log_cmd.main", locked)

    assert main(["log", "show"]) == 5
    assert "msg-1116" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["log", "enable"], ["log", "prune", "--before", "2020-01-01"], ["provider", "add", "p", "--endpoint", "https://x.example"], ["adapter", "remove", "x"], ["verify", "run"]])
def test_a_writing_verb_needs_a_writable_home(source_root, tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "absent-home"))
    recorder = Recorder()
    for module in ("log_cmd", "providers", "registry"):
        monkeypatch.setattr(f"stratarc.{module}.main", recorder)

    assert main(command) == 3
    assert "msg-1003" in capsys.readouterr().err
    assert recorder.calls == []


def test_verify_run_needs_a_source_root(stratarc_home, tmp_path, capsys):
    assert main(["--root", str(tmp_path / "absent"), "verify", "run"]) == 2
    assert "msg-1001" in capsys.readouterr().err


def test_a_reading_verb_runs_without_a_writable_home(source_root, tmp_path, monkeypatch):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "absent-home"))
    recorder = patch_main(monkeypatch, "providers")

    assert main(["provider", "list"]) == 0
    assert recorder.calls == [["list"]]


@pytest.mark.parametrize("command", [["config"], ["log"], ["verify"], ["provider"], ["adapter"], ["log", "frobnicate"], ["provider", "add"], ["source"], ["project", "frobnicate"], ["account", "add"]])
def test_json_mode_prints_one_envelope_even_when_the_module_prints_none(ready, capsys, command):
    assert main(["--json", *command]) == 2

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is False and body["data"] is None
    assert body["error"]["code"] == "invalid-input" and body["error"]["message"]
    assert set(body["error"]) == {"code", "message", "param", "hint"}


def test_resource_help_reaches_the_module(capsys):
    assert main(["config", "--help"]) == 0
    assert "stratarc config" in capsys.readouterr().out


# ----- the resource commands against their real modules -----


def test_config_get_resolves_a_key_and_names_an_unknown_one(stratarc_home, capsys):
    assert main(["--root", str(LAYERS_FIXTURE), "config", "get", "permissions.timeout", "--project", "notes"]) == 0
    assert capsys.readouterr().out == "60\n"

    assert main(["--root", str(LAYERS_FIXTURE), "config", "get", "permissions.nope"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error msg-1105") and "stratarc config list" in err


def test_config_json_error_carries_the_catalog_id(stratarc_home, capsys):
    assert main(["--json", "--root", str(LAYERS_FIXTURE), "config", "list", "--project", "nomode"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["error"]["code"] == "msg-1101"
    assert body["data"]["values"]["permissions.timeout"] == 30


def test_config_with_a_missing_root_is_msg_1110(stratarc_home, tmp_path, capsys):
    assert main(["--root", str(tmp_path / "absent"), "config", "list"]) == 2
    assert "msg-1110" in capsys.readouterr().err


def test_provider_and_adapter_lists_run_for_real(ready, capsys):
    assert main(["provider", "list"]) == 0
    assert "No providers" in capsys.readouterr().out

    assert main(["--json", "adapter", "list"]) == 0
    names = [a["name"] for a in json.loads(capsys.readouterr().out)["data"]["adapters"]]
    assert names == ["claude", "codex", "cursor", "gemini", "opencode"]


def test_log_enable_show_and_verify_last_run_for_real(ready, capsys):
    assert main(["log", "enable"]) == 0
    assert "enabled" in capsys.readouterr().out
    assert main(["--json", "log", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["enabled"] is True
    assert main(["verify", "last"]) == 5
    assert "no verify report" in capsys.readouterr().err


# ----- sync flags and doctor flags -----


def test_sync_forwards_verify_and_rollback_flags(ready, monkeypatch):
    recorder = patch_main(monkeypatch, "sync")

    assert main(["sync", "apply", "--verify"]) == 0
    assert main(["sync", "--rollback-on-drift"]) == 0
    assert recorder.calls == [["--verify"], ["--rollback-on-drift"]]


def test_a_sync_drift_status_from_verify_is_kept(ready, monkeypatch):
    patch_main(monkeypatch, "sync", 6)

    assert main(["sync", "--verify"]) == 6


def test_doctor_permissions_lists_every_command(stratarc_home, source_root, capsys):
    assert main(["--json", "doctor", "--permissions"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]["permissions"]
    commands = {row["command"] for row in rows}
    assert {"sync", "check", "diff", "provider test", "verify run", "doctor --clean --yes"} <= commands
    assert [row["command"] for row in rows if row["network"]] == ["provider test"]

    assert main(["doctor", "--permissions"]) == 0
    assert "permissions: what each command may touch" in capsys.readouterr().out


def make_backups(stratarc_home: Path, count: int) -> list[Path]:
    directory = stratarc_home / ".stratarc" / "backups" / "settings"
    directory.mkdir(parents=True)
    made = []
    for n in range(count):
        path = directory / f"20200101T000000{n:06d}Z"
        path.write_text(str(n))
        made.append(path)
    return made


def test_doctor_clean_lists_and_removes_only_with_yes(stratarc_home, source_root, capsys):
    made = make_backups(stratarc_home, 22)

    assert main(["--json", "doctor", "--clean"]) == 0
    report = json.loads(capsys.readouterr().out)["data"]["clean"]
    assert report["applied"] is False and len(report["would_remove"]) == 2 and report["kept"] == 20
    assert all(path.exists() for path in made)

    assert main(["--json", "doctor", "--clean", "--yes"]) == 0
    report = json.loads(capsys.readouterr().out)["data"]["clean"]
    assert report["applied"] is True and len(report["removed"]) == 2
    assert [path.exists() for path in made] == [False, False] + [True] * 20


def test_doctor_clean_never_lists_user_data(stratarc_home, source_root, capsys):
    from stratarc import home_layout

    home_layout.ensure_layout()
    (home_layout.providers_dir() / "p.json").write_text("{}")

    assert main(["--json", "doctor", "--clean", "--yes"]) == 0
    assert (home_layout.providers_dir() / "p.json").is_file()
    assert (stratarc_home / ".stratarc" / "config.toml").is_file()


def test_doctor_report_writes_a_redacted_private_bundle(stratarc_home, source_root, monkeypatch, capsys):
    secret = "ghp_" + "a1B2c3D4e5" * 4
    monkeypatch.setenv("STRATARC_NOTE", secret)

    assert main(["--json", "doctor", "--report"]) == 0

    path = Path(json.loads(capsys.readouterr().out)["data"]["report"])
    assert path.parent == stratarc_home / ".stratarc" / "state" / "debug"
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text(encoding="utf-8")
    assert secret not in text and "[REDACTED" in text
    assert str(stratarc_home) not in text
    assert json.loads(text)["stratarc"] == __version__


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_doctor_report_in_an_unwritable_home_is_msg_1118(stratarc_home, source_root, capsys):
    stratarc_home.chmod(0o500)
    try:
        assert main(["doctor", "--report"]) == 3
    finally:
        stratarc_home.chmod(0o700)
    assert "msg-1118" in capsys.readouterr().err


# ----- the remaining module codes resolve at the command line -----


def test_an_unknown_provider_is_msg_1133_with_the_module_text_inside(ready, capsys):
    assert main(["provider", "show", "nope"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error msg-1133  The provider is not registered: ")
    assert "The provider nope is not registered." in err
    assert "stratarc provider list" in err and "hint" not in err


def test_an_unknown_adapter_json_error_carries_the_catalog_id(ready, capsys):
    assert main(["--json", "adapter", "show", "nope"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "msg-1125" and error["hint"] == messages.CATALOG["msg-1125"].recovery
    assert "The adapter nope is not registered." in error["message"]


def test_a_bad_runtime_version_is_msg_1130(ready, capsys):
    assert main(["adapter", "status", "--runtime-version", "oops"]) == 2
    assert capsys.readouterr().err.startswith("error msg-1130")


def test_an_invalid_generic_input_is_msg_1137(ready, capsys):
    assert main(["verify", "show", "bad-id"]) == 2
    assert "error msg-1137" in capsys.readouterr().err


# ----- the api resource -----


def test_api_schema_prints_the_published_schema(ready, capsys):
    assert main(["api", "schema"]) == 0
    assert json.loads(capsys.readouterr().out)["$schema"]


def test_api_json_flag_adds_no_envelope_to_the_schema(ready, capsys):
    assert main(["--json", "api", "schema"]) == 0
    assert "$schema" in json.loads(capsys.readouterr().out)


def test_api_serve_forwards_its_arguments_and_the_global_root(ready, monkeypatch):
    recorder = patch_main(monkeypatch, "api", 0)
    seen: dict[str, str] = {}

    def record(argv=None) -> int:
        seen["root"] = os.environ.get("STRATARC_SOURCE", "")
        return recorder(argv)

    monkeypatch.setattr("stratarc.api.main", record)
    assert main(["--root", str(ready), "api", "serve", "--port", "0"]) == 0
    assert recorder.calls == [["serve", "--port", "0"]]
    assert seen["root"] == str(ready.resolve())


def test_api_serve_in_an_unwritable_home_is_denied_before_it_binds(ready, monkeypatch, capsys):
    recorder = patch_main(monkeypatch, "api", 0)
    monkeypatch.setattr(cli, "_require_writable_home", lambda: (_ for _ in ()).throw(messages.CliError("msg-1003", param="home", path="~")))
    assert main(["api", "serve"]) == 3
    assert recorder.calls == [] and "msg-1003" in capsys.readouterr().err


def test_api_serve_with_a_bad_port_is_the_catalog_invalid_input(ready, capsys):
    assert main(["api", "serve", "--port", "70000"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error msg-1137") and "--port must be between 0 and 65535" in err


def test_api_serve_with_a_missing_source_root_is_msg_1110(stratarc_home, tmp_path, capsys):
    assert main(["--root", str(tmp_path / "absent"), "api", "serve", "--port", "0"]) == 2
    assert "error msg-1110" in capsys.readouterr().err


def test_api_json_error_is_one_catalog_envelope(ready, capsys):
    assert main(["--json", "api", "serve", "--port", "70000"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["error"]["code"] == "msg-1137"


def test_api_exit_statuses_pass_through(ready, monkeypatch):
    for status in (5, 130):
        patch_main(monkeypatch, "api", status)
        assert main(["api", "serve", "--port", "0"]) == status
    patch_main(monkeypatch, "api", 9)
    assert main(["api", "serve", "--port", "0"]) == 1


def test_help_lists_the_api_command(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert re.search(r"\bapi\b", capsys.readouterr().out)


# ----- the source, project, runtime, agent and account resources -----


@pytest.mark.parametrize(
    ("command", "forwarded"),
    [
        (["source", "list"], ["source", "list"]),
        (["source", "use", "main"], ["source", "use", "main"]),
        (["project", "add", "notes", "--dry-run"], ["project", "add", "notes", "--dry-run"]),
        (["runtime", "target", "claude", "~/.claude-x"], ["runtime", "target", "claude", "~/.claude-x"]),
        (["agent", "show", "writer", "--project", "notes"], ["agent", "show", "writer", "--project", "notes"]),
        (["account", "add", "work", "--set", "permissions.timeout=60"], ["account", "add", "work", "--set", "permissions.timeout=60"]),
        (["--json", "account", "list"], ["account", "list", "--json"]),
    ],
)
def test_resource_verbs_reach_resources_cmd_prefixed_with_the_resource(ready, monkeypatch, command, forwarded):
    recorder = patch_main(monkeypatch, "resources_cmd")

    assert main(command) == 0
    assert recorder.calls == [forwarded]


@pytest.mark.parametrize("command", [["source", "use", "x"], ["source", "init", "p"], ["project", "add", "x"], ["runtime", "enable", "claude"], ["agent", "edit", "a"], ["account", "remove", "a"]])
def test_a_writing_resource_verb_needs_a_writable_home(source_root, tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "absent-home"))
    recorder = patch_main(monkeypatch, "resources_cmd")

    assert main(command) == 3
    assert "msg-1003" in capsys.readouterr().err
    assert recorder.calls == []


def test_a_dry_run_and_a_reading_verb_need_no_writable_home(source_root, tmp_path, monkeypatch):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "absent-home"))
    recorder = patch_main(monkeypatch, "resources_cmd")

    assert main(["project", "add", "x", "--dry-run"]) == 0
    assert main(["project", "list"]) == 0
    assert len(recorder.calls) == 2


def test_resource_error_codes_are_shown_as_catalog_messages(ready, monkeypatch, capsys):
    cases = {
        "needs-yes": ("msg-1141", 2), "invalid-edit": ("msg-1139", 2), "no-editor": ("msg-1140", 2), "source-not-found": ("msg-1142", 2),
        "invalid-name": ("msg-1143", 2), "invalid-value": ("msg-1144", 2), "invalid-path": ("msg-1145", 2), "invalid-config": ("msg-1146", 2),
        "project-exists": ("msg-1147", 4), "account-exists": ("msg-1148", 4), "source-exists": ("msg-1149", 4), "path-exists": ("msg-1150", 4),
        "not-reconciled": ("msg-1151", 5), "editor-failed": ("msg-1152", 1), "conflict": ("msg-1154", 4),
        "unknown-project": ("msg-1106", 2), "unknown-agent": ("msg-1107", 2), "unknown-account": ("msg-1108", 2), "unknown-runtime": ("msg-1109", 2),
        "source-root-missing": ("msg-1110", 2), "home-unwritable": ("msg-1118", 3),
    }
    for code, (message_id, status) in cases.items():
        body = {"code": code, "message": "Module wording.", "param": "name", "hint": "Module hint."}
        monkeypatch.setattr("stratarc.resources_cmd.main", fake_module(body, status))
        assert main(["--json", "project", "show", "x"]) == status, code
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == message_id and "Module wording." in error["message"], code
        assert error["hint"] == messages.CATALOG[message_id].recovery


def test_removing_a_project_without_yes_is_msg_1141_for_real(ready, capsys):
    assert main(["project", "add", "notes"]) == 0
    capsys.readouterr()

    assert main(["project", "remove", "notes"]) == 2

    assert "error msg-1141" in capsys.readouterr().err
    assert (ready / "projects-root" / "notes").is_dir()


def test_adding_a_project_twice_is_a_conflict_msg_1147_for_real(ready, capsys):
    assert main(["project", "add", "notes"]) == 0
    capsys.readouterr()

    assert main(["project", "add", "notes"]) == 4
    assert "error msg-1147" in capsys.readouterr().err


def test_source_use_is_read_by_the_next_command_with_no_flag_variable_or_config(stratarc_home, tmp_path, monkeypatch, capsys):
    """The defect: `source use` wrote sources.toml and nothing else read it."""
    monkeypatch.delenv("STRATARC_SOURCE", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    target = tmp_path / "registered-source"

    assert main(["source", "init", str(target), "--name", "main", "--use"]) == 0
    capsys.readouterr()
    assert main(["--json", "source", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["root"] == str(target.resolve())
    assert paths.source_root() == target.resolve()


def test_the_package_installs_exactly_one_console_script_named_stratarc():
    import tomllib

    scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"stratarc": "stratarc.cli:main"}


# ----- the ui command -----


def test_ui_forwards_its_arguments_and_the_global_root(ready, monkeypatch):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: True)
    monkeypatch.setattr("stratarc.ui.interactive", lambda: True)
    seen: dict[str, str] = {}
    recorder = Recorder(0)

    def record(argv=None) -> int:
        seen["root"] = os.environ.get("STRATARC_SOURCE", "")
        return recorder(argv)

    monkeypatch.setattr("stratarc.ui.main", record)
    assert main(["--root", str(ready), "ui"]) == 0
    assert recorder.calls == [[]] and seen["root"] == str(ready.resolve())


def test_ui_passes_a_nonzero_status_through(ready, monkeypatch):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: True)
    monkeypatch.setattr("stratarc.ui.interactive", lambda: True)
    for status in (5, 130):
        patch_main(monkeypatch, "ui", status)
        assert main(["ui"]) == status


def test_ui_without_the_extra_is_msg_1153_and_exit_5(ready, monkeypatch, capsys):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: False)
    recorder = patch_main(monkeypatch, "ui")

    assert main(["ui"]) == 5

    err = capsys.readouterr().err
    assert err.startswith("error msg-1153") and "pip install 'stratarc[ui]'" in err
    assert recorder.calls == []


def test_ui_without_the_extra_is_one_json_envelope(ready, monkeypatch, capsys):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: False)

    assert main(["--json", "ui"]) == 5

    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["error"]["code"] == "msg-1153"


def test_ui_help_works_without_the_extra(monkeypatch, capsys):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: False)

    assert main(["ui", "--help"]) == 0
    assert "stratarc ui" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [False, True], ids=["extra-absent", "extra-present"])
def test_ui_on_a_missing_source_root_is_msg_1001_and_exit_2_without_opening_the_screen(stratarc_home, tmp_path, monkeypatch, capsys, extra):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: extra)
    monkeypatch.setattr("stratarc.ui.interactive", lambda: False)
    recorder = patch_main(monkeypatch, "ui")
    missing = tmp_path / "no-such-root"

    assert main(["ui", "--root", str(missing)]) == 2

    err = capsys.readouterr().err
    assert err.startswith("error msg-1001") and "does not exist" in err
    assert recorder.calls == []


def test_ui_on_a_missing_source_root_is_one_json_envelope(stratarc_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: True)
    monkeypatch.setattr("stratarc.ui.interactive", lambda: False)

    assert main(["--json", "ui", "--root", str(tmp_path / "no-such-root")]) == 2

    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["error"]["code"] == "msg-1001"


def test_ui_without_the_extra_on_a_good_root_is_still_msg_1153_and_exit_5(ready, monkeypatch, capsys):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: False)
    monkeypatch.setattr("stratarc.ui.interactive", lambda: False)

    assert main(["ui", "--root", str(ready)]) == 5
    assert capsys.readouterr().err.startswith("error msg-1153")


def test_ui_without_a_terminal_is_msg_1160_and_exit_5_instead_of_hanging(ready, monkeypatch, capsys):
    monkeypatch.setattr("stratarc.ui.textual_available", lambda: True)
    monkeypatch.setattr("stratarc.ui.interactive", lambda: False)
    recorder = patch_main(monkeypatch, "ui")

    assert main(["ui", "--root", str(ready)]) == 5

    err = capsys.readouterr().err
    assert err.startswith("error msg-1160") and "terminal" in err
    assert recorder.calls == []


def test_ui_probe_reads_both_standard_streams(monkeypatch):
    from stratarc import ui

    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    for stdin, stdout, expected in ((True, True, True), (False, True, False), (True, False, False), (False, False, False)):
        monkeypatch.setattr("sys.stdin", Stream(stdin))
        monkeypatch.setattr("sys.stdout", Stream(stdout))
        assert ui.interactive() is expected
