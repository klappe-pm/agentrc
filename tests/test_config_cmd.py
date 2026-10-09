from __future__ import annotations

import json
from pathlib import Path

import pytest

from stratarc.config_cmd import main

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "layers" / "source"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "notes-cli" / "source"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, stratarc_home):
    import os

    for name in [n for n in os.environ if n.startswith("STRATARC_") and n != "STRATARC_HOME"]:
        monkeypatch.delenv(name)


def run(capsys, *argv, root=FIXTURE):
    code = main(["--root", str(root), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_get_scalar(capsys):
    assert run(capsys, "get", "permissions.timeout", "--project", "notes") == (0, "60\n", "")


def test_get_table_prefix_returns_nested_value(capsys):
    code, out, _ = run(capsys, "get", "permissions.network", "--json")
    assert code == 0
    assert json.loads(out)["data"]["value"] == {"allow": ["api.example.com"], "deny": []}


def test_get_unknown_key_is_invalid_input(capsys):
    code, out, err = run(capsys, "get", "permissions.nope")
    assert (code, out) == (2, "")
    assert err.startswith("error unknown-key")


def test_list_prints_every_key(capsys):
    code, out, _ = run(capsys, "list", "--project", "notes")
    assert code == 0
    assert 'permissions.timeout = 60' in out.splitlines()
    assert 'settings.owner = "example-owner"' in out.splitlines()
    assert out.count("\n") == len(out.splitlines())


def test_list_reports_a_missing_list_mode_and_exits_2(capsys):
    code, out, err = run(capsys, "list", "--project", "nomode")
    assert code == 2
    assert "permissions.network.allow   ERROR list-mode-missing: projects-root/nomode/permissions.json:3:" in out
    assert "permissions.timeout = 30" in out
    assert err.startswith("error list-mode-missing")


def test_explain_human_chain(capsys):
    code, out, _ = run(capsys, "explain", "permissions.network.allow", "--project", "notes", "--agent", "reviewer", "--runtime", "codex")
    assert code == 0
    width = len("projects-root/notes/permissions.json:4")
    assert out.splitlines() == [
        "permissions.network.allow   (project: notes, agent: reviewer, runtime: codex)",
        f'  base     {"permissions.json:4":<{width}}  ["api.example.com"]',
        f'  runtime  {"runtimes/codex.toml:4":<{width}}  mode=extend  + ["registry.example.org"]',
        f'  project  {"projects-root/notes/permissions.json:4":<{width}}  mode=replace  ["api.example.com", "tools.example.net"]',
        f'  agent    {"":<{width}}  (not set)',
        'result: ["api.example.com", "tools.example.net"]   decided by: project (replace)',
    ]


def test_explain_json_envelope(capsys):
    code, out, _ = run(capsys, "explain", "permissions.timeout", "--project", "notes", "--agent", "reviewer", "--json")
    body = json.loads(out)
    assert code == 0 and body["ok"] is True and body["error"] is None
    key = body["data"]["keys"][0]
    assert key["value"] == 90 and key["decided_by"] == {"layer": "agent", "op": "set"}
    assert [(s["layer"], s["file"], s["line"]) for s in key["steps"]] == [
        ("base", "permissions.json", 7),
        ("project", "projects-root/notes/permissions.json", 2),
        ("agent", "projects-root/notes/agents/reviewer.json", 3),
    ]
    assert key["steps"][2]["overrode"] == [{"layer": "base", "value": 30}, {"layer": "project", "value": 60}]


def test_json_error_envelope(capsys):
    code, out, err = run(capsys, "explain", "permissions.network.allow", "--project", "nomode", "--json")
    body = json.loads(out)
    assert (code, err) == (2, "")
    assert body["ok"] is False
    assert set(body["error"]) == {"code", "message", "param", "hint"}
    assert body["error"]["code"] == "list-mode-missing" and body["error"]["param"] == "permissions.network.allow"


def test_unknown_project_json(capsys):
    code, out, _ = run(capsys, "get", "permissions.timeout", "--project", "nope", "--json")
    body = json.loads(out)
    assert code == 2 and body["data"] is None and body["error"]["code"] == "unknown-project"


def test_tree_is_pruned_and_names_file_and_line(capsys):
    code, out, _ = run(capsys, "explain", "--tree", "--project", "notes", "--agent", "reviewer")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "notes   (project: notes)"
    assert "keys inherited unchanged from base" in lines[1]
    assert "  permissions.timeout   90   decided by: agent (set)" in lines
    assert "    project  projects-root/notes/permissions.json:2  set" in lines
    assert "    agent    projects-root/notes/agents/reviewer.json:3  set" in lines
    assert not any(line.startswith("  permissions.defaultMode") for line in lines)


def test_tree_json(capsys):
    code, out, _ = run(capsys, "explain", "--tree", "--project", "notes", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["project"] == "notes"
    assert {k["key"] for k in data["keys"]} == {"permissions.timeout", "permissions.network.allow"}
    assert data["unchanged"] == 5


def test_tree_needs_a_project(capsys):
    code, _, err = run(capsys, "explain", "--tree")
    assert code == 2 and err.startswith("error project-required")


def test_home_is_shown_as_tilde(capsys, stratarc_home, monkeypatch):
    monkeypatch.setenv("STRATARC_SETTINGS__OWNER", f"{stratarc_home}/me")
    code, out, _ = run(capsys, "get", "settings.owner")
    assert (code, out) == (0, "~/me\n")
    code, out, _ = run(capsys, "explain", "settings.owner", "--json")
    assert str(stratarc_home) not in out and "~/me" in out


def test_command_never_writes(capsys, tmp_path):
    before = sorted((p, p.read_bytes()) for p in FIXTURE.rglob("*") if p.is_file())
    for argv in (["list"], ["explain", "--tree", "--project", "notes"], ["get", "permissions.timeout"]):
        run(capsys, *argv)
    assert before == sorted((p, p.read_bytes()) for p in FIXTURE.rglob("*") if p.is_file())


def test_notes_cli_example_key_with_project_overlay(capsys):
    code, out, _ = run(capsys, "explain", "permissions.blockReadsOutsideWorkingDirectories", "--project", "notes-cli", root=EXAMPLE)
    assert code == 0
    assert out.splitlines()[0].startswith("permissions.blockReadsOutsideWorkingDirectories   (project: notes-cli)")
    assert "result: true   decided by: project (set)" in out
    assert "projects-root/notes-cli/permissions.json:5" in out
    assert "  base     permissions.json:6" in out


def test_notes_cli_example_lists_resolve_for_its_project(capsys):
    code, out, err = run(capsys, "list", "--project", "notes-cli", root=EXAMPLE)
    assert code == 0, out + err
    assert "list-mode-missing" not in out + err
