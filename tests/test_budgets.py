"""Tests for agentrc.budgets: the budget schema, its four levels, and the check.

The units and the layering these tests pin: user defaults, then the project, then the work item's loadout, then the session, each able only to lower a limit set above it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentrc import budgets

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "budgets"


class TestValidate:
    def test_a_well_formed_budget_has_no_errors(self):
        budget = {
            "tool_calls": {"total": 400, "per_tool": {"Bash": 150}},
            "tokens": {"input": None, "output": 2_000_000, "cache_read": None, "cache_write": None, "total": None},
            "turns": 80,
            "wall_minutes": None,
            "skills": {"count": 12, "tokens": 6000},
            "mcp_servers": {"count": 3, "tools": 60},
            "rules": {"words": 3000},
            "hooks": {"count": 12},
            "warn_at": 0.8,
        }
        assert budgets.validate(budget) == []

    def test_unknown_keys_and_bad_values_are_reported(self):
        errors = budgets.validate({"tool_calls": {"totl": 3}, "turns": -1, "warn_at": 2, "dollars": 5})
        joined = "\n".join(errors)
        assert "tool_calls.totl" in joined
        assert "turns" in joined
        assert "warn_at" in joined
        # Dollars are recorded, never a limit.
        assert "dollars" in joined

    def test_a_boolean_is_not_a_count(self):
        assert budgets.validate({"turns": True})


class TestFleet:
    """budgets.fleet {model, concurrent} is a machine-wide cap, set only at the user level."""

    def test_a_fleet_block_is_valid_at_the_user_level(self):
        assert budgets.validate({"fleet": {"model": "example-model", "concurrent": 20}}) == []
        assert budgets.validate({"fleet": {"model": None, "concurrent": None}}) == []

    @pytest.mark.parametrize(
        "fleet",
        [
            {"model": "", "concurrent": 20},
            {"model": 5, "concurrent": 20},
            {"model": "example-model", "concurrent": 0},
            {"model": "example-model", "concurrent": True},
            {"model": "example-model", "concurrent": None},
            {"model": None, "concurrent": 20},
            {"model": "example-model", "concurrent": 20, "extra": 1},
        ],
    )
    def test_a_malformed_fleet_block_is_reported(self, fleet):
        assert budgets.validate({"fleet": fleet})

    @pytest.mark.parametrize(
        "budget",
        [{"fleet.concurrent": "abc"}, {"fleet.model": 5}, {"fleet.concurrent": 10}],
    )
    def test_a_flat_fleet_key_is_not_a_known_key(self, budget):
        assert budgets.validate(budget)

    @pytest.mark.parametrize(
        "flat_key, budget",
        [
            ("fleet.concurrent", {"fleet": {"model": "m", "concurrent": 5}, "fleet.concurrent": 30}),
            ("fleet.model", {"fleet": {"model": "m", "concurrent": 5}, "fleet.model": "other"}),
        ],
    )
    def test_a_flat_fleet_key_is_refused_even_beside_a_valid_nested_block(self, flat_key, budget):
        # Exactly one error, naming the flat key: the nested leaf, which walks to the identical dotted string, must not also be misreported as unknown, and the flat key must not be reported twice.
        assert budgets.validate(budget) == [f"{flat_key} is not a budget key; fleet is set only as a nested object"]

    def test_a_fleet_that_is_not_an_object_is_reported_once(self):
        assert budgets.validate({"fleet": "example"}) == ["fleet must be an object of model and concurrent"]

    def test_a_fleet_block_below_the_user_level_is_an_error(self):
        effective, errors = budgets.resolve(
            [
                ("user", {"fleet": {"model": "example-model", "concurrent": 20}}),
                ("loadout", {"fleet": {"model": "example-model", "concurrent": 50}}),
            ]
        )
        assert any("loadout" in error and "fleet" in error for error in errors), errors
        assert "fleet.concurrent" not in effective
        assert "fleet.model" not in effective

    @pytest.mark.parametrize("level", ["project", "loadout", "session"])
    def test_a_null_fleet_block_below_the_user_level_is_an_error(self, level):
        effective, errors = budgets.resolve(
            [("user", {"turns": 10}), (level, {"fleet": {"model": None, "concurrent": None}})]
        )
        assert effective == {"turns": 10}
        assert any(level in error and "fleet" in error for error in errors), errors

    def test_resolve_keeps_the_fleet_out_of_the_effective_limits(self):
        effective, errors = budgets.resolve(
            [("user", {"wall_minutes": 15, "fleet": {"model": "example-model", "concurrent": 20}})]
        )
        assert errors == []
        assert effective == {"wall_minutes": 15}

    def test_fleet_reads_the_user_level(self, tmp_path):
        assert budgets.fleet(tmp_path) is None
        manifest = tmp_path / "components.json"
        manifest.write_text(json.dumps({"budgets": {"fleet": {"model": None, "concurrent": None}}}), encoding="utf-8")
        assert budgets.fleet(tmp_path) is None
        manifest.write_text(
            json.dumps({"budgets": {"fleet": {"model": "example-model", "concurrent": 20}}}), encoding="utf-8"
        )
        assert budgets.fleet(tmp_path) == ("example-model", 20)

    def test_the_fixture_manifest_caps_the_example_model_at_20(self):
        assert budgets.fleet(FIXTURES) == ("example-model", 20)


class TestFlatten:
    def test_nested_limits_flatten_to_dotted_keys_and_nulls_drop(self):
        flat = budgets.flatten({"tool_calls": {"total": 5, "per_tool": {"Read": 2}}, "turns": None})
        assert flat == {"tool_calls.total": 5, "tool_calls.per_tool.Read": 2}


class TestResolve:
    def test_the_lowest_limit_across_levels_wins(self):
        effective, errors = budgets.resolve(
            [
                ("user", {"tool_calls": {"total": 500}, "turns": 100}),
                ("project", {"tool_calls": {"total": 300}}),
                ("loadout", {"turns": 40}),
                ("session", {}),
            ]
        )
        assert errors == []
        assert effective["tool_calls.total"] == 300
        assert effective["turns"] == 40

    def test_a_lower_level_cannot_raise_a_limit(self):
        effective, errors = budgets.resolve(
            [("user", {"tool_calls": {"total": 300}}), ("loadout", {"tool_calls": {"total": 900}})]
        )
        assert effective["tool_calls.total"] == 300
        assert len(errors) == 1
        assert "loadout" in errors[0]
        assert "tool_calls.total" in errors[0]
        assert "300" in errors[0]

    def test_a_level_can_set_a_limit_nothing_above_set(self):
        effective, errors = budgets.resolve([("user", {}), ("loadout", {"skills": {"count": 4}})])
        assert errors == []
        assert effective == {"skills.count": 4}

    def test_warn_at_is_a_threshold_not_a_limit_and_the_lowest_level_sets_it(self):
        effective, errors = budgets.resolve([("user", {"warn_at": 0.8}), ("loadout", {"warn_at": 0.9})])
        assert errors == []
        assert effective["warn_at"] == 0.9

    def test_an_invalid_level_is_reported_and_ignored(self):
        effective, errors = budgets.resolve([("user", {"turns": 10}), ("loadout", {"turns": "many"})])
        assert effective["turns"] == 10
        assert any("loadout" in e for e in errors)


class TestCheck:
    def test_over_and_warn_findings(self):
        effective = {"tool_calls.total": 100, "turns": 10, "skills.count": 4, "warn_at": 0.8}
        findings = budgets.check(effective, {"tool_calls.total": 85, "turns": 11, "skills.count": 2})
        by_key = {f.key: f for f in findings}
        assert by_key["turns"].state == "over"
        assert by_key["tool_calls.total"].state == "warn"
        assert "skills.count" not in by_key

    def test_exactly_at_the_limit_is_not_over(self):
        findings = budgets.check({"turns": 10}, {"turns": 10})
        assert [f.state for f in findings] == ["warn"]

    def test_a_measure_with_no_limit_is_never_a_finding(self):
        assert budgets.check({}, {"turns": 10_000}) == []

    def test_the_tokens_total_counts_every_kind(self):
        measured = budgets.token_measures({"in": 1, "out": 2, "cache_read": 30, "cache_write": 4})
        assert measured["tokens.total"] == 37
        assert measured["tokens.cache_read"] == 30


class TestLevels:
    def test_levels_read_from_their_files(self, tmp_path):
        (tmp_path / "components.json").write_text(json.dumps({"version": 1, "budgets": {"turns": 50}}))
        project = tmp_path / "projects-root" / "demo"
        project.mkdir(parents=True)
        (project / "budgets.json").write_text(json.dumps({"budgets": {"turns": 30}}))
        levels = budgets.levels(tmp_path, project="demo", loadout={"budgets": {"turns": 20}}, session={"turns": 10})
        assert [name for name, _ in levels] == ["user", "project", "loadout", "session"]
        effective, errors = budgets.resolve(levels)
        assert errors == []
        assert effective["turns"] == 10

    def test_missing_files_are_empty_levels(self, tmp_path):
        levels = budgets.levels(tmp_path, project="none")
        assert [budget for _, budget in levels] == [{}, {}, {}, {}]

    def test_the_fixture_manifest_is_the_user_level(self):
        levels = budgets.levels(FIXTURES, project="none")
        assert levels[0][1]["turns"] == 50
