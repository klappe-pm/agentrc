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


def test_explain_shows_the_relay_after_the_agent_layer(capsys):
    code, out, _ = run(capsys, "explain", "permissions.timeout", "--project", "notes", "--agent", "worker")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "permissions.timeout   (project: notes, agent: worker)"
    agent = next(i for i, line in enumerate(lines) if line.startswith("  agent"))
    assert "projects-root/notes/agents/lead.json:4" in lines[agent] and lines[agent].endswith("120   (via lead)")
    assert lines[agent + 1 :] == [
        "  relay    worker <- lead   inherit: permissions.*   (projects-root/notes/agents/worker.json:2)",
        "  relay    permissions.timeout inherited from lead",
        "  relay    account: work (from lead; worker sets none)",
        "result: 120   decided by: agent (set)",
    ]


def test_explain_says_why_a_key_was_not_inherited(capsys):
    _, out, _ = run(capsys, "explain", "settings.owner", "--project", "notes", "--agent", "worker")
    assert "  relay    lead sets settings.owner but it is not inherited: no relay.inherit pattern matches (inherit: permissions.*)" in out.splitlines()
    assert "result: \"worker-owner\"   decided by: agent (set)" in out
    _, out, _ = run(capsys, "explain", "permissions.timeout", "--project", "notes", "--agent", "quiet")
    assert "  relay    lead sets permissions.timeout but it is not inherited: relay.inherit is empty, so nothing is inherited" in out.splitlines()
    assert "result: 60   decided by: project (set)" in out


def test_explain_relay_json(capsys):
    code, out, _ = run(capsys, "explain", "permissions.timeout", "--project", "notes", "--agent", "worker", "--json")
    data = json.loads(out)["data"]
    assert code == 0
    assert data["relay"]["chain"] == [
        {"agent": "worker", "parent": "lead", "inherit": ["permissions.*"], "file": "projects-root/notes/agents/worker.json", "line": 2}
    ]
    assert data["relay"]["account"] == {"name": "work", "from": "parent", "agent": "lead"}
    key = data["keys"][0]
    assert key["relay"] == {"supplied_by": "lead", "not_inherited": []}
    assert key["steps"][-1]["via"] == "lead"
    _, out, _ = run(capsys, "explain", "settings.owner", "--project", "notes", "--agent", "worker", "--json")
    assert json.loads(out)["data"]["keys"][0]["relay"]["not_inherited"] == [
        {"parent": "lead", "reason": "no relay.inherit pattern matches (inherit: permissions.*)"}
    ]


def test_explain_without_a_relay_prints_no_relay_lines(capsys):
    _, out, _ = run(capsys, "explain", "permissions.timeout", "--project", "notes", "--agent", "reviewer", "--json")
    assert "relay" not in json.loads(out)["data"]
    _, out, _ = run(capsys, "explain", "permissions.timeout", "--project", "notes", "--agent", "reviewer")
    assert "relay" not in out


def test_relay_cycle_is_exit_2_with_the_chain(capsys):
    code, out, err = run(capsys, "get", "permissions.timeout", "--project", "cycle", "--agent", "ping")
    assert (code, out) == (2, "")
    assert err.startswith("error relay-cycle") and "ping -> pong -> ping" in err


def test_tree_shows_relay_edges(capsys):
    code, out, _ = run(capsys, "explain", "--tree", "--project", "notes")
    assert code == 0
    assert "  relay   quiet <- lead   inherit: (none)   projects-root/notes/agents/quiet.json:2" in out.splitlines()
    assert "  relay   worker <- lead   inherit: permissions.*   projects-root/notes/agents/worker.json:2" in out.splitlines()
    _, out, _ = run(capsys, "explain", "--tree", "--project", "notes", "--json")
    edges = json.loads(out)["data"]["relay"]
    assert [(e["agent"], e["parent"], e["inherit"]) for e in edges] == [("quiet", "lead", []), ("worker", "lead", ["permissions.*"])]


# ---- --set, --set-json, --set-mode ----------------------------------------------------------


def test_set_is_a_literal_string_flag_layer(capsys):
    assert run(capsys, "get", "permissions.timeout", "--set", "permissions.timeout=5") == (0, "5\n", "")
    code, out, _ = run(capsys, "get", "permissions.timeout", "--project", "notes", "--set-json", "permissions.timeout=5", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["value"] == 5
    last = data["steps"][-1]
    assert (last["layer"], last["file"], last["line"]) == ("flags", "--set", 0)
    assert last["overrode"] == [{"layer": "base", "value": 30}, {"layer": "project", "value": 60}]


def test_set_works_before_the_action_and_repeats(capsys):
    code, out, _ = run(capsys, "--set", "settings.a=1", "list", "--set", "settings.b=2")
    assert code == 0
    assert 'settings.a = "1"' in out.splitlines() and 'settings.b = "2"' in out.splitlines()


def test_explain_prints_the_flags_layer_last(capsys):
    _, out, _ = run(capsys, "explain", "permissions.timeout", "--project", "notes", "--set", "permissions.timeout=5")
    lines = out.splitlines()
    assert lines[-2].split() == ["flags", "--set", '"5"']
    assert lines[-1] == 'result: "5"   decided by: flags (set)'


def test_a_set_list_needs_set_mode(capsys):
    code, out, err = run(capsys, "get", "permissions.network.allow", "--set-json", 'permissions.network.allow=["x.example"]')
    assert (code, out) == (2, "")
    assert err.startswith("error list-mode-missing") and "--set-mode" in err
    argv = ["get", "permissions.network.allow", "--set-json", 'permissions.network.allow=["x.example"]']
    assert run(capsys, *argv, "--set-mode", "permissions.network.allow=extend")[1] == '["api.example.com", "x.example"]\n'
    assert run(capsys, *argv, "--set-mode", "permissions.network.allow=replace")[1] == '["x.example"]\n'


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["--set", "nokey"], "flag-invalid"),
        (["--set", "=1"], "flag-invalid"),
        (["--set-json", "settings.a={oops"], "flag-invalid"),
        (["--set", "settings.a=1", "--set-mode", "settings.a=extend"], "mode-invalid"),
        (["--set-json", "settings.a=[1]", "--set-mode", "settings.a=append"], "mode-invalid"),
        (["--set-mode", "settings.zzz=extend"], "flag-invalid"),
        (["--set-mode", "nomode"], "flag-invalid"),
    ],
)
def test_bad_flags_are_invalid_input(capsys, argv, code):
    exit_code, out, _ = run(capsys, "list", *argv, "--json")
    body = json.loads(out)
    assert exit_code == 2 and body["ok"] is False and body["error"]["code"] == code and body["error"]["hint"]


def test_flags_never_write(capsys):
    before = sorted((p, p.read_bytes()) for p in FIXTURE.rglob("*") if p.is_file())
    run(capsys, "list", "--set", "settings.a=1")
    assert before == sorted((p, p.read_bytes()) for p in FIXTURE.rglob("*") if p.is_file())


def test_notes_cli_example_lists_resolve_for_its_project(capsys):
    code, out, err = run(capsys, "list", "--project", "notes-cli", root=EXAMPLE)
    assert code == 0, out + err
    assert "list-mode-missing" not in out + err
