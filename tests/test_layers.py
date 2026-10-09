from __future__ import annotations

from pathlib import Path

import pytest

from stratarc import layers
from stratarc.layers import FileKind, LayerError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "layers" / "source"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "notes-cli" / "source"


def load(**scope):
    return layers.load(FIXTURE, environ={}, **scope)


def line_of(path: Path, needle: str) -> int:
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        if needle in text:
            return number
    raise AssertionError(needle)


def test_base_alone():
    res = load().resolve("permissions.timeout")
    assert res.value == 30
    assert [s.layer for s in res.steps] == ["base"]


def test_precedence_scalar_follows_the_documented_order():
    assert load().resolve("permissions.defaultMode").value == "default"
    assert load(account="work").resolve("permissions.defaultMode").value == "acceptEdits"
    assert load(project="notes").resolve("permissions.timeout").value == 60
    full = load(project="notes", agent="reviewer").resolve("permissions.timeout")
    assert full.value == 90
    assert [s.layer for s in full.steps] == ["base", "project", "agent"]
    assert full.decided_by.layer == "agent"


def test_env_and_flags_sit_above_the_agent():
    env = {"STRATARC_PERMISSIONS__TIMEOUT": "120"}
    res = layers.load(FIXTURE, project="notes", agent="reviewer", environ=env).resolve("permissions.timeout")
    assert res.value == 120 and res.decided_by.layer == "env"
    res = layers.load(FIXTURE, project="notes", environ=env, flags={"permissions.timeout": 5}).resolve("permissions.timeout")
    assert res.value == 5 and res.decided_by.layer == "flags"
    assert [s.layer for s in res.steps] == ["base", "project", "env", "flags"]


def test_extend_appends_and_replace_swaps():
    assert load(runtime="codex").resolve("permissions.network.allow").value == ["api.example.com", "registry.example.org"]
    both = load(runtime="codex", project="notes").resolve("permissions.network.allow")
    assert both.value == ["api.example.com", "tools.example.net"]
    assert [s.op for s in both.steps] == ["set", "extend", "replace"]


def test_provenance_records_overridden_values():
    res = load(runtime="codex", project="notes").resolve("permissions.network.allow")
    extend, replace = res.steps[1], res.steps[2]
    assert extend.overrode == ()
    assert replace.overrode == (("base", ["api.example.com"]), ("runtime", ["registry.example.org"]))
    timeout = load(project="notes", agent="reviewer").resolve("permissions.timeout")
    assert timeout.steps[2].overrode == (("base", 30), ("project", 60))


def test_provenance_file_and_line_are_accurate():
    res = load(runtime="codex", project="notes", agent="reviewer").resolve("permissions.network.allow")
    base, runtime, project = res.steps
    assert (base.file, base.line) == (FIXTURE / "permissions.json", line_of(FIXTURE / "permissions.json", '"allow"'))
    assert (runtime.file, runtime.line) == (FIXTURE / "runtimes/codex.toml", line_of(FIXTURE / "runtimes/codex.toml", "allow = ["))
    project_file = FIXTURE / "projects-root/notes/permissions.json"
    assert (project.file, project.line) == (project_file, line_of(project_file, '"allow": ['))
    agent = load(project="notes", agent="reviewer").resolve("permissions.timeout").steps[-1]
    assert agent.file == FIXTURE / "projects-root/notes/agents/reviewer.json"
    assert agent.line == line_of(agent.file, '"timeout"')
    owner = load().resolve("settings.owner").steps[0]
    assert (owner.file, owner.line) == (FIXTURE / "stratarc.toml", 1)
    assert load().resolve("settings.runtimes.codex.enabled").steps[0].line == line_of(FIXTURE / "stratarc.toml", "enabled")


def test_list_without_mode_is_a_clear_error():
    with pytest.raises(LayerError) as caught:
        load(project="nomode").resolve("permissions.network.allow")
    exc = caught.value
    assert exc.code == "list-mode-missing"
    assert exc.file == FIXTURE / "projects-root/nomode/permissions.json"
    assert exc.line == 3
    assert "replace" in exc.hint and "extend" in exc.hint


def test_resolve_all_reports_the_bad_key_and_keeps_the_rest():
    done, failed = load(project="nomode").resolve_all()
    assert set(failed) == {"permissions.network.allow"}
    assert done["permissions.timeout"].value == 30


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('{"a": {"x": [1], "_modes": {"x": "append"}}}', "mode-invalid"),
        ('{"a": {"x": 1, "_modes": {"x": "extend"}}}', "mode-invalid"),
        ('{"a": ', "parse-error"),
    ],
)
def test_bad_files_name_file_and_line(tmp_path, body, code):
    (tmp_path / "projects-root" / "p").mkdir(parents=True)
    bad = tmp_path / "projects-root" / "p" / "permissions.json"
    bad.write_text(body)
    with pytest.raises(LayerError) as caught:
        layers.load(tmp_path, project="p", environ={})
    assert caught.value.code == code and caught.value.file == bad and caught.value.line


def test_type_mismatch_between_layers():
    root = FIXTURE
    sources = [
        layers.Source("base", None, "a", {"k": layers.Entry([1], 1)}),
        layers.Source("project", None, "b", {"k": layers.Entry(3, 2)}),
    ]
    with pytest.raises(LayerError) as caught:
        layers.Layers(layers.Request(root), sources).resolve("k")
    assert caught.value.code == "type-mismatch"


def test_unknown_names_and_keys():
    for scope, code in [({"project": "nope"}, "unknown-project"), ({"account": "nope"}, "unknown-account"), ({"runtime": "nope"}, "unknown-runtime"), ({"agent": "nope"}, "unknown-agent")]:
        with pytest.raises(LayerError) as caught:
            load(**scope)
        assert caught.value.code == code
    with pytest.raises(LayerError) as caught:
        load().resolve("permissions.missing")
    assert caught.value.code == "unknown-key"


def test_table_prefix_expands_and_nests():
    layered = load()
    assert layered.expand("permissions.network") == ["permissions.network.allow", "permissions.network.deny"]
    assert layers.nest({"a.b": 1, "a.c": 2}) == {"a": {"b": 1, "c": 2}}


def test_one_registration_adds_a_file_kind(tmp_path):
    (tmp_path / "extra.json").write_text('{\n  "x": 1\n}\n')
    kind = FileKind("base", "extra.json", "extra")
    layers.register_file_kind(kind)
    try:
        res = layers.load(tmp_path, environ={}).resolve("extra.x")
    finally:
        layers.FILE_KINDS.remove(kind)
    assert (res.value, res.steps[0].line) == (1, 2)


def test_toml_line_scanner_handles_multiline_values(tmp_path):
    (tmp_path / "stratarc.toml").write_text('# c\nitems = [\n  "a = b",\n  "c",\n]\n\n[t]\nname = """\nx = 1\n"""\nlast = 2\n')
    layered = layers.load(tmp_path, environ={})
    assert layered.resolve("settings.items").steps[0].line == 2
    assert layered.resolve("settings.t.last").steps[0].line == 11
    assert "settings.x" not in layered.keys()


def test_notes_cli_example_resolves_with_project_overlay():
    setting = "permissions.blockReadsOutsideWorkingDirectories"
    assert layers.load(EXAMPLE, environ={}).resolve(setting).value is False
    res = layers.load(EXAMPLE, project="notes-cli", environ={}).resolve(setting)
    assert res.value is True
    base, project = res.steps
    assert base.file == EXAMPLE / "permissions.json" and base.line == line_of(base.file, "blockReads")
    assert project.file == EXAMPLE / "projects-root/notes-cli/permissions.json"
    assert project.line == line_of(project.file, "blockReads")
    assert project.overrode == (("base", False),)


def test_display_path_is_relative_or_tilde(stratarc_home):
    assert layers.display_path(FIXTURE / "permissions.json", FIXTURE) == "permissions.json"
    assert layers.display_path(stratarc_home / "x" / "f.json", FIXTURE) == "~/x/f.json"
