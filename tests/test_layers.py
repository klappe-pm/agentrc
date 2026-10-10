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


# ---- the dispatch relay ---------------------------------------------------------------------


def agents_root(tmp_path, files: dict[str, str], accounts: dict[str, str] | None = None) -> Path:
    (tmp_path / "agents").mkdir(exist_ok=True)
    for name, body in files.items():
        (tmp_path / "agents" / f"{name}.json").write_text(body)
    if accounts:
        (tmp_path / "accounts").mkdir(exist_ok=True)
        for name, body in accounts.items():
            (tmp_path / "accounts" / f"{name}.toml").write_text(body)
    return tmp_path


def test_relay_passes_only_the_keys_named_in_inherit():
    layered = load(project="notes", agent="worker")
    timeout = layered.resolve("permissions.timeout")
    assert timeout.value == 120 and timeout.decided_by.layer == "agent"
    assert timeout.decided_by.via == "lead"
    assert timeout.decided_by.file == FIXTURE / "projects-root/notes/agents/lead.json"
    owner = layered.resolve("settings.owner")
    assert owner.value == "worker-owner" and owner.decided_by.via is None


def test_relay_inherits_nothing_by_default():
    layered = load(project="notes", agent="quiet")
    assert layered.resolve("permissions.timeout").value == 60
    link = layered.relay.links[0]
    assert (link.agent, link.parent, link.inherit, link.taken) == ("quiet", "lead", (), ())
    assert dict(link.skipped)["permissions.timeout"].startswith("relay.inherit is empty")


def test_relay_records_what_was_not_inherited_and_why():
    link = load(project="notes", agent="worker").relay.links[0]
    assert link.inherit == ("permissions.*",)
    assert link.taken == ("permissions.defaultMode", "permissions.timeout")
    assert dict(link.skipped) == {"settings.owner": "no relay.inherit pattern matches (inherit: permissions.*)"}
    assert link.file == FIXTURE / "projects-root/notes/agents/worker.json" and link.line == 2


def test_account_comes_from_the_parent_when_the_child_has_none():
    layered = load(project="notes", agent="worker")
    assert (layered.relay.account, layered.relay.account_from, layered.relay.account_agent) == ("work", "parent", "lead")
    mode = layered.resolve("permissions.defaultMode")
    assert [s.layer for s in mode.steps] == ["base", "account", "agent"]
    assert mode.value == "plan"


def test_child_account_wins_over_the_parent_and_the_flag_wins_over_both(tmp_path):
    root = agents_root(
        tmp_path,
        {"lead": '{"account": "a"}', "kid": '{"parent": "lead", "account": "b"}'},
        {"a": 'x = 1\n', "b": 'x = 2\n', "c": 'x = 3\n'},
    )
    assert layers.load(root, agent="kid", environ={}).relay.account == "b"
    assert layers.load(root, agent="kid", account="c", environ={}).relay.account_from == "--account"
    assert layers.load(root, agent="lead", environ={}).relay.links == []


def test_relay_chains_through_a_grandparent_when_each_link_allows_it(tmp_path):
    root = agents_root(
        tmp_path,
        {
            "boss": '{"permissions": {"timeout": 7}}',
            "lead": '{"parent": "boss", "relay": {"inherit": ["permissions.*"]}}',
            "kid": '{"parent": "lead", "relay": {"inherit": ["permissions.*"]}}',
            "blocked": '{"parent": "lead"}',
        },
    )
    res = layers.load(root, agent="kid", environ={}).resolve("permissions.timeout")
    assert res.value == 7 and res.decided_by.via == "lead <- boss"
    with pytest.raises(LayerError) as caught:
        layers.load(root, agent="blocked", environ={}).resolve("permissions.timeout")
    assert caught.value.code == "unknown-key" and "relay" in caught.value.hint


def test_relay_cycle_is_a_clear_error():
    with pytest.raises(LayerError) as caught:
        load(project="cycle", agent="ping")
    exc = caught.value
    assert exc.code == "relay-cycle"
    assert "ping -> pong -> ping" in exc.message
    assert exc.file == FIXTURE / "projects-root/cycle/agents/pong.json" and exc.line == 2


def test_relay_depth_limit_is_eight_agents(tmp_path):
    def chain(count: int) -> Path:
        files = {f"a{i}": ('{"parent": "a%d"}' % (i + 1)) if i < count - 1 else "{}" for i in range(count)}
        (tmp_path / f"c{count}").mkdir()
        return agents_root(tmp_path / f"c{count}", files)

    assert len(layers.load(chain(8), agent="a0", environ={}).relay.links) == 7
    with pytest.raises(LayerError) as caught:
        layers.load(chain(9), agent="a0", environ={})
    assert caught.value.code == "relay-depth" and "8" in caught.value.message


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('{"parent": 3}', "parent"),
        ('{"parent": "lead", "relay": {"inherit": "permissions.*"}}', "inherit"),
        ('{"parent": "lead", "relay": {"inherit": [], "other": 1}}', "other"),
        ('{"relay": {"inherit": ["a.*"]}}', "parent"),
    ],
)
def test_relay_declarations_are_validated(tmp_path, body, fragment):
    root = agents_root(tmp_path, {"lead": "{}", "kid": body})
    with pytest.raises(LayerError) as caught:
        layers.load(root, agent="kid", environ={})
    exc = caught.value
    assert exc.code == "relay-invalid" and fragment in exc.message and exc.file == root / "agents/kid.json" and exc.line


def test_a_parent_that_does_not_exist_names_the_declaring_line(tmp_path):
    root = agents_root(tmp_path, {"kid": '{\n  "parent": "ghost"\n}'})
    with pytest.raises(LayerError) as caught:
        layers.load(root, agent="kid", environ={})
    exc = caught.value
    assert exc.code == "unknown-agent" and "ghost" in exc.message and exc.file == root / "agents/kid.json" and exc.line == 2


def test_relay_edges_list_the_declared_parents():
    edges = layers.relay_edges(FIXTURE, "notes")
    assert [(e.agent, e.parent, e.inherit) for e in edges] == [("quiet", "lead", ()), ("worker", "lead", ("permissions.*",))]
    assert edges[1].file == FIXTURE / "projects-root/notes/agents/worker.json" and edges[1].line == 2
    assert layers.relay_edges(FIXTURE, None) == []


# ---- the flags layer ------------------------------------------------------------------------


def test_flags_apply_last_with_provenance_set_line_zero():
    env = {"STRATARC_PERMISSIONS__TIMEOUT": "120"}
    res = layers.load(FIXTURE, project="notes", agent="reviewer", environ=env, flags={"permissions.timeout": "5"}).resolve("permissions.timeout")
    assert res.value == "5" and res.decided_by.layer == "flags"
    step = res.decided_by
    assert (step.file, step.label, step.line, step.op) == (None, "--set", 0, "set")
    assert [s.layer for s in res.steps] == ["base", "project", "agent", "env", "flags"]
    assert step.overrode[-1] == ("env", 120)


def test_a_flag_can_define_a_key_no_file_sets():
    res = layers.load(FIXTURE, environ={}, flags={"settings.fresh": True}).resolve("settings.fresh")
    assert res.value is True and [s.layer for s in res.steps] == ["flags"]


def test_a_flag_list_needs_a_mode_like_any_layer():
    flags = {"permissions.network.allow": ["x.example"]}
    with pytest.raises(LayerError) as caught:
        layers.load(FIXTURE, environ={}, flags=flags).resolve("permissions.network.allow")
    exc = caught.value
    assert exc.code == "list-mode-missing" and "--set-mode" in exc.hint
    extend = layers.load(FIXTURE, environ={}, flags=flags, flag_modes={"permissions.network.allow": "extend"}).resolve("permissions.network.allow")
    assert extend.value == ["api.example.com", "x.example"] and extend.decided_by.op == "extend"
    replace = layers.load(FIXTURE, environ={}, flags=flags, flag_modes={"permissions.network.allow": "replace"}).resolve("permissions.network.allow")
    assert replace.value == ["x.example"] and replace.decided_by.overrode == (("base", ["api.example.com"]),)


@pytest.mark.parametrize(
    ("flags", "modes", "code"),
    [
        ({"a": ["x"]}, {"a": "append"}, "mode-invalid"),
        ({"a": "x"}, {"a": "extend"}, "mode-invalid"),
        ({"a": ["x"]}, {"b": "extend"}, "flag-invalid"),
    ],
)
def test_flag_modes_are_validated(flags, modes, code):
    with pytest.raises(LayerError) as caught:
        layers.load(FIXTURE, environ={}, flags=flags, flag_modes=modes)
    assert caught.value.code == code
