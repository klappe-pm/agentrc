"""Tests for the project plugin ingester.

Every tree is a synthetic fixture under a pytest temporary directory: a source root, a projects tree and a home, reached through STRATARC_HOME and LLM_ROOT_PROJECTS_DIR. The tests that drive `stratarc.sync.main` run the real reconciler as a child.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stratarc import project_plugins_ingest as ingest
from stratarc import sync
from stratarc.components import validate
from stratarc.control_plane import ControlPlane, set_cell

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INGEST_FIXTURES = FIXTURES / "plugins_ingest"
SYNC_SOURCE = FIXTURES / "sync" / "source"
TODAY = datetime.date(2026, 10, 9)


def run_git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def commit_all(root: Path) -> None:
    for arguments in (
        ("add", "-A"),
        ("-c", "user.email=test@example.com", "-c", "user.name=test", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"),
    ):
        run_git("-C", str(root), *arguments)


def control_plane_text(projects: list[str], rows: list[str] = (), inactive: tuple[str, ...] = ()) -> str:
    columns = " | ".join(["global", *projects])
    lines = [
        "# control-plane",
        "",
        "## projects",
        "",
        "| project | status | tier | template | origin |",
        "|---|---|---|---|---|",
        *[f"| {name} | {'inactive' if name in inactive else 'active'} | normal | base | - |" for name in projects],
        "",
        "## plugins",
        "",
        f"| option | {columns} |",
        "|" + "|".join("---" for _ in range(len(projects) + 2)) + "|",
        *[f"| [plugin:{row}](components.json) | " + " | ".join("" for _ in range(len(projects) + 1)) + " |" for row in rows],
        "",
    ]
    return "\n".join(lines)


class Fixture:
    """A source root, a projects tree and a Claude home, all under one temporary directory."""

    def __init__(
        self,
        base: Path,
        projects: tuple[str, ...] = ("alpha",),
        plugin_rows: tuple[str, ...] = (),
        plugins: list | None = None,
        control: bool = False,
    ):
        """control adds a project `zz` with a hand-added plugin that must always be found,
        so a test of what is skipped cannot pass against an ingester that finds nothing."""
        projects = (*projects, "zz") if control else projects
        self.base = base.resolve()
        self.root = self.base / "source"
        self.projects = self.base / "projects"
        self.home = self.base / "home"
        self.claude = self.home / ".claude"
        (self.root / "projects-root").mkdir(parents=True)
        self.claude.mkdir(parents=True)
        self.names = list(projects)
        (self.root / "components.json").write_text(json.dumps({"version": 1, "plugins": plugins or []}, indent=2) + "\n")
        (self.root / "control-plane.md").write_text(control_plane_text(self.names, plugin_rows))
        self.checkouts = {}
        for name in self.names:
            checkout = self.projects / "active" / name
            checkout.mkdir(parents=True)
            run_git("init", "-q", str(checkout))
            self.checkouts[name] = checkout
        if control:
            self.enable("zz", {"control@m": True})

    def enable(self, project: str, plugins: dict, relative: str = ".claude/settings.local.json") -> None:
        path = self.checkouts[project] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        current = json.loads(path.read_text()).get("enabledPlugins", {}) if path.exists() else {}
        path.write_text(json.dumps({"enabledPlugins": {**current, **plugins}}))

    def scan(self) -> ingest.Plan:
        return ingest.scan(self.root, projects=self.projects, claude=self.claude, today=TODAY)

    def manifest(self) -> dict:
        return json.loads((self.root / "components.json").read_text())

    def source_bytes(self) -> tuple[bytes, bytes]:
        return (self.root / "components.json").read_bytes(), (self.root / "control-plane.md").read_bytes()


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The home, the projects root and the source root come from the fixture, never from the machine."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("STRATARC_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "projects"))
    for name in ("STRATARC_SOURCE", "STRATARC_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def keys(plan: ingest.Plan) -> dict[str, list[str]]:
    return {candidate.key: candidate.projects for candidate in plan.candidates}


def only_control(fx: Fixture) -> None:
    """Nothing but the control plugin of a control=True fixture was found."""
    assert keys(fx.scan()) == {"control@m": ["zz"]}


def install_record(fx: Fixture, key: str, project_path: str, scope: str = "project") -> None:
    (fx.claude / "plugins").mkdir(exist_ok=True)
    path = fx.claude / "plugins" / "installed_plugins.json"
    current = json.loads(path.read_text()) if path.exists() else {"version": 2, "plugins": {}}
    current["plugins"].setdefault(key, []).append({"scope": scope, "projectPath": project_path})
    path.write_text(json.dumps(current))


class TestDetection:
    def test_a_key_enabled_in_settings_local_json_is_found(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        assert keys(fixture.scan()) == {"plug@m": ["alpha"]}

    def test_a_key_enabled_in_settings_json_is_found(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True}, ".claude/settings.json")
        assert keys(fixture.scan()) == {"plug@m": ["alpha"]}

    def test_project_and_local_scope_installs_for_the_checkout_are_found(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        here = str(fx.checkouts["alpha"])
        (fx.claude / "plugins").mkdir()
        (fx.claude / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "proj@m": [{"scope": "project", "projectPath": here}],
                        "loc@m": [{"scope": "local", "projectPath": here}],
                        "usr@m": [{"scope": "user"}],
                        "elsewhere@m": [{"scope": "project", "projectPath": str(fx.base / "other")}],
                    },
                }
            )
        )
        assert keys(fx.scan()) == {"loc@m": ["alpha"], "proj@m": ["alpha"]}

    def test_one_key_found_both_ways_names_the_project_once(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        install_record(fixture, "plug@m", str(fixture.checkouts["alpha"]), scope="local")
        assert keys(fixture.scan()) == {"plug@m": ["alpha"]}

    def test_a_declared_key_is_skipped(self, tmp_path: Path) -> None:
        entry = {"name": "plug", "runtimes": ["claude"], "owner": "claude-code", "wanted": False, "marketplace": "m"}
        fx = Fixture(tmp_path, plugins=[entry], control=True)
        fx.enable("alpha", {"plug@m": True})
        only_control(fx)

    def test_a_delivered_key_is_skipped(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, control=True)
        fx.enable("alpha", {"plug@m": True})
        (fx.checkouts["alpha"] / ".claude" / "stratarc-delivered.json").write_text(json.dumps({"project_plugins": ["plug@m"]}))
        only_control(fx)

    def test_the_delivered_manifest_is_named_for_the_engine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRATARC_NAME", "acme")
        fx = Fixture(tmp_path, control=True)
        fx.enable("alpha", {"plug@m": True})
        (fx.checkouts["alpha"] / ".claude" / "acme-delivered.json").write_text(json.dumps({"project_plugins": ["plug@m"]}))
        only_control(fx)
        assert str(ingest.delivered_manifest(fx.root)) == ".claude/acme-delivered.json"

    def test_a_false_value_is_skipped(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, control=True)
        fx.enable("alpha", {"plug@m": False})
        only_control(fx)

    def test_a_key_enabled_at_user_scope_is_noted_and_skipped(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"plug@m": True}}))
        plan = fixture.scan()
        assert plan.candidates == []
        assert any("user scope" in note and "plug@m" in note for note in plan.notes), plan.notes

    def test_a_user_scope_key_set_false_does_not_block_ingestion(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"plug@m": False}}))
        assert keys(fixture.scan()) == {"plug@m": ["alpha"]}

    def test_a_public_target_is_skipped(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "published"), control=True)
        (fx.root / "projects-root" / "public-targets.json").write_text(json.dumps(["published"]))
        fx.enable("published", {"plug@m": True})
        only_control(fx)

    def test_a_checkout_git_cannot_read_is_skipped(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, control=True)
        fx.enable("alpha", {"plug@m": True})
        shutil.rmtree(fx.checkouts["alpha"] / ".git")
        (fx.checkouts["alpha"] / ".git").mkdir()
        only_control(fx)

    def test_an_inactive_project_is_skipped(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, control=True)
        (fx.root / "control-plane.md").write_text(control_plane_text(["alpha", "zz"], inactive=("alpha",)))
        fx.enable("alpha", {"plug@m": True})
        only_control(fx)

    def test_a_source_tree_is_never_scanned(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "elsewhere"), control=True)
        checkout = fx.checkouts["elsewhere"]
        (checkout / "projects-root").mkdir()
        (checkout / "control-plane.md").write_text("")
        fx.enable("elsewhere", {"plug@m": True})
        only_control(fx)

    def test_a_key_in_two_projects_is_one_candidate_naming_both(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta", "gamma"))
        fx.enable("alpha", {"plug@m": True})
        fx.enable("gamma", {"plug@m": True})
        assert keys(fx.scan()) == {"plug@m": ["alpha", "gamma"]}

    def test_the_default_projects_directory_comes_from_the_environment(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        assert keys(ingest.scan(fixture.root, claude=fixture.claude, today=TODAY)) == {"plug@m": ["alpha"]}

    def test_the_default_claude_home_is_under_the_engine_home(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"plug@m": True}}))
        assert ingest.scan(fixture.root, projects=fixture.projects, today=TODAY).candidates == []


class TestInstalledRecords:
    def test_an_install_the_project_set_false_in_settings_local_json_is_not_ingested(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, control=True)
        install_record(fx, "off@m", str(fx.checkouts["alpha"]))
        fx.enable("alpha", {"off@m": False})
        only_control(fx)

    def test_an_install_the_project_set_false_in_settings_json_is_not_ingested(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, control=True)
        install_record(fx, "off@m", str(fx.checkouts["alpha"]), scope="local")
        fx.enable("alpha", {"off@m": False}, ".claude/settings.json")
        only_control(fx)

    def test_a_local_false_overrides_a_shared_true(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, control=True)
        fx.enable("alpha", {"off@m": True}, ".claude/settings.json")
        fx.enable("alpha", {"off@m": False})
        only_control(fx)

    def test_an_install_without_a_false_is_still_ingested(self, fixture: Fixture) -> None:
        install_record(fixture, "on@m", str(fixture.checkouts["alpha"]))
        assert keys(fixture.scan()) == {"on@m": ["alpha"]}

    def test_a_trailing_slash_and_a_symlinked_project_path_both_match_the_checkout(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        link = fx.base / "via-link"
        link.symlink_to(fx.projects, target_is_directory=True)
        install_record(fx, "slash@m", str(fx.checkouts["alpha"]) + "/")
        install_record(fx, "link@m", str(link / "active" / "beta"), scope="local")
        assert keys(fx.scan()) == {"slash@m": ["alpha"], "link@m": ["beta"]}

    def test_a_project_path_naming_a_different_checkout_does_not_match(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        install_record(fx, "on@m", str(fx.checkouts["beta"]))
        assert keys(fx.scan()) == {"on@m": ["beta"]}


class TestRecovery:
    """A declare not followed by its opt-in leaves a declared plugin no column selects,
    which the project render would strip; the next scan finishes the job."""

    ENTRY = {"name": "plug", "runtimes": ["claude"], "owner": "claude-code", "wanted": True, "description": "declared by hand", "marketplace": "m"}

    def test_a_declared_plugin_with_an_unselected_row_is_an_opt_in_only_candidate(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, plugin_rows=("plug",), plugins=[self.ENTRY])
        fx.enable("alpha", {"plug@m": True})
        plan = fx.scan()
        assert keys(plan) == {"plug@m": ["alpha"]}
        assert plan.candidates[0].declared
        assert plan.would_declare() == ["would select plugin plug@m for alpha"]
        before = (fx.root / "components.json").read_bytes()
        assert ingest.declare(fx.root, plan, TODAY) == ["plugins: selected plug@m for alpha; commit control-plane.md"]
        assert (fx.root / "components.json").read_bytes() == before
        assert ingest.opt_in(fx.root, plan) == []
        cp = ControlPlane.load(fx.root / "control-plane.md")
        assert cp.enabled("alpha", "plugin:plug")
        assert not cp.enabled("global", "plugin:plug")
        assert fx.scan().candidates == []

    def test_a_declared_plugin_whose_row_does_not_exist_yet_is_recovered_too(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, plugins=[self.ENTRY])
        fx.enable("alpha", {"plug@m": True})
        assert keys(fx.scan()) == {"plug@m": ["alpha"]}

    def test_recovery_is_not_keyed_on_the_description(self, tmp_path: Path) -> None:
        entry = dict(self.ENTRY, description="Added by hand to nowhere; ingested 1999-01-01.")
        fx = Fixture(tmp_path, plugin_rows=("plug",), plugins=[entry])
        fx.enable("alpha", {"plug@m": True})
        assert keys(fx.scan()) == {"plug@m": ["alpha"]}

    def test_a_row_selected_in_any_column_is_left_alone(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"), plugin_rows=("plug",), plugins=[self.ENTRY], control=True)
        fx.enable("alpha", {"plug@m": True})
        set_cell(fx.root / "control-plane.md", "plugin:plug", "beta")
        only_control(fx)

    def test_a_plugin_not_enabled_by_hand_or_already_delivered_is_not_recovered(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"), plugin_rows=("plug",), plugins=[self.ENTRY], control=True)
        fx.enable("alpha", {"plug@m": False})
        fx.enable("beta", {"plug@m": True})
        (fx.checkouts["beta"] / ".claude" / "stratarc-delivered.json").write_text(json.dumps({"project_plugins": ["plug@m"]}))
        only_control(fx)

    def test_an_unwanted_declaration_is_not_recovered(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, plugins=[dict(self.ENTRY, wanted=False)], control=True)
        fx.enable("alpha", {"plug@m": True})
        only_control(fx)


class TestRefusals:
    def test_a_key_without_a_marketplace_is_refused_with_a_note(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"bare": True})
        plan = fixture.scan()
        assert plan.candidates == []
        assert any("bare" in note and "marketplace" in note for note in plan.notes), plan.notes

    def test_a_name_declared_under_another_marketplace_is_refused_with_a_note(self, tmp_path: Path) -> None:
        entry = {"name": "plug", "runtimes": ["claude"], "owner": "claude-code", "wanted": True, "marketplace": "old"}
        fx = Fixture(tmp_path, plugins=[entry])
        fx.enable("alpha", {"plug@new": True})
        plan = fx.scan()
        assert plan.candidates == []
        assert any("plug@new" in note and "bare name" in note for note in plan.notes), plan.notes

    def test_two_found_keys_sharing_a_name_are_both_refused(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        fx.enable("alpha", {"plug@one": True})
        fx.enable("beta", {"plug@two": True})
        plan = fx.scan()
        assert plan.candidates == []
        assert len(plan.notes) == 2, plan.notes

    def test_declare_refuses_an_entry_that_would_leave_the_source_invalid(self, fixture: Fixture) -> None:
        before = (fixture.root / "components.json").read_bytes()
        plan = ingest.Plan(candidates=[ingest.Candidate("bare", "bare", None, ["alpha"])])
        with pytest.raises(ValueError):
            ingest.declare(fixture.root, plan, TODAY)
        assert (fixture.root / "components.json").read_bytes() == before


class TestFailClosed:
    """An input that exists but cannot be read withholds every candidate: declaring a key owns it in every project."""

    def test_an_unreadable_settings_file_in_one_project_withholds_a_key_found_in_another(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        fx.enable("alpha", {"plug@m": True})
        (fx.checkouts["beta"] / ".claude").mkdir()
        (fx.checkouts["beta"] / ".claude" / "settings.local.json").write_text("{not json")
        plan = fx.scan()
        assert plan.candidates == []
        assert any("withheld" in note and "beta" in note for note in plan.notes), plan.notes

    def test_a_non_object_settings_file_withholds_too(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        fx.enable("alpha", {"plug@m": True})
        (fx.checkouts["beta"] / ".claude").mkdir()
        (fx.checkouts["beta"] / ".claude" / "settings.json").write_text("[]")
        assert fx.scan().candidates == []

    def test_an_unreadable_install_record_withholds(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.claude / "plugins").mkdir()
        (fixture.claude / "plugins" / "installed_plugins.json").write_text("{")
        plan = fixture.scan()
        assert plan.candidates == []
        assert any("installed_plugins.json" in note for note in plan.notes), plan.notes

    def test_an_unreadable_user_settings_file_withholds(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.claude / "settings.json").write_text("{")
        plan = fixture.scan()
        assert plan.candidates == []
        assert any("user settings.json" in note for note in plan.notes), plan.notes

    def test_an_unreadable_delivered_manifest_withholds(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.checkouts["alpha"] / ".claude" / "stratarc-delivered.json").write_text("{")
        assert fixture.scan().candidates == []

    def test_an_unreadable_components_manifest_withholds_with_a_note(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.root / "components.json").write_text("{")
        plan = fixture.scan()
        assert plan.candidates == []
        assert any("components.json" in note for note in plan.notes), plan.notes

    def test_unreadable_public_targets_withhold_with_a_note(self, fixture: Fixture) -> None:
        fixture.enable("alpha", {"plug@m": True})
        (fixture.root / "projects-root" / "public-targets.json").write_text("{")
        plan = fixture.scan()
        assert plan.candidates == []
        assert any("public targets" in note for note in plan.notes), plan.notes

    def test_a_failed_write_of_components_json_leaves_it_as_it_was(self, fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        fixture.enable("alpha", {"plug@m": True})
        plan = fixture.scan()
        before = fixture.source_bytes()

        def refuse(self: Path, target: Path) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "replace", refuse)
        with pytest.raises(ValueError, match="nothing written"):
            ingest.declare(fixture.root, plan, TODAY)
        monkeypatch.undo()
        assert fixture.source_bytes() == before
        assert sorted(path.name for path in fixture.root.iterdir()) == ["components.json", "control-plane.md", "projects-root"]

    def test_opt_in_reports_every_cell_it_cannot_set_and_sets_the_rest(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"), plugin_rows=("plug",))
        plan = ingest.Plan(candidates=[ingest.Candidate("plug@m", "plug", "m", ["alpha", "nobody", "beta"])])
        failures = ingest.opt_in(fx.root, plan)
        assert len(failures) == 1 and "nobody" in failures[0]
        cp = ControlPlane.load(fx.root / "control-plane.md")
        assert cp.enabled("alpha", "plugin:plug") and cp.enabled("beta", "plugin:plug")

    def test_opt_in_names_a_missing_row_and_an_unreadable_file(self, fixture: Fixture) -> None:
        plan = ingest.Plan(candidates=[ingest.Candidate("plug@m", "plug", "m", ["alpha"])])
        assert "no row plugin:plug" in ingest.opt_in(fixture.root, plan)[0]
        (fixture.root / "control-plane.md").write_bytes(b"\xff\xfe| option |\n")
        failures = ingest.opt_in(fixture.root, plan)
        assert len(failures) == 1 and "plug@m not selected for alpha" in failures[0]


class TestDeclare:
    def test_the_entry_shape(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        fx.enable("alpha", {"my-plug@some-market": True})
        fx.enable("beta", {"my-plug@some-market": True})
        lines = ingest.declare(fx.root, fx.scan(), TODAY)
        assert lines == [
            "plugins: declared my-plug@some-market for alpha; commit components.json and control-plane.md",
            "plugins: declared my-plug@some-market for beta; commit components.json and control-plane.md",
        ]
        assert fx.manifest()["plugins"] == [
            {
                "name": "my-plug",
                "runtimes": ["claude"],
                "owner": "claude-code",
                "wanted": True,
                "description": "Added by hand to alpha, beta; ingested 2026-10-09.",
                "marketplace": "some-market",
            }
        ]
        assert validate(fx.manifest()) == []

    def test_a_marketplace_containing_an_at_sign_splits_on_the_last_one(self) -> None:
        assert ingest._split("name@a@b") == ("name@a", "b")
        assert ingest._split("name") == ("name", None)

    def test_appending_to_an_inline_empty_list_keeps_every_other_byte(self, fixture: Fixture) -> None:
        text = (INGEST_FIXTURES / "components-inline-empty.json").read_text()
        (fixture.root / "components.json").write_text(text)
        fixture.enable("alpha", {"plug@m": True})
        ingest.declare(fixture.root, fixture.scan(), TODAY)
        updated = (fixture.root / "components.json").read_text()
        assert updated.startswith(text.split('"plugins": [],')[0])
        assert updated.endswith('  ],\n  "services": []\n}\n')
        assert json.loads(updated)["plugins"][0]["name"] == "plug"

    def test_appending_after_a_one_line_entry_matches_that_style(self, fixture: Fixture) -> None:
        text = (INGEST_FIXTURES / "components-one-line.json").read_text()
        first = json.dumps(json.loads(text)["plugins"][0])
        (fixture.root / "components.json").write_text(text)
        fixture.enable("alpha", {"plug@m": True})
        ingest.declare(fixture.root, fixture.scan(), TODAY)
        updated = (fixture.root / "components.json").read_text()
        assert f'    {first},\n    {{"name": "plug"' in updated
        assert updated.endswith('\n  ],\n  "services": []\n}\n')
        assert len(json.loads(updated)["plugins"]) == 2

    def test_appending_after_a_multiline_entry_matches_that_style(self, fixture: Fixture) -> None:
        (fixture.root / "components.json").write_text((INGEST_FIXTURES / "components-multiline.json").read_text())
        fixture.enable("alpha", {"plug@m": True})
        ingest.declare(fixture.root, fixture.scan(), TODAY)
        updated = (fixture.root / "components.json").read_text()
        assert '    },\n    {\n      "name": "plug",\n      "runtimes": ["claude"],' in updated
        assert updated.endswith("    }\n  ]\n}\n")
        assert validate(json.loads(updated)) == []

    @pytest.mark.parametrize("text", ['{\n  "version": 1\n}\n', '{"version": 1}', '{\n  "version": 1,\n  "services": []\n}'])
    def test_a_manifest_with_no_plugins_list_gains_one(self, fixture: Fixture, text: str) -> None:
        (fixture.root / "components.json").write_text(text)
        fixture.enable("alpha", {"plug@m": True})
        ingest.declare(fixture.root, fixture.scan(), TODAY)
        updated = (fixture.root / "components.json").read_text()
        assert [entry["name"] for entry in json.loads(updated)["plugins"]] == ["plug"]
        assert {key: value for key, value in json.loads(updated).items() if key != "plugins"} == {key: value for key, value in json.loads(text).items()}
        assert validate(json.loads(updated)) == []

    def test_the_file_mode_survives_the_atomic_write(self, fixture: Fixture) -> None:
        path = fixture.root / "components.json"
        path.chmod(0o640)
        fixture.enable("alpha", {"plug@m": True})
        ingest.declare(fixture.root, fixture.scan(), TODAY)
        assert path.stat().st_mode & 0o777 == 0o640

    def test_zero_candidates_write_nothing_and_print_nothing(self, fixture: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
        before = fixture.source_bytes()
        assert ingest.main(["--root", str(fixture.root)]) == 0
        assert ingest.main(["--root", str(fixture.root), "--check"]) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")
        assert fixture.source_bytes() == before
        # The control: the same call is not silent once a plugin is hand-added.
        fixture.enable("alpha", {"plug@m": True})
        assert ingest.main(["--root", str(fixture.root), "--check"]) == 1
        assert "plug@m" in capsys.readouterr().out


class TestReportOnly:
    def test_check_reports_writes_nothing_and_signals_stale(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        fx.enable("alpha", {"plug@m": True})
        fx.enable("beta", {"plug@m": True})
        before = fx.source_bytes()
        assert ingest.main(["--root", str(fx.root), "--check"]) == 1
        assert capsys.readouterr().out.splitlines() == ["would declare plugin plug@m for alpha", "would declare plugin plug@m for beta"]
        assert fx.source_bytes() == before

    def test_a_linked_worktree_reports_and_writes_nothing(self, fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        fixture.enable("alpha", {"plug@m": True})
        before = fixture.source_bytes()
        monkeypatch.setattr(ingest, "is_linked_worktree", lambda root: True)
        assert ingest.main(["--root", str(fixture.root)]) == 0
        assert capsys.readouterr().out.splitlines() == ["would declare plugin plug@m for alpha"]
        assert fixture.source_bytes() == before

    def test_a_stable_deploy_reports_writes_nothing_and_says_where_to_declare(
        self, fixture: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (fixture.root / "components.json").write_text(json.dumps({"version": 1, "environments": {"mac": {"ref": "stable"}}, "plugins": []}) + "\n")
        fixture.enable("alpha", {"plug@m": True})
        before = fixture.source_bytes()
        assert ingest.deploys_from_stable(fixture.root) is False
        monkeypatch.setenv("STRATARC_ENVIRONMENT", "mac")
        assert ingest.deploys_from_stable(fixture.root) is True
        assert ingest.main(["--root", str(fixture.root)]) == 0
        captured = capsys.readouterr()
        assert captured.out.splitlines() == ["would declare plugin plug@m for alpha"]
        assert "declare these plugins on main" in captured.err
        assert fixture.source_bytes() == before

    def test_an_undeclared_environment_is_not_stable(self, fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRATARC_ENVIRONMENT", "nowhere")
        assert ingest.deploys_from_stable(fixture.root) is False

    def test_is_linked_worktree_tells_a_worktree_from_its_primary_checkout(self, tmp_path: Path) -> None:
        primary = tmp_path / "primary"
        primary.mkdir()
        run_git("-C", str(primary), "init", "-q", "-b", "main")
        run_git("-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
        linked = tmp_path / "linked"
        run_git("-C", str(primary), "worktree", "add", "-q", "-b", "side", str(linked))
        assert not ingest.is_linked_worktree(primary)
        assert ingest.is_linked_worktree(linked)
        assert not ingest.is_linked_worktree(tmp_path)


class TestReconcileOrder:
    """Declare, reconcile, select, in the order sync runs them, with the real reconciler."""

    def source(self, fx: Fixture) -> None:
        shutil.rmtree(fx.root)
        shutil.copytree(SYNC_SOURCE, fx.root)
        (fx.root / "projects-root" / "alpha").mkdir(exist_ok=True)
        (fx.root / "projects-root" / "alpha" / "AGENTS.md").write_text("# alpha\n")

    def reconcile(self, fx: Fixture) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "stratarc.reconcile", "--root", str(fx.root)],
            capture_output=True,
            text=True,
            env=sync.child_environment(fx.root),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_plugin_is_selected_for_its_project_only(self, tmp_path: Path) -> None:
        fx = Fixture(tmp_path, projects=("alpha", "beta"))
        self.source(fx)
        fx.enable("alpha", {"plug@m": True, "mine@x": True})
        self.reconcile(fx)
        plan = fx.scan()
        assert keys(plan) == {"mine@x": ["alpha"], "plug@m": ["alpha"]}
        ingest.declare(fx.root, plan, TODAY)
        self.reconcile(fx)
        assert not ControlPlane.load(fx.root / "control-plane.md").enabled("alpha", "plugin:plug")
        assert ingest.opt_in(fx.root, plan) == []
        self.reconcile(fx)
        cp = ControlPlane.load(fx.root / "control-plane.md")
        for name in ("plug", "mine"):
            assert cp.enabled("alpha", f"plugin:{name}")
            assert not cp.enabled("global", f"plugin:{name}")
            assert not cp.enabled("beta", f"plugin:{name}")
        assert fx.scan().candidates == []


class TestSyncCallSite:
    """`stratarc sync` driven in process against a committed fixture source, with the real reconciler."""

    @pytest.fixture
    def run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        fx = Fixture(tmp_path, projects=("alpha",))
        shutil.rmtree(fx.root)
        shutil.copytree(SYNC_SOURCE, fx.root)
        (fx.root / "projects-root" / "alpha").mkdir(exist_ok=True)
        (fx.root / "projects-root" / "alpha" / "AGENTS.md").write_text("# alpha\n")
        # The generated control plane cites the configuration file, so a valid tree carries one.
        (fx.root / "stratarc.toml").write_text('name = "stratarc"\n')
        fx.enable("alpha", {"plug@m": True})
        monkeypatch.setattr(sync, "detected", lambda runtimes=None: {})
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
        first = subprocess.run(
            [sys.executable, "-m", "stratarc.reconcile", "--root", str(fx.root)],
            capture_output=True,
            text=True,
            env=sync.child_environment(fx.root),
        )
        assert first.returncode == 0, first.stdout + first.stderr
        run_git("-C", str(fx.root), "init", "-q", "-b", "main")
        commit_all(fx.root)

        def call(*argv: str, root: Path | None = None) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = sync.main(["--root", str(root or fx.root), *argv])
            return code, out.getvalue(), err.getvalue()

        return fx, call

    def test_check_is_stale_and_writes_nothing(self, run) -> None:
        fx, call = run
        before = fx.source_bytes()
        code, out, err = call("--check")
        assert code == 1, out + err
        assert "sync: would declare plugin plug@m for alpha" in out
        assert fx.source_bytes() == before

    def test_an_applying_run_declares_selects_and_keeps_the_plugin_in_its_project(self, run) -> None:
        fx, call = run
        code, out, err = call()
        assert code == 0, out + err
        assert "sync: plugins: declared plug@m for alpha; commit components.json and control-plane.md" in out
        assert [entry["name"] for entry in fx.manifest()["plugins"]] == ["plug"]
        cp = ControlPlane.load(fx.root / "control-plane.md")
        assert cp.enabled("alpha", "plugin:plug")
        assert not cp.enabled("global", "plugin:plug")
        local = json.loads((fx.checkouts["alpha"] / ".claude" / "settings.local.json").read_text())
        assert local["enabledPlugins"]["plug@m"] is True
        code, out, err = call()
        assert code == 0, out + err
        assert "plugins:" not in out

    def test_a_run_that_died_after_declaring_is_recovered_by_the_next_run(self, run) -> None:
        fx, call = run
        ingest.declare(fx.root, fx.scan(), TODAY)
        assert ControlPlane.load(fx.root / "control-plane.md").enabled_ids("alpha", "plugin:") == set()
        code, out, err = call()
        assert code == 0, out + err
        assert "sync: plugins: selected plug@m for alpha; commit control-plane.md" in out
        assert len(fx.manifest()["plugins"]) == 1
        assert ControlPlane.load(fx.root / "control-plane.md").enabled("alpha", "plugin:plug")
        local = json.loads((fx.checkouts["alpha"] / ".claude" / "settings.local.json").read_text())
        assert local["enabledPlugins"]["plug@m"] is True

    def test_a_failed_opt_in_stops_the_run_before_any_project_is_touched(self, run, monkeypatch: pytest.MonkeyPatch) -> None:
        fx, call = run
        monkeypatch.setattr(
            ingest, "opt_in", lambda root, plan: [f"plugins: {c.key} not selected for {p}: boom" for c in plan.candidates for p in c.projects]
        )
        code, out, err = call()
        assert code == 2, out + err
        assert "plug@m not selected for alpha" in err
        assert "refusing to deploy projects" in err
        assert not (fx.checkouts["alpha"] / ".claude" / "stratarc-delivered.json").exists()

    def test_a_failed_declare_stops_the_run_with_nothing_written(self, run, monkeypatch: pytest.MonkeyPatch) -> None:
        fx, call = run
        def refuse(root, plan, today=None):
            raise ValueError("components.json would be invalid after ingestion; nothing written")

        monkeypatch.setattr(ingest, "declare", refuse)
        before = fx.source_bytes()
        code, out, err = call()
        assert code == 2, out + err
        assert "sync: plugins: components.json would be invalid" in err
        assert fx.source_bytes() == before
        assert not (fx.checkouts["alpha"] / ".claude" / "stratarc-delivered.json").exists()

    def test_an_unreadable_project_file_withholds_the_declaration_for_every_project(self, run, monkeypatch: pytest.MonkeyPatch) -> None:
        fx, call = run
        (fx.checkouts["alpha"] / ".claude" / "settings.json").write_text("{not json")
        before = fx.source_bytes()
        # The project render reads the same file; only the ingest is under test here.
        monkeypatch.setattr(sync, "run_projects", lambda root, check: 0)
        code, out, err = call()
        assert "withheld" in err and "alpha" in err
        assert "would declare" not in out
        assert fx.source_bytes() == before

    def test_a_linked_worktree_run_reports_and_writes_nothing(self, run, tmp_path: Path) -> None:
        fx, call = run
        linked = tmp_path / "linked-source"
        run_git("-C", str(fx.root), "worktree", "add", "-q", "-b", "side", str(linked))
        before = (linked / "components.json").read_bytes(), (linked / "control-plane.md").read_bytes()
        code, out, err = call("--allow-branch", "side", root=linked)
        assert code == 0, out + err
        assert "sync: would declare plugin plug@m for alpha" in out
        assert before == ((linked / "components.json").read_bytes(), (linked / "control-plane.md").read_bytes())

    def test_a_stable_deploy_reports_and_leaves_the_tree_clean(self, run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fx, call = run
        (fx.root / "components.json").write_text(json.dumps({"version": 1, "environments": {"mac": {"ref": "stable"}}}, indent=2) + "\n")
        run_git("-C", str(fx.root), "switch", "-q", "-c", "stable")
        commit_all(fx.root)
        origin = tmp_path / "origin.git"
        run_git("init", "-q", "--bare", str(origin))
        run_git("-C", str(fx.root), "remote", "add", "origin", str(origin))
        run_git("-C", str(fx.root), "push", "-q", "origin", "stable")
        monkeypatch.setenv("STRATARC_ENVIRONMENT", "mac")
        # The derived files a first run regenerates are promoted before the plugin check.
        call()
        commit_all(fx.root)
        run_git("-C", str(fx.root), "push", "-q", "origin", "stable")
        before = fx.source_bytes()
        code, out, err = call()
        assert code == 0, out + err
        assert "sync: would declare plugin plug@m for alpha" in out
        assert "declare these plugins on main" in err
        assert fx.source_bytes() == before
        status = subprocess.run(["git", "-C", str(fx.root), "status", "--porcelain"], capture_output=True, text=True, check=True)
        assert status.stdout == ""
