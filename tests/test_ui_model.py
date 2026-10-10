from __future__ import annotations

import os
from pathlib import Path

import pytest

from stratarc.ui import model

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ui" / "source"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, stratarc_home):
    for name in [n for n in os.environ if n.startswith("STRATARC_") and n != "STRATARC_HOME"]:
        monkeypatch.delenv(name)


def test_model_does_not_import_textual():
    source = Path(model.__file__).read_text(encoding="utf-8")
    assert "textual" not in source


def test_ui_exits_5_and_names_the_extra_without_textual(monkeypatch, capsys):
    import stratarc.ui as ui

    monkeypatch.setattr(ui, "textual_available", lambda: False)
    assert ui.main(["--root", str(FIXTURE)]) == 5
    err = capsys.readouterr().err
    assert "stratarc[ui]" in err and "Textual" in err


def test_projects_and_agents_are_listed():
    assert model.list_projects(FIXTURE) == ["notes"]
    assert model.list_agents(FIXTURE, "notes") == ["reviewer"]
    assert model.list_agents(FIXTURE) == []


def test_tree_has_source_projects_and_agents():
    tree = model.build_tree(FIXTURE)
    assert tree.kind == model.SOURCE
    project = next(n for n in tree.children if n.kind == model.PROJECT)
    assert project.label == "notes"
    agent = next(n for n in project.children if n.kind == model.AGENT)
    assert agent.label == "reviewer"
    assert [n.key for n in agent.children] == ["permissions.timeout"]


def test_source_lists_every_key_and_project_only_what_it_changes():
    tree = model.build_tree(FIXTURE)
    source_keys = {n.key for n in tree.children if n.kind == model.VALUE}
    assert {"permissions.timeout", "permissions.defaultMode", "settings.owner"} <= source_keys
    project = next(n for n in tree.children if n.kind == model.PROJECT)
    assert [n.key for n in project.children if n.kind == model.VALUE] == ["permissions.timeout"]


def test_scope_rows_carry_provenance():
    rows = {r.key: r for r in model.scope_rows(FIXTURE, "notes")}
    timeout = rows["permissions.timeout"]
    assert (timeout.value, timeout.layer, timeout.file) == (60, "project", "projects-root/notes/permissions.json")
    assert timeout.line == 2
    assert rows["permissions.defaultMode"].layer == "base"
    agent = {r.key: r for r in model.scope_rows(FIXTURE, "notes", "reviewer")}
    assert agent["permissions.timeout"].value == 90


def test_detail_text_for_a_value_names_the_deciding_file():
    node = model.Node("n", model.VALUE, "permissions.timeout", "notes", None, "permissions.timeout")
    text = model.detail_text(FIXTURE, node)
    assert "value:  60" in text
    assert "projects-root/notes/permissions.json:2" in text


def test_detail_text_for_a_scope_lists_values():
    node = model.build_tree(FIXTURE).children[-1]
    text = model.detail_text(FIXTURE, node)
    assert "permissions.timeout = 60" in text


def test_explain_text_matches_config_explain(capsys):
    from stratarc.config_cmd import main

    assert main(["--root", str(FIXTURE), "explain", "permissions.timeout", "--project", "notes", "--agent", "reviewer"]) == 0
    expected = capsys.readouterr().out.strip()
    node = model.Node("n", model.VALUE, "permissions.timeout", "notes", "reviewer", "permissions.timeout")
    assert model.explain_text(FIXTURE, node) == expected


def test_explain_text_for_a_project_is_the_outline():
    node = model.build_tree(FIXTURE).children[-1]
    text = model.explain_text(FIXTURE, node)
    assert text.startswith("notes   (project: notes)")
    assert "permissions.timeout" in text


def test_explain_text_for_the_source_asks_for_a_value():
    assert "Select a value" in model.explain_text(FIXTURE, model.build_tree(FIXTURE))


def test_owning_file_is_the_deciding_file():
    value = model.Node("n", model.VALUE, "permissions.timeout", "notes", "reviewer", "permissions.timeout")
    assert model.owning_file(FIXTURE, value) == FIXTURE / "projects-root" / "notes" / "agents" / "reviewer.json"
    project = model.Node("p", model.PROJECT, "notes", "notes")
    assert model.owning_file(FIXTURE, project) == FIXTURE / "projects-root" / "notes" / "permissions.json"
    assert model.owning_file(FIXTURE, model.build_tree(FIXTURE)) == FIXTURE / "stratarc.toml"


def test_log_entries_filter_by_project_and_key(monkeypatch):
    from stratarc import changelog

    seen = []
    monkeypatch.setattr(changelog, "query", lambda filters, limit=None: seen.append((dict(filters), limit)) or [])
    value = model.Node("n", model.VALUE, "permissions.timeout", "notes", None, "permissions.timeout")
    assert model.log_text(value) == "no changes recorded"
    model.log_entries(model.Node("p", model.PROJECT, "notes", "notes"))
    assert seen == [({"project": "notes", "key": "permissions.timeout"}, 20), ({"project": "notes"}, 20)]


def test_sync_preview_writes_nothing():
    before = sorted(p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file())
    code, text = model.sync_preview(FIXTURE)
    assert isinstance(code, int) and isinstance(text, str) and text
    assert sorted(p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file()) == before


def _copy(tmp_path):
    import shutil

    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    return root


def test_edit_role_names_the_root_files_only(tmp_path):
    assert model.edit_role(FIXTURE, FIXTURE / "stratarc.toml") == "config"
    assert model.edit_role(FIXTURE, FIXTURE / "permissions.json") == "permissions"
    assert model.edit_role(FIXTURE, FIXTURE / "projects-root" / "notes" / "permissions.json") is None


def test_finish_edit_keeps_a_valid_edit_and_backs_up_the_original(tmp_path):
    from stratarc import home_layout as layout

    root = _copy(tmp_path)
    path = root / "projects-root" / "notes" / "permissions.json"
    original = path.read_bytes()
    session = model.begin_edit(path)
    path.write_text('{"timeout": 90}\n', encoding="utf-8")
    outcome = model.finish_edit(root, session)
    assert outcome.ok and outcome.changed and not outcome.problems
    assert path.read_text(encoding="utf-8") == '{"timeout": 90}\n'
    assert any(p.read_bytes() == original for p in layout.backups_dir().rglob("*") if p.is_file())


def test_finish_edit_restores_the_original_on_an_invalid_edit(tmp_path):
    root = _copy(tmp_path)
    path = root / "projects-root" / "notes" / "permissions.json"
    original = path.read_bytes()
    session = model.begin_edit(path)
    path.write_text("{not json", encoding="utf-8")
    outcome = model.finish_edit(root, session)
    assert not outcome.ok and outcome.problems
    assert path.read_bytes() == original


def test_finish_edit_uses_the_config_validator_for_the_root_file(tmp_path):
    root = _copy(tmp_path)
    path = root / "stratarc.toml"
    original = path.read_bytes()
    session = model.begin_edit(path)
    path.write_text("owner = [\n", encoding="utf-8")
    assert not model.finish_edit(root, session).ok
    assert path.read_bytes() == original


def test_finish_edit_removes_a_file_that_did_not_exist_when_it_is_invalid(tmp_path):
    path = tmp_path / "new.json"
    session = model.begin_edit(path)
    path.write_text("{", encoding="utf-8")
    assert not model.finish_edit(tmp_path, session).ok
    assert not path.exists()


def test_open_in_editor_uses_the_environment(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("VISUAL", "")
    monkeypatch.setenv("EDITOR", "myeditor --wait")
    monkeypatch.setattr(model.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    assert model.open_in_editor(tmp_path / "f.json") == 0
    assert calls == [["myeditor", "--wait", str(tmp_path / "f.json")]]
