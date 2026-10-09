"""Tests for ControlPlane parsing: the option tables, the rules-table tier column and checkout discovery."""

from __future__ import annotations

import pytest

from agentrc.control_plane import ControlPlane, main, set_cell

OLD_SHAPE = """\
# control-plane

## rules

| option | global | demo |
|---|---|---|
| [rule:repo-scope](rules/repo-scope.md) | x | x |
"""

NEW_SHAPE = """\
# control-plane

## rules

| option | tier | global | demo |
|---|---|---|---|
| [rule:repo-scope](rules/repo-scope.md) | global | x | x |
| [rule:macos-shell](rules/macos-shell.md) | project | | x |

## instruction-file

| option | global | demo |
|---|---|---|
| [AGENTS.md](AGENTS.md) | x | x |

## projects

| project | status | tier | template |
|---|---|---|---|
| demo | active | strict | - |
| old | archived | normal | base |
"""


@pytest.fixture
def load(tmp_path):
    def _load(text: str) -> ControlPlane:
        path = tmp_path / "control-plane.md"
        path.write_text(text, encoding="utf-8")
        return ControlPlane.load(path)

    return _load


class TestLoadDefaultPath:
    def test_load_reads_the_source_roots_control_plane(self, source_root):
        (source_root / "control-plane.md").write_text(OLD_SHAPE, encoding="utf-8")
        cp = ControlPlane.load()
        assert cp.path == source_root / "control-plane.md"
        assert cp.columns == ["global", "demo"]

    def test_a_missing_file_yields_an_empty_plane(self, source_root):
        cp = ControlPlane.load()
        assert cp.columns == [] and cp.rows == []


class TestFindCheckoutHome:
    """find_checkout's default base hangs under paths.home(), so AGENTRC_HOME redirects it like every runtime target."""

    def test_checkouts_are_found_under_the_overridden_home(self, agentrc_home):
        checkout = agentrc_home / "projects" / "active" / "demo"
        (checkout / ".git").mkdir(parents=True)
        (agentrc_home / "projects" / "active" / "plain").mkdir()
        assert ControlPlane().find_checkout("demo") == [checkout]
        assert ControlPlane().find_checkout("plain") == []

    def test_trash_and_pool_trees_are_not_checkouts(self, agentrc_home):
        for hidden in (".trash", "_pool"):
            (agentrc_home / "projects" / hidden / "demo" / ".git").mkdir(parents=True)
        assert ControlPlane().find_checkout("demo") == []

    def test_an_explicit_root_wins(self, agentrc_home, tmp_path):
        base = tmp_path / "elsewhere"
        (base / "active" / "demo" / ".git").mkdir(parents=True)
        assert ControlPlane().find_checkout("demo", base) == [base / "active" / "demo"]


class TestTierColumnParsing:
    def test_old_header_shape_has_no_tier_column_and_parses_as_before(self, load):
        cp = load(OLD_SHAPE)
        assert cp.columns == ["global", "demo"]
        assert cp.rule_tiers == {}
        assert cp.enabled("global", "rule:repo-scope")
        assert cp.enabled("demo", "rule:repo-scope")

    def test_new_header_shape_extracts_tier_and_keeps_columns_aligned(self, load):
        cp = load(NEW_SHAPE)
        assert cp.columns == ["global", "demo"]
        assert cp.rule_tiers["rule:repo-scope"] == "global"
        assert cp.rule_tiers["rule:macos-shell"] == "project"
        assert cp.rule_tier("rule:repo-scope") == "global"
        assert cp.rule_tier("rule:unknown") == ""
        assert cp.enabled("global", "rule:repo-scope")
        assert cp.enabled("demo", "rule:repo-scope")
        assert not cp.enabled("global", "rule:macos-shell")
        assert cp.enabled("demo", "rule:macos-shell")

    def test_tier_never_appears_as_a_data_column(self, load):
        cp = load(NEW_SHAPE)
        assert "tier" not in cp.columns
        assert "tier" not in cp.projects()

    def test_a_plain_table_after_a_tiered_one_is_unaffected(self, load):
        """The tier column is per table, decided fresh at each header row."""
        cp = load(NEW_SHAPE)
        assert cp.enabled("global", "AGENTS.md")
        assert cp.enabled("demo", "AGENTS.md")


class TestQueries:
    def test_enabled_ids_strips_the_prefix(self, load):
        cp = load(NEW_SHAPE)
        assert cp.enabled_ids("demo", "rule:") == {"repo-scope", "macos-shell"}
        assert cp.enabled_ids("global", "rule:") == {"repo-scope"}

    def test_manifest_fields_and_defaults(self, load):
        cp = load(NEW_SHAPE)
        assert cp.status("demo") == "active"
        assert cp.tier("demo") == "strict"
        assert cp.template("demo") == "base"  # "-" reads as empty
        assert cp.status("missing") == "unlisted"
        assert cp.active_projects() == ["demo"]

    def test_option_labels_may_be_markdown_links(self, load):
        cp = load(OLD_SHAPE)
        assert cp.rows == ["rule:repo-scope"]


class TestSetCell:
    def test_sets_and_clears_one_cell_leaving_other_bytes(self, tmp_path):
        path = tmp_path / "control-plane.md"
        path.write_text(NEW_SHAPE, encoding="utf-8")
        assert set_cell(path, "rule:macos-shell", "global") is True
        assert ControlPlane.load(path).enabled("global", "rule:macos-shell")
        assert set_cell(path, "rule:macos-shell", "global") is False
        assert set_cell(path, "rule:macos-shell", "global", on=False) is True
        assert path.read_text(encoding="utf-8") == NEW_SHAPE.replace(
            "| [rule:macos-shell](rules/macos-shell.md) | project | | x |",
            "| [rule:macos-shell](rules/macos-shell.md) | project |  | x |",
        )

    def test_unknown_file_row_or_column_raises(self, tmp_path):
        path = tmp_path / "control-plane.md"
        with pytest.raises(KeyError):
            set_cell(path, "rule:repo-scope", "global")
        path.write_text(NEW_SHAPE, encoding="utf-8")
        with pytest.raises(KeyError, match="no row"):
            set_cell(path, "rule:nope", "global")
        with pytest.raises(KeyError, match="no column"):
            set_cell(path, "rule:repo-scope", "nope")


class TestMain:
    def test_main_reports_the_enabled_count(self, source_root, capsys):
        (source_root / "control-plane.md").write_text(NEW_SHAPE, encoding="utf-8")
        assert main(["demo"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("demo: 3 of 3 options enabled")
        assert "rule:macos-shell" in out
