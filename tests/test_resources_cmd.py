from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import tree_snapshot

from stratarc import resources_cmd, validate
from stratarc.control_plane import ControlPlane
from stratarc.resources_cmd import main, remove_toml_key, set_toml_values

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "resources" / "source"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, stratarc_home):
    import os

    for name in [n for n in os.environ if n.startswith("STRATARC_") and n != "STRATARC_HOME"]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)


@pytest.fixture
def src(tmp_path) -> Path:
    target = tmp_path / "src"
    shutil.copytree(FIXTURE, target)
    return target


def run(capsys, root, *argv, editor=None, environ=None):
    code = main(["--root", str(root), *argv], editor=editor, environ={} if environ is None else environ)
    out = capsys.readouterr()
    return code, out.out, out.err


def run_json(capsys, root, *argv, **kw):
    code, out, err = run(capsys, root, *argv, "--json", **kw)
    return code, json.loads(out), err


def backups(home: Path) -> list[Path]:
    directory = home / ".stratarc" / "backups"
    return sorted(p for p in directory.rglob("*") if p.is_file()) if directory.is_dir() else []


def replacing(old: str, new: str):
    def edit(path: Path) -> None:
        path.write_text(path.read_text().replace(old, new))

    return edit


def writing(text: str):
    def edit(path: Path) -> None:
        path.write_text(text)

    return edit


# ---- the line editor ------------------------------------------------------------------------


def test_set_toml_values_keeps_comments_and_layout():
    text = (FIXTURE / "stratarc.toml").read_text()
    out = set_toml_values(text, ("runtimes", "claude"), {"enabled": "false"})
    assert out == text.replace("enabled = true   # on by default", "enabled = false   # on by default")


def test_set_toml_values_inserts_a_missing_key_after_the_last_key():
    text = '[runtimes.gemini]\nenabled = true\n\n[runtimes.codex]\nenabled = false\n'
    out = set_toml_values(text, ("runtimes", "gemini"), {"target": '"~/.gemini"'})
    assert out == '[runtimes.gemini]\nenabled = true\ntarget = "~/.gemini"\n\n[runtimes.codex]\nenabled = false\n'


def test_set_toml_values_appends_a_missing_table_and_handles_no_trailing_newline():
    out = set_toml_values('owner = "x"', ("runtimes", "gemini"), {"enabled": "true"})
    assert out == 'owner = "x"\n\n[runtimes.gemini]\nenabled = true\n'
    assert set_toml_values("", ("runtimes", "gemini"), {"enabled": "true"}) == "[runtimes.gemini]\nenabled = true\n"


def test_set_toml_values_keeps_crlf_and_a_hash_inside_a_string():
    text = '[runtimes.codex]\r\nenabled = false\r\ntarget = "~/a#b"  # where\r\n'
    out = set_toml_values(text, ("runtimes", "codex"), {"target": '"~/other"', "enabled": "true"})
    assert out == '[runtimes.codex]\r\nenabled = true\r\ntarget = "~/other"  # where\r\n'
    assert tomllib.loads(out)["runtimes"]["codex"] == {"enabled": True, "target": "~/other"}


def test_set_toml_values_only_touches_the_named_table():
    text = '[runtimes.claude]\nenabled = true\n\n[runtimes.codex]\nenabled = true\n'
    out = set_toml_values(text, ("runtimes", "codex"), {"enabled": "false"})
    assert out == '[runtimes.claude]\nenabled = true\n\n[runtimes.codex]\nenabled = false\n'


def test_set_toml_values_sets_a_top_level_key_before_the_first_table():
    text = "# Values tied to the work account.\n[permissions]\ntimeout = 90\n"
    out = set_toml_values(text, (), {"note": '"hello"'})
    assert out == '# Values tied to the work account.\nnote = "hello"\n\n[permissions]\ntimeout = 90\n'
    assert set_toml_values(out, (), {"note": '"bye"'}) == out.replace("hello", "bye")
    assert set_toml_values("", (), {"note": "1"}) == "note = 1\n"


def test_remove_toml_key_keeps_everything_else_and_reports_a_missing_key():
    text = "[permissions]\n# why\ntimeout = 90  # seconds\nretries = 2\n\n[other]\ntimeout = 1\n"
    assert remove_toml_key(text, ("permissions",), "timeout") == "[permissions]\n# why\nretries = 2\n\n[other]\ntimeout = 1\n"
    assert remove_toml_key(text, ("permissions",), "nope") is None
    assert remove_toml_key(text, ("ghost",), "timeout") is None


# ---- runtime --------------------------------------------------------------------------------


def test_runtime_list_and_show(capsys, src):
    code, data, _ = run_json(capsys, src, "runtime", "list")
    assert code == 0
    rows = {r["name"]: r for r in data["data"]["runtimes"]}
    assert rows["claude"]["enabled"] is True and rows["codex"]["enabled"] is False
    assert rows["codex"]["overlay"] == "runtimes/codex.toml"
    assert rows["gemini"]["configured"] is False
    code, out, _ = run(capsys, src, "runtime", "show", "codex")
    assert code == 0 and "defined in: stratarc.toml:9" in out and "overlay: runtimes/codex.toml" in out


def test_runtime_disable_keeps_comments_and_backs_up(capsys, src, stratarc_home):
    before = (src / "stratarc.toml").read_text()
    code, out, _ = run(capsys, src, "runtime", "disable", "claude")
    assert code == 0
    after = (src / "stratarc.toml").read_text()
    assert after == before.replace("enabled = true   # on by default", "enabled = false   # on by default")
    assert "# Source root settings. This comment must survive every edit." in after
    assert "# codex stays off until it is needed" in after
    (backup,) = backups(stratarc_home)
    assert backup.read_text() == before
    assert "backup:" in out


def test_runtime_enable_is_a_no_op_when_already_enabled(capsys, src, stratarc_home):
    code, out, _ = run(capsys, src, "runtime", "enable", "claude")
    assert code == 0 and "already enabled" in out
    assert backups(stratarc_home) == []


def test_runtime_enable_adds_a_missing_table_with_the_default_target(capsys, src):
    code, _, _ = run(capsys, src, "runtime", "enable", "gemini")
    assert code == 0
    text = (src / "stratarc.toml").read_text()
    assert tomllib.loads(text)["runtimes"]["gemini"] == {"enabled": True, "target": "~/.gemini"}
    assert text.startswith((FIXTURE / "stratarc.toml").read_text())


def test_runtime_target_changes_only_the_target(capsys, src):
    before = (src / "stratarc.toml").read_text()
    code, _, _ = run(capsys, src, "runtime", "target", "codex", "~/work/codex")
    assert code == 0
    assert (src / "stratarc.toml").read_text() == before.replace('target = "~/.codex"', 'target = "~/work/codex"')


def test_runtime_target_rejects_an_empty_path(capsys, src):
    code, data, _ = run_json(capsys, src, "runtime", "target", "codex", " ")
    assert (code, data["error"]["code"]) == (2, "invalid-value")


def test_runtime_unknown_is_invalid_input(capsys, src, stratarc_home):
    code, data, _ = run_json(capsys, src, "runtime", "enable", "nope")
    assert code == 2 and data["error"]["code"] == "unknown-runtime"
    assert data["ok"] is False and data["data"] is None
    assert set(data["error"]) == {"code", "message", "param", "hint"}


def test_runtime_dry_run_writes_nothing(capsys, src, stratarc_home, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, data, _ = run_json(capsys, src, "runtime", "disable", "claude", "--dry-run")
    assert code == 0 and data["data"]["file"]["changed"] is True and data["data"]["dry_run"] is True
    assert tree_snapshot(tmp_path) == snapshot


def test_runtime_edit_refuses_a_layout_the_editor_cannot_follow(capsys, src, stratarc_home):
    (src / "stratarc.toml").write_text('[runtimes]\nclaude = { enabled = true }\n')
    before = (src / "stratarc.toml").read_text()
    code, data, _ = run_json(capsys, src, "runtime", "disable", "claude")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert (src / "stratarc.toml").read_text() == before
    assert backups(stratarc_home) == []


def test_runtime_enable_refuses_a_newer_schema_file(capsys, src, stratarc_home):
    (src / "stratarc.toml").write_text("schema_version = 9\n" + (FIXTURE / "stratarc.toml").read_text())
    before = (src / "stratarc.toml").read_bytes()
    code, data, _ = run_json(capsys, src, "runtime", "disable", "claude")
    assert (code, data["error"]["code"]) == (5, "newer-schema")
    assert (src / "stratarc.toml").read_bytes() == before


# ---- project --------------------------------------------------------------------------------


def test_project_list_and_show(capsys, src):
    code, data, _ = run_json(capsys, src, "project", "list")
    assert code == 0
    assert data["data"]["projects"] == [{"name": "notes", "path": "projects-root/notes", "files": 3, "status": "active"}]
    code, data, _ = run_json(capsys, src, "project", "show", "notes")
    assert code == 0
    shown = data["data"]
    assert shown["files"] == ["AGENTS.md", "agents/helper.json", "permissions.json"]
    assert shown["in_control_plane"] is True
    assert shown["enabled_rows"] == ["project:AGENTS.md", "project:rules"]
    assert shown["values"] == {"permissions.timeout": 60}


def test_project_unknown(capsys, src):
    for verb in ("show", "edit", "remove", "enable", "disable"):
        code, data, _ = run_json(capsys, src, "project", verb, "ghost", "--yes") if verb == "remove" else run_json(capsys, src, "project", verb, "ghost")
        assert (code, data["error"]["code"]) == (2, "unknown-project"), verb


def test_project_add_creates_the_folder_and_asks_for_a_reconcile(capsys, src):
    code, data, _ = run_json(capsys, src, "project", "add", "extra")
    assert code == 0
    assert (src / "projects-root" / "extra" / "AGENTS.md").read_text() == "# extra\n"
    assert data["data"]["reconcile_needed"] is True


def test_project_add_opts_in_when_the_column_exists(capsys, src):
    plane = src / "control-plane.md"
    text = plane.read_text().replace("| notes |", "| notes | extra |").replace("|---|---|---|", "|---|---|---|---|")
    plane.write_text(text.replace("| x | x |", "| x | x |  |").replace("|  | x |", "|  | x |  |"))
    code, data, _ = run_json(capsys, src, "project", "add", "extra")
    assert code == 0 and data["data"]["reconcile_needed"] is False
    assert data["data"]["control_plane"]["changed"] == ["project:AGENTS.md", "project:rules"]


def test_project_add_rejects_an_existing_project_and_a_bad_name(capsys, src):
    code, data, _ = run_json(capsys, src, "project", "add", "notes")
    assert (code, data["error"]["code"]) == (4, "project-exists")
    code, data, _ = run_json(capsys, src, "project", "add", "Bad_Name")
    assert (code, data["error"]["code"]) == (2, "invalid-name")


def test_project_add_dry_run_writes_nothing(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, _, _ = run(capsys, src, "project", "add", "extra", "--dry-run")
    assert code == 0 and tree_snapshot(tmp_path) == snapshot


def test_project_remove_needs_yes(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, data, err = run_json(capsys, src, "project", "remove", "notes")
    assert (code, data["error"]["code"]) == (2, "needs-yes")
    assert "--yes" in data["error"]["hint"]
    assert tree_snapshot(tmp_path) == snapshot


def test_project_remove_with_yes_backs_up_and_opts_out(capsys, src, stratarc_home):
    code, data, _ = run_json(capsys, src, "project", "remove", "notes", "--yes")
    assert code == 0
    assert not (src / "projects-root" / "notes").exists()
    assert data["data"]["backups"] == 3 and data["data"]["control_plane"]["changed"] == ["project:AGENTS.md", "project:rules"]
    names = {p.read_text().strip() for p in backups(stratarc_home)}
    assert "# notes\n\nThe notes project.".strip() in names
    plane = (src / "control-plane.md").read_text()
    assert "| [project:AGENTS.md](projects-root/notes/AGENTS.md) |  |  |" in plane
    assert "| notes | active |" in plane  # the manifest is the reconciler's to update


def test_project_remove_dry_run_writes_nothing(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, out, _ = run(capsys, src, "project", "remove", "notes", "--dry-run")
    assert code == 0 and "would remove" in out
    assert tree_snapshot(tmp_path) == snapshot


def test_project_disable_then_enable_round_trips_the_cells(capsys, src, stratarc_home):
    original = (src / "control-plane.md").read_text()
    code, data, _ = run_json(capsys, src, "project", "disable", "notes")
    assert code == 0 and data["data"]["control_plane"]["changed"] == ["project:AGENTS.md", "project:rules"]
    disabled = (src / "control-plane.md").read_text()
    assert disabled != original and "| [rule:style](rules/style.md) | common | x | x |" in disabled
    (backup,) = backups(stratarc_home)
    assert backup.read_text() == original
    code, _, _ = run(capsys, src, "project", "enable", "notes")
    assert code == 0
    assert (src / "control-plane.md").read_text() == original
    code, out, _ = run(capsys, src, "project", "enable", "notes")
    assert code == 0 and "no control-plane cells" in out


def test_project_enable_dry_run_writes_nothing(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, data, _ = run_json(capsys, src, "project", "disable", "notes", "--dry-run")
    assert code == 0 and len(data["data"]["control_plane"]["changed"]) == 2
    assert tree_snapshot(tmp_path) == snapshot


def test_project_disable_and_enable_set_the_manifest_status_in_the_same_write(capsys, src, stratarc_home):
    plane = src / "control-plane.md"
    original = plane.read_text()
    code, data, _ = run_json(capsys, src, "project", "disable", "notes")
    assert code == 0 and data["data"]["reconcile_needed"] is False
    assert data["data"]["control_plane"]["status"] == {"from": "active", "to": "inactive", "changed": True}
    disabled = plane.read_text()
    assert "| notes | inactive | normal | base | example-owner/notes |" in disabled
    assert ControlPlane.load(plane).status("notes") == "inactive"
    (backup,) = backups(stratarc_home)
    assert backup.read_text() == original
    code, data, _ = run_json(capsys, src, "project", "enable", "notes")
    assert code == 0 and data["data"]["control_plane"]["status"] == {"from": "inactive", "to": "active", "changed": True}
    assert plane.read_text() == original
    code, out, _ = run(capsys, src, "project", "enable", "notes")
    assert code == 0 and "no control-plane cells" in out and plane.read_text() == original


def test_project_disable_dry_run_reports_the_status_and_writes_nothing(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, data, _ = run_json(capsys, src, "project", "disable", "notes", "--dry-run")
    assert code == 0 and data["data"]["control_plane"]["status"]["to"] == "inactive"
    assert tree_snapshot(tmp_path) == snapshot


def test_project_disable_records_reconcile_needed_when_the_manifest_has_no_status(capsys, src):
    plane = src / "control-plane.md"
    plane.write_text(plane.read_text().replace("| notes | active | normal | base | example-owner/notes |\n", ""))
    code, data, _ = run_json(capsys, src, "project", "disable", "notes")
    assert code == 0 and data["data"]["reconcile_needed"] is True
    assert data["data"]["control_plane"]["status"] is None
    assert data["data"]["control_plane"]["changed"] == ["project:AGENTS.md", "project:rules"]
    plane.write_text(plane.read_text() + "\n## extra\n\n| project | tier |\n|---|---|\n| notes | normal |\n")
    code, data, _ = run_json(capsys, src, "project", "enable", "notes")
    assert code == 0 and data["data"]["reconcile_needed"] is True


def test_project_enable_without_a_control_plane_column_is_unavailable(capsys, src):
    (src / "projects-root" / "orphan").mkdir()
    code, data, _ = run_json(capsys, src, "project", "enable", "orphan")
    assert (code, data["error"]["code"]) == (5, "not-reconciled")


def test_project_edit_saves_a_valid_edit_with_a_backup(capsys, src, stratarc_home):
    code, data, _ = run_json(capsys, src, "project", "edit", "notes", "--file", "permissions.json", editor=replacing('"timeout": 60', '"timeout": 75'))
    assert code == 0 and data["data"]["file"]["changed"] is True
    assert json.loads((src / "projects-root/notes/permissions.json").read_text()) == {"timeout": 75}
    (backup,) = backups(stratarc_home)
    assert json.loads(backup.read_text()) == {"timeout": 60}


def test_project_edit_rolls_back_invalid_json(capsys, src, stratarc_home):
    path = src / "projects-root/notes/permissions.json"
    before = path.read_bytes()
    code, data, _ = run_json(capsys, src, "project", "edit", "notes", "--file", "permissions.json", editor=writing('{"timeout": '))
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == before
    assert backups(stratarc_home) == []


def test_project_edit_rolls_back_a_schema_violation(capsys, src):
    path = src / "projects-root/notes/permissions.json"
    before = path.read_bytes()
    code, data, _ = run_json(capsys, src, "project", "edit", "notes", "--file", "permissions.json", editor=writing('{"schemaVersion": 1, "allow": "all"}'))
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == before


def test_project_edit_rolls_back_an_invalid_list_mode(capsys, src):
    path = src / "projects-root/notes/permissions.json"
    before = path.read_bytes()
    code, data, _ = run_json(capsys, src, "project", "edit", "notes", "--file", "permissions.json", editor=writing('{"allow": ["a"], "_modes": {"allow": "append"}}'))
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == before


def test_project_edit_defaults_to_the_projects_settings_file_then_agents_md(capsys, src):
    seen = []
    code, _, _ = run(capsys, src, "project", "edit", "notes", editor=lambda p: seen.append(p.name))
    assert code == 0 and seen == ["permissions.json"]


def test_project_edit_rejects_a_path_outside_the_project(capsys, src):
    code, data, _ = run_json(capsys, src, "project", "edit", "notes", "--file", "../../stratarc.toml", editor=writing(""))
    assert (code, data["error"]["code"]) == (2, "invalid-path")


def test_edit_without_an_editor_says_what_to_set(capsys, src):
    code, data, _ = run_json(capsys, src, "project", "edit", "notes")
    assert (code, data["error"]["code"]) == (2, "no-editor")
    assert "$EDITOR" in data["error"]["hint"]


def test_edit_uses_the_editor_variable_and_reports_a_failing_editor(capsys, src):
    environ = {"EDITOR": f"{sys.executable} -c pass"}
    code, out, _ = run(capsys, src, "account", "edit", "work", environ=environ)
    assert code == 0 and "unchanged" in out
    environ = {"EDITOR": f"{sys.executable} -c 'import sys; sys.exit(3)'"}
    code, data, _ = run_json(capsys, src, "account", "edit", "work", environ=environ)
    assert (code, data["error"]["code"]) == (1, "editor-failed")


def test_edit_dry_run_validates_but_saves_nothing(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, out, _ = run(capsys, src, "project", "edit", "notes", "--file", "permissions.json", "--dry-run", editor=replacing("60", "61"))
    assert code == 0 and "would change" in out
    assert tree_snapshot(tmp_path) == snapshot


# ---- agent ----------------------------------------------------------------------------------


def test_agent_list_and_show(capsys, src):
    code, data, _ = run_json(capsys, src, "agent", "list")
    assert code == 0
    rows = {(r["project"], r["name"]): r["files"] for r in data["data"]["agents"]}
    assert rows == {(None, "reviewer"): ["agents/reviewer.md", "agents/reviewer.toml"], ("notes", "helper"): ["projects-root/notes/agents/helper.json"]}
    code, data, _ = run_json(capsys, src, "agent", "show", "reviewer")
    assert code == 0
    assert data["data"]["definition"]["description"] == "Review a diff before it is committed."
    assert data["data"]["values"] == {"permissions.timeout": 15}
    assert [f["kind"] for f in data["data"]["files"]] == ["settings", "definition"]


def test_agent_list_scoped_to_a_project(capsys, src):
    code, data, _ = run_json(capsys, src, "agent", "list", "--project", "notes")
    assert code == 0 and [r["name"] for r in data["data"]["agents"]] == ["reviewer", "helper"]
    code, data, _ = run_json(capsys, src, "agent", "list", "--project", "ghost")
    assert (code, data["error"]["code"]) == (2, "unknown-project")


def test_agent_unknown(capsys, src):
    for verb in ("show", "explain", "edit"):
        code, data, _ = run_json(capsys, src, "agent", verb, "ghost")
        assert (code, data["error"]["code"]) == (2, "unknown-agent"), verb
    code, data, _ = run_json(capsys, src, "agent", "show", "helper")
    assert (code, data["error"]["code"]) == (2, "unknown-agent")


def test_agent_explain_shows_the_chain(capsys, src):
    code, data, _ = run_json(capsys, src, "agent", "explain", "helper", "--project", "notes")
    assert code == 0
    (entry,) = data["data"]["keys"]
    assert entry["key"] == "permissions.timeout" and entry["value"] == 120
    assert [s["layer"] for s in entry["steps"]] == ["project", "agent"]
    assert entry["decided_by"] == {"layer": "agent", "op": "set"}
    code, out, _ = run(capsys, src, "agent", "explain", "helper", "--project", "notes")
    assert "permissions.timeout = 120" in out and "projects-root/notes/agents/helper.json" in out


def test_agent_edit_opens_the_settings_file_by_default(capsys, src, stratarc_home):
    code, _, _ = run(capsys, src, "agent", "edit", "reviewer", editor=replacing("timeout = 15", "timeout = 20"))
    assert code == 0
    assert "timeout = 20" in (src / "agents/reviewer.toml").read_text()
    assert "# Settings for the reviewer agent." in (src / "agents/reviewer.toml").read_text()
    assert len(backups(stratarc_home)) == 1


def test_agent_edit_definition_rolls_back_broken_frontmatter(capsys, src, stratarc_home):
    path = src / "agents/reviewer.md"
    before = path.read_bytes()
    code, data, _ = run_json(capsys, src, "agent", "edit", "reviewer", "--definition", editor=writing("---\nname: reviewer\n\n# no end\n"))
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == before and backups(stratarc_home) == []
    code, _, _ = run(capsys, src, "agent", "edit", "reviewer", "--definition", editor=replacing("Read the diff", "Read every diff"))
    assert code == 0 and "Read every diff" in path.read_text()


def test_agent_edit_rolls_back_invalid_toml(capsys, src):
    path = src / "agents/reviewer.toml"
    before = path.read_bytes()
    code, data, _ = run_json(capsys, src, "agent", "edit", "reviewer", editor=writing("[permissions\ntimeout = 1\n"))
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == before


def validator_errors(root: Path) -> list[dict]:
    with validate.bound_source(root):
        return [f for f in validate.run_all(root, "generic") if f["severity"] == "error"]


def test_agent_add_writes_a_definition_the_validator_accepts(capsys, tmp_path, stratarc_home):
    root = tmp_path / "fresh"
    assert run(capsys, root, "source", "init", str(root))[0] == 0
    baseline = validator_errors(root)
    code, data, _ = run_json(capsys, root, "agent", "add", "scout", "--tools", "Read", "Grep,Glob", "--description", "Find files: fast.")
    assert code == 0 and data["data"]["file"]["created"] is True
    text = (root / "agents" / "scout.md").read_text()
    assert text.startswith("---\nname: scout\n")
    assert 'description: "Find files: fast."' in text and "tools: Read, Grep, Glob\n" in text and "model: inherit\n" in text
    assert validator_errors(root) == baseline == []
    assert resources_cmd._frontmatter(text) == {"name": "scout", "description": '"Find files: fast."', "tools": "Read, Grep, Glob", "model": "inherit"}


def test_agent_add_defaults_and_a_parent_that_supplies_them(capsys, tmp_path):
    root = tmp_path / "fresh"
    assert run(capsys, root, "source", "init", str(root))[0] == 0
    assert run(capsys, root, "agent", "add", "base", "--tools", "Read", "Edit", "--description", "The base agent.")[0] == 0
    assert run(capsys, root, "agent", "add", "plain")[0] == 0
    plain = (root / "agents" / "plain.md").read_text()
    assert "tools: Read, Grep, Glob\n" in plain and "description:" in plain
    assert run(capsys, root, "agent", "add", "child", "--parent", "base", "--tools", "Read")[0] == 0
    child = (root / "agents" / "child.md").read_text()
    assert 'description: "The base agent."' in child and "tools: Read\n" in child
    assert run(capsys, root, "agent", "add", "heir", "--parent", "base")[0] == 0
    assert "tools: Read, Edit\n" in (root / "agents" / "heir.md").read_text()
    assert validator_errors(root) == []


def test_agent_add_refuses_an_existing_name_and_bad_input(capsys, src, tmp_path):
    before = tree_snapshot(src)
    code, data, _ = run_json(capsys, src, "agent", "add", "reviewer")
    assert (code, data["error"]["code"]) == (4, "agent-exists")
    code, data, _ = run_json(capsys, src, "agent", "add", "Not Valid")
    assert (code, data["error"]["code"]) == (2, "invalid-name")
    code, data, _ = run_json(capsys, src, "agent", "add", "x", "--tools", "Read", "Hammer")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    code, data, _ = run_json(capsys, src, "agent", "add", "x", "--description", "two\nlines")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    code, data, _ = run_json(capsys, src, "agent", "add", "x", "--parent", "ghost")
    assert (code, data["error"]["code"]) == (2, "unknown-agent")
    assert tree_snapshot(src) == before


def test_agent_add_accepts_an_mcp_tool_and_dry_run_writes_nothing(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, out, _ = run(capsys, src, "agent", "add", "tool-user", "--tools", "mcp__docs__search", "--dry-run")
    assert code == 0 and "would create" in out and tree_snapshot(tmp_path) == snapshot
    assert run(capsys, src, "agent", "add", "tool-user", "--tools", "mcp__docs__search")[0] == 0
    assert "tools: mcp__docs__search\n" in (src / "agents" / "tool-user.md").read_text()


def test_agent_remove_needs_yes_then_backs_up_every_file(capsys, src, stratarc_home, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, data, _ = run_json(capsys, src, "agent", "remove", "reviewer")
    assert (code, data["error"]["code"]) == (2, "needs-yes")
    code, out, _ = run(capsys, src, "agent", "remove", "reviewer", "--dry-run")
    assert code == 0 and "would remove" in out and tree_snapshot(tmp_path) == snapshot
    code, data, _ = run_json(capsys, src, "agent", "remove", "reviewer", "--yes")
    assert code == 0 and data["data"]["backups"] == 2
    assert not (src / "agents" / "reviewer.md").exists() and not (src / "agents" / "reviewer.toml").exists()
    texts = {p.read_text() for p in backups(stratarc_home)}
    assert any("Review a diff before it is committed." in t for t in texts) and any("timeout = 15" in t for t in texts)
    code, data, _ = run_json(capsys, src, "agent", "remove", "reviewer", "--yes")
    assert (code, data["error"]["code"]) == (2, "unknown-agent")


def test_agent_remove_with_a_project_leaves_the_source_root_agents(capsys, src):
    code, _, _ = run(capsys, src, "agent", "remove", "helper", "--project", "notes", "--yes")
    assert code == 0 and not (src / "projects-root/notes/agents/helper.json").exists()
    assert (src / "agents" / "reviewer.md").is_file()


# ---- account --------------------------------------------------------------------------------


def test_account_list_and_show(capsys, src):
    code, data, _ = run_json(capsys, src, "account", "list")
    assert code == 0 and data["data"]["accounts"] == [{"name": "work", "files": ["accounts/work.toml"]}]
    code, data, _ = run_json(capsys, src, "account", "show", "work")
    assert code == 0 and data["data"]["values"]["permissions.timeout"]["value"] == 90
    assert data["data"]["values"]["permissions.timeout"]["line"] == 3


def test_account_unknown(capsys, src):
    for verb in ("show", "edit"):
        code, data, _ = run_json(capsys, src, "account", verb, "ghost")
        assert (code, data["error"]["code"]) == (2, "unknown-account"), verb
    code, data, _ = run_json(capsys, src, "account", "remove", "ghost", "--yes")
    assert (code, data["error"]["code"]) == (2, "unknown-account")


def test_account_add_writes_a_valid_file_the_layers_can_read(capsys, src):
    code, _, _ = run(capsys, src, "account", "add", "personal", "--set", "permissions.timeout=30", "--set", 'permissions.allow=["a","b"]', "--set", "note=hello")
    assert code == 0
    text = (src / "accounts/personal.toml").read_text()
    assert text.startswith("# Values tied to the account personal.\n")
    assert tomllib.loads(text) == {"note": "hello", "permissions": {"timeout": 30, "allow": ["a", "b"]}}
    code, data, _ = run_json(capsys, src, "account", "show", "personal")
    assert code == 0 and data["data"]["values"]["permissions.allow"]["value"] == ["a", "b"]


def test_account_add_without_values_and_dry_run(capsys, src, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, out, _ = run(capsys, src, "account", "add", "empty", "--dry-run")
    assert code == 0 and "would create" in out and tree_snapshot(tmp_path) == snapshot
    code, _, _ = run(capsys, src, "account", "add", "empty")
    assert code == 0 and tomllib.loads((src / "accounts/empty.toml").read_text()) == {}


def test_account_add_rejects_existing_bad_names_and_bad_values(capsys, src):
    code, data, _ = run_json(capsys, src, "account", "add", "work")
    assert (code, data["error"]["code"]) == (4, "account-exists")
    code, data, _ = run_json(capsys, src, "account", "add", "Not Valid")
    assert (code, data["error"]["code"]) == (2, "invalid-name")
    code, data, _ = run_json(capsys, src, "account", "add", "x", "--set", "novalue")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    code, data, _ = run_json(capsys, src, "account", "add", "x", "--set", "a.b=1.5")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    assert not (src / "accounts/x.toml").exists()


def test_account_edit_saves_valid_and_rolls_back_invalid(capsys, src, stratarc_home):
    path = src / "accounts/work.toml"
    code, _, _ = run(capsys, src, "account", "edit", "work", editor=replacing("90", "95"))
    assert code == 0 and "timeout = 95" in path.read_text()
    assert "# Values tied to the work account." in path.read_text()
    saved = path.read_bytes()
    (backup,) = backups(stratarc_home)
    assert "timeout = 90" in backup.read_text()
    code, data, _ = run_json(capsys, src, "account", "edit", "work", editor=writing("= broken"))
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == saved
    code, data, _ = run_json(capsys, src, "account", "edit", "work", editor=writing('[permissions]\nallow = ["a"]\n_modes = { allow = "sideways" }\n'))
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == saved


def test_account_edit_set_changes_a_value_and_keeps_the_comments(capsys, src, stratarc_home):
    path = src / "accounts/work.toml"
    before = path.read_text()
    code, out, _ = run(capsys, src, "account", "edit", "work", "--set", "permissions.timeout=95")
    assert code == 0 and "changed" in out
    assert path.read_text() == before.replace("timeout = 90", "timeout = 95")
    (backup,) = backups(stratarc_home)
    assert backup.read_text() == before
    code, out, _ = run(capsys, src, "account", "edit", "work", "--set", "permissions.timeout=95")
    assert code == 0 and "unchanged" in out and len(backups(stratarc_home)) == 1


def test_account_edit_set_adds_keys_tables_and_top_level_values(capsys, src):
    path = src / "accounts/work.toml"
    code, _, _ = run(capsys, src, "account", "edit", "work", "--set", 'permissions.allow=["a","b"]', "--set", "other.level=3", "--set", "note=hello", "--set", "flag=true")
    assert code == 0
    text = path.read_text()
    assert text.startswith("# Values tied to the work account.\n")
    assert tomllib.loads(text) == {"note": "hello", "flag": True, "permissions": {"timeout": 90, "allow": ["a", "b"]}, "other": {"level": 3}}
    assert "timeout = 90\nallow = " in text
    code, data, _ = run_json(capsys, src, "account", "show", "work")
    assert code == 0 and data["data"]["values"]["other.level"]["value"] == 3


def test_account_edit_unset_removes_a_key_and_keeps_the_rest(capsys, src, stratarc_home):
    path = src / "accounts/work.toml"
    before = path.read_text()
    code, _, _ = run(capsys, src, "account", "edit", "work", "--unset", "permissions.timeout")
    assert code == 0
    assert path.read_text() == "# Values tied to the work account.\n[permissions]\n"
    (backup,) = backups(stratarc_home)
    assert backup.read_text() == before
    code, data, _ = run_json(capsys, src, "account", "edit", "work", "--unset", "permissions.timeout")
    assert (code, data["error"]["code"]) == (2, "unknown-key")
    assert path.read_text() == "# Values tied to the work account.\n[permissions]\n"


def test_account_edit_set_and_unset_together_and_in_dry_run(capsys, src, tmp_path):
    path = src / "accounts/work.toml"
    code, _, _ = run(capsys, src, "account", "edit", "work", "--set", "permissions.retries=2", "--unset", "permissions.timeout")
    assert code == 0 and tomllib.loads(path.read_text()) == {"permissions": {"retries": 2}}
    snapshot = tree_snapshot(tmp_path)
    code, out, _ = run(capsys, src, "account", "edit", "work", "--set", "permissions.retries=5", "--dry-run")
    assert code == 0 and "would change" in out and tree_snapshot(tmp_path) == snapshot


def test_account_edit_set_refuses_bad_input_and_leaves_the_file(capsys, src, stratarc_home):
    path = src / "accounts/work.toml"
    before = path.read_bytes()
    code, data, _ = run_json(capsys, src, "account", "edit", "work", "--set", "novalue")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    code, data, _ = run_json(capsys, src, "account", "edit", "work", "--set", "permissions.timeout=1.5")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    code, data, _ = run_json(capsys, src, "account", "edit", "work", "--set", "permissions.timeout=1", "--unset", "permissions.timeout")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    code, data, _ = run_json(capsys, src, "account", "edit", "work", "--set", "permissions._modes.allow=sideways")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    code, data, _ = run_json(capsys, src, "account", "edit", "work", "--set", "permissions=1")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_bytes() == before and backups(stratarc_home) == []


SIDE = '{\n  "zeta": 1,\n  "permissions": {\n    "timeout": 90,\n    "allow": ["a"]\n  },\n  "alpha": "x"\n}\n'


def test_account_edit_set_changes_a_json_value_keeping_key_order_and_indentation(capsys, src, stratarc_home):
    path = src / "accounts/side.json"
    path.write_text(SIDE)
    code, out, _ = run(capsys, src, "account", "edit", "side", "--set", "permissions.timeout=95", "--set", "alpha=y")
    assert code == 0 and "changed" in out
    doc = json.loads(path.read_text())
    assert list(doc) == ["zeta", "permissions", "alpha"] and doc["permissions"] == {"timeout": 95, "allow": ["a"]} and doc["alpha"] == "y"
    assert path.read_text() == json.dumps(doc, indent=2) + "\n"
    (backup,) = backups(stratarc_home)
    assert json.loads(backup.read_text())["permissions"]["timeout"] == 90
    code, out, _ = run(capsys, src, "account", "edit", "side", "--set", "permissions.timeout=95")
    assert code == 0 and "unchanged" in out and len(backups(stratarc_home)) == 1


def test_account_edit_set_adds_json_keys_and_tables_after_the_existing_ones(capsys, src):
    path = src / "accounts/side.json"
    path.write_text(SIDE)
    code, _, _ = run(capsys, src, "account", "edit", "side", "--set", 'permissions.deny=["b"]', "--set", "other.level=3", "--set", "flag=true")
    assert code == 0
    doc = json.loads(path.read_text())
    assert list(doc) == ["zeta", "permissions", "alpha", "other", "flag"]
    assert list(doc["permissions"]) == ["timeout", "allow", "deny"] and doc["other"] == {"level": 3} and doc["flag"] is True
    code, data, _ = run_json(capsys, src, "account", "show", "side")
    assert code == 0 and data["data"]["values"]["other.level"]["value"] == 3


def test_account_edit_unset_removes_a_json_key_and_dry_run_writes_nothing(capsys, src, stratarc_home, tmp_path):
    path = src / "accounts/side.json"
    path.write_text(SIDE)
    snapshot = tree_snapshot(tmp_path)
    code, out, _ = run(capsys, src, "account", "edit", "side", "--unset", "permissions.timeout", "--dry-run")
    assert code == 0 and "would change" in out and tree_snapshot(tmp_path) == snapshot
    code, _, _ = run(capsys, src, "account", "edit", "side", "--unset", "permissions.timeout", "--unset", "alpha")
    assert code == 0
    doc = json.loads(path.read_text())
    assert doc == {"zeta": 1, "permissions": {"allow": ["a"]}} and list(doc) == ["zeta", "permissions"]
    code, data, _ = run_json(capsys, src, "account", "edit", "side", "--unset", "alpha")
    assert (code, data["error"]["code"]) == (2, "unknown-key")


def test_account_edit_set_refuses_bad_json_edits_and_leaves_the_file(capsys, src, stratarc_home):
    path = src / "accounts/side.json"
    path.write_text(SIDE)
    code, data, _ = run_json(capsys, src, "account", "edit", "side", "--set", "alpha.deeper=1")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    code, data, _ = run_json(capsys, src, "account", "edit", "side", "--set", "permissions=1")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    code, data, _ = run_json(capsys, src, "account", "edit", "side", "--set", "permissions._modes.allow=sideways")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    code, data, _ = run_json(capsys, src, "account", "edit", "side", "--set", "alpha=1.5")
    assert (code, data["error"]["code"]) == (2, "invalid-value")
    path.write_text("[1, 2]\n")
    code, data, _ = run_json(capsys, src, "account", "edit", "side", "--set", "a=1")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    path.write_text("{broken")
    code, data, _ = run_json(capsys, src, "account", "edit", "side", "--set", "a=1")
    assert (code, data["error"]["code"]) == (2, "invalid-edit")
    assert path.read_text() == "{broken" and backups(stratarc_home) == []


def test_account_edit_set_never_opens_the_editor(capsys, src):
    calls = []
    code, _, _ = run(capsys, src, "account", "edit", "work", "--set", "permissions.timeout=1", editor=lambda p: calls.append(p))
    assert code == 0 and calls == []


def test_source_root_writes_keep_the_mode_of_the_file_and_the_home_stays_private(capsys, src, stratarc_home):
    old = os.umask(0o022)
    try:
        path = src / "accounts/work.toml"
        path.chmod(0o755)
        code, _, _ = run(capsys, src, "account", "edit", "work", "--set", "permissions.timeout=95")
        assert code == 0 and stat_mode(path) == 0o755
        code, _, _ = run(capsys, src, "account", "add", "fresh", "--set", "note=hi")
        assert code == 0 and stat_mode(src / "accounts/fresh.toml") == 0o644
        code, _, _ = run(capsys, src, "project", "add", "extra")
        assert code == 0 and stat_mode(src / "projects-root/extra/AGENTS.md") == 0o644 and stat_mode(src / "projects-root/extra") == 0o755
        code, _, _ = run(capsys, src, "project", "disable", "notes")
        assert code == 0 and stat_mode(src / "control-plane.md") == 0o644
    finally:
        os.umask(old)
    assert backups(stratarc_home) and all(stat_mode(b) == 0o600 for b in backups(stratarc_home))
    assert stat_mode(stratarc_home / ".stratarc") == 0o700


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o7777


def test_account_remove_needs_yes_then_backs_up(capsys, src, stratarc_home, tmp_path):
    snapshot = tree_snapshot(tmp_path)
    code, data, _ = run_json(capsys, src, "account", "remove", "work")
    assert (code, data["error"]["code"]) == (2, "needs-yes")
    assert tree_snapshot(tmp_path) == snapshot
    code, out, _ = run(capsys, src, "account", "remove", "work", "--dry-run")
    assert code == 0 and "would remove" in out and tree_snapshot(tmp_path) == snapshot
    code, _, _ = run(capsys, src, "account", "remove", "work", "--yes")
    assert code == 0 and not (src / "accounts/work.toml").exists()
    (backup,) = backups(stratarc_home)
    assert "timeout = 90" in backup.read_text()


# ---- source ---------------------------------------------------------------------------------


def test_source_init_scaffolds_registers_and_activates(capsys, tmp_path, stratarc_home):
    target = tmp_path / "fresh"
    code, data, _ = run_json(capsys, target, "source", "init", str(target))
    assert code == 0 and data["data"]["name"] == "fresh" and data["data"]["active"] is True
    assert (target / "stratarc.toml").is_file() and (target / "control-plane.md").is_file()
    sources = tomllib.loads((stratarc_home / ".stratarc/sources.toml").read_text())
    assert sources["active"] == "fresh" and sources["sources"]["fresh"]["path"] == str(target.resolve())
    assert sources["schema_version"] == 1
    other = tmp_path / "second"
    code, data, _ = run_json(capsys, other, "source", "init", str(other), "--name", "two")
    assert code == 0 and data["data"]["active"] is False
    code, data, _ = run_json(capsys, other, "source", "list")
    assert [(r["name"], r["active"]) for r in data["data"]["sources"]] == [("fresh", True), ("two", False)]


def test_source_init_dry_run_writes_nothing(capsys, tmp_path, stratarc_home):
    snapshot = tree_snapshot(tmp_path)
    code, data, _ = run_json(capsys, tmp_path / "fresh", "source", "init", str(tmp_path / "fresh"), "--dry-run")
    assert code == 0 and data["data"]["files"] > 0 and data["data"]["dry_run"] is True
    assert tree_snapshot(tmp_path) == snapshot and not (tmp_path / "fresh").exists()


def test_source_init_writes_files_with_the_default_mode_in_a_new_directory(capsys, tmp_path, stratarc_home):
    target = tmp_path / "deep" / "er" / "root"
    old = os.umask(0o022)
    try:
        code, _, _ = run(capsys, target, "source", "init", str(target))
    finally:
        os.umask(old)
    assert code == 0
    modes = {p: p.stat().st_mode & 0o777 for p in target.rglob("*") if p.is_file()}
    assert modes and set(modes.values()) == {0o644}
    assert {p.stat().st_mode & 0o777 for p in [target, *(d for d in target.rglob("*") if d.is_dir())]} == {0o755}
    assert (stratarc_home / ".stratarc" / "sources.toml").stat().st_mode & 0o777 == 0o600


def test_source_init_refuses_a_non_empty_directory_and_a_duplicate_name(capsys, src, tmp_path):
    code, data, _ = run_json(capsys, src, "source", "init", str(src))
    assert (code, data["error"]["code"]) == (4, "path-exists")
    first = tmp_path / "one"
    assert run(capsys, first, "source", "init", str(first), "--name", "same")[0] == 0
    code, data, _ = run_json(capsys, first, "source", "init", str(tmp_path / "two"), "--name", "same")
    assert (code, data["error"]["code"]) == (4, "source-exists")
    code, data, _ = run_json(capsys, first, "source", "init", str(tmp_path / "three"), "--name", "Bad Name")
    assert (code, data["error"]["code"]) == (2, "invalid-name")


def test_source_use_registers_a_path_and_switches_between_names(capsys, src, tmp_path, stratarc_home):
    code, data, _ = run_json(capsys, src, "source", "use", str(src))
    assert code == 0 and data["data"]["registered"] is True and data["data"]["name"] == "src"
    other = tmp_path / "other"
    other.mkdir()
    assert run(capsys, src, "source", "use", str(other))[0] == 0
    code, data, _ = run_json(capsys, src, "source", "use", "src")
    assert code == 0 and data["data"]["changed"] is True
    code, out, _ = run(capsys, src, "source", "use", "src")
    assert code == 0 and "already active" in out
    assert tomllib.loads((stratarc_home / ".stratarc/sources.toml").read_text())["active"] == "src"
    assert len(backups(stratarc_home)) >= 1


def test_source_use_unknown_and_dry_run(capsys, src, tmp_path, stratarc_home):
    code, data, _ = run_json(capsys, src, "source", "use", "ghost")
    assert (code, data["error"]["code"]) == (2, "source-not-found")
    snapshot = tree_snapshot(tmp_path)
    code, out, _ = run(capsys, src, "source", "use", str(src), "--dry-run")
    assert code == 0 and "would make" in out and tree_snapshot(tmp_path) == snapshot


def test_source_show_reports_the_effective_root(capsys, src):
    code, data, _ = run_json(capsys, src, "source", "show")
    assert code == 0
    assert data["data"]["has_config"] is True and data["data"]["projects"] == 1 and data["data"]["registered_as"] is None


def test_source_active_entry_is_used_when_no_root_is_given(capsys, src, monkeypatch, tmp_path):
    assert run(capsys, src, "source", "use", str(src))[0] == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code = main(["project", "list", "--json"], environ={})
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and [p["name"] for p in data["data"]["projects"]] == ["notes"]


def test_source_move_needs_yes_then_moves_and_updates_sources(capsys, src, tmp_path, stratarc_home):
    assert run(capsys, src, "source", "use", str(src))[0] == 0
    destination = tmp_path / "moved" / "root"
    snapshot = tree_snapshot(tmp_path)
    code, data, _ = run_json(capsys, src, "source", "move", str(destination))
    assert (code, data["error"]["code"]) == (2, "needs-yes")
    code, out, _ = run(capsys, src, "source", "move", str(destination), "--dry-run")
    assert code == 0 and "would move" in out and tree_snapshot(tmp_path) == snapshot
    code, _, _ = run(capsys, destination, "source", "move", str(destination), "--yes")
    assert code == 0 and not src.exists() and (destination / "stratarc.toml").is_file()
    state = tomllib.loads((stratarc_home / ".stratarc/sources.toml").read_text())
    assert state["sources"]["src"]["path"] == str(destination.resolve())


def test_source_move_refuses_bad_targets(capsys, src, tmp_path):
    assert run(capsys, src, "source", "use", str(src))[0] == 0
    code, data, _ = run_json(capsys, src, "source", "move", str(src / "inside"), "--yes")
    assert (code, data["error"]["code"]) == (2, "invalid-path")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "file").write_text("x")
    code, data, _ = run_json(capsys, src, "source", "move", str(busy), "--yes")
    assert (code, data["error"]["code"]) == (4, "path-exists")
    code, data, _ = run_json(capsys, src, "source", "move", str(tmp_path / "x"), "--name", "ghost", "--yes")
    assert (code, data["error"]["code"]) == (2, "source-not-found")


def test_source_use_refuses_a_newer_sources_file(capsys, src, stratarc_home):
    home = stratarc_home / ".stratarc"
    home.mkdir()
    (home / "sources.toml").write_text("schema_version = 9\n")
    before = (home / "sources.toml").read_bytes()
    code, data, _ = run_json(capsys, src, "source", "use", str(src))
    assert (code, data["error"]["code"]) == (5, "newer-schema")
    assert (home / "sources.toml").read_bytes() == before


def test_missing_source_root_is_invalid_input(capsys, tmp_path):
    code, data, _ = run_json(capsys, tmp_path / "nowhere", "project", "list")
    assert (code, data["error"]["code"]) == (2, "source-root-missing")


# ---- envelope and output --------------------------------------------------------------------


def test_json_envelope_shape_and_human_output_channels(capsys, src):
    code, out, err = run(capsys, src, "account", "list", "--json")
    body = json.loads(out)
    assert (code, err) == (0, "") and set(body) == {"ok", "data", "error"} and body["ok"] is True and body["error"] is None
    code, out, err = run(capsys, src, "account", "show", "ghost")
    assert code == 2 and out == "" and err.startswith("error unknown-account")
    code, out, err = run(capsys, src, "account", "list")
    assert code == 0 and err == "" and out == "work  accounts/work.toml\n"


def test_options_are_accepted_before_and_after_the_verb(capsys, src):
    code = main(["account", "list", "--root", str(src), "--json"], environ={})
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    code = main(["--json", "--root", str(src), "account", "list"], environ={})
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"] is True


def test_every_write_verb_takes_dry_run_and_every_delete_takes_yes():
    parser = resources_cmd._parser()
    writes = {"add", "edit", "enable", "disable", "target", "use", "init", "move", "remove"}
    no_argument = {("source", "list"), ("source", "show"), ("project", "list"), ("runtime", "list"), ("agent", "list"), ("account", "list")}
    for resource, verb in resources_cmd.HANDLERS:
        positional = ["a", "b"] if verb == "target" else [] if (resource, verb) in no_argument else ["a"]
        if verb in writes:
            assert parser.parse_args([resource, verb, *positional, "--dry-run"]).dry_run is True, (resource, verb)
        if verb in {"remove", "move"}:
            assert parser.parse_args([resource, verb, *positional, "--yes"]).yes is True, (resource, verb)
        if verb not in writes:
            with pytest.raises(SystemExit):
                parser.parse_args([resource, verb, *positional, "--dry-run"])


def test_runtime_unknown_error_text_goes_to_standard_error(capsys, src):
    code, out, err = run(capsys, src, "runtime", "enable", "nope")
    assert code == 2 and out == "" and err.startswith("error unknown-runtime") and "stratarc runtime list" in err
