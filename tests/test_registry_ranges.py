"""The recorded runtime version ranges of the bundled adapters and the version detection table."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stratarc import registry
from stratarc.adapters._common import runtime_registry
from stratarc.adapters._supports import DETECT, SUPPORTS

VERSIONS = Path(__file__).resolve().parent / "fixtures" / "registry" / "versions"
RUNTIMES = sorted(SUPPORTS)


def bump(version: str, index: int) -> str:
    """`version` with the part at `index` raised by one and later parts zeroed."""
    parts = [int(p) for p in version.split(".")]
    parts[index] += 1
    return ".".join(str(p) for p in parts[: index + 1] + [0] * (len(parts) - index - 1))


def lower_bound(spec: str) -> str:
    return re.search(r">=([\d.]+)", spec).group(1)


def upper_bound(spec: str) -> str:
    return re.search(r"<([\d.]+)", spec).group(1)


def below(version: str) -> str:
    """A version just under `version` (its last nonzero part lowered by one)."""
    parts = [int(p) for p in version.split(".")]
    index = max(i for i, p in enumerate(parts) if p > 0)
    parts[index] -= 1
    return ".".join(str(p) for p in parts)


def test_the_tables_cover_every_bundled_adapter() -> None:
    bundled = set(runtime_registry())
    assert set(SUPPORTS) == bundled
    assert set(DETECT) == bundled
    assert set(registry.bundled_manifests()) == bundled


def test_every_row_is_complete_and_consistent() -> None:
    for name, row in SUPPORTS.items():
        assert registry.RANGE_PATTERN.match(row["supports"]) and row["supports"] != "*", name
        assert row["tested_on"] == "2026-10-09" and row["exercised_by"] and row["evidence"], name
        assert registry.in_range(registry.parse_version(row["tested"]), row["supports"]), name


def test_bundled_manifests_carry_the_table() -> None:
    for name, manifest in registry.bundled_manifests().items():
        assert manifest["supports"] == SUPPORTS[name]["supports"]
        assert manifest["tested"] == SUPPORTS[name]["tested"]
        assert not registry.validate_manifest(manifest), name


@pytest.mark.parametrize("name", RUNTIMES)
def test_each_state_for_each_adapter(name: str, stratarc_home: Path) -> None:
    row = SUPPORTS[name]
    tested, low, high = row["tested"], lower_bound(row["supports"]), upper_bound(row["supports"])

    def state(installed: str | None) -> str:
        return {s.name: s for s in registry.status({name: installed})}[name].state

    assert state(tested) == registry.OK
    assert state(low) == registry.OK
    assert state(bump(tested, len(tested.split(".")) - 1)) == registry.OUTDATED
    assert state(bump(tested, 1)) == registry.OUTDATED
    assert state(below(low)) == registry.UNSUPPORTED
    assert state(high) == registry.UNSUPPORTED
    assert state(None) == registry.UNKNOWN


def test_other_adapters_stay_unknown_when_only_one_version_is_given(stratarc_home: Path) -> None:
    rows = {s.name: s.state for s in registry.status({"claude": SUPPORTS["claude"]["tested"]})}
    assert rows["claude"] == registry.OK
    assert {state for name, state in rows.items() if name != "claude"} == {registry.UNKNOWN}


def test_a_stored_bundled_snapshot_does_not_pin_old_ranges(stratarc_home: Path) -> None:
    registry.register_bundled()
    stored = registry._stored("codex")
    stored.write_text(stored.read_text().replace(SUPPORTS["codex"]["supports"], "*"))
    assert registry.get("codex")["supports"] == SUPPORTS["codex"]["supports"]


@pytest.mark.parametrize("name", RUNTIMES)
def test_detection_reads_the_recorded_output(name: str) -> None:
    command, arguments, _ = DETECT[name]
    text = (VERSIONS / f"{name}.txt").read_text()
    calls: list[list[str]] = []

    class Result:
        stdout = text
        stderr = ""

    def runner(argv, **kwargs):
        calls.append(argv)
        return Result()

    versions = registry.detect_versions(runner=runner, which=lambda c: "/bin/" + c if c == command else None)
    assert calls == [[command, *arguments]]
    assert versions[name] == SUPPORTS[name]["tested"]
    assert [v for runtime, v in versions.items() if runtime != name and v] == []


def test_detection_searches_stderr_and_ignores_unrelated_output() -> None:
    class Result:
        stdout = "Credentials are resolved from the environment.\n"
        stderr = "codex-cli 0.162.0\n"

    versions = registry.detect_versions(runner=lambda *a, **k: Result(), which=lambda c: "/bin/" + c if c == "codex" else None)
    assert versions["codex"] == "0.162.0"

    class Noise:
        stdout = "no version here\n"
        stderr = ""

    versions = registry.detect_versions(runner=lambda *a, **k: Noise(), which=lambda c: "/bin/" + c if c == "codex" else None)
    assert versions["codex"] is None
