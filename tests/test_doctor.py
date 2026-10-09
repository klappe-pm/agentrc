from __future__ import annotations

import os
from pathlib import Path

import pytest

from stratarc import __version__, doctor


def test_collect_on_a_healthy_install(stratarc_home: Path, source_root: Path):
    (source_root / "stratarc.toml").write_text(
        '[runtimes.claude]\nenabled = true\ntarget = "~/.claude"\n', encoding="utf-8"
    )

    data, problems = doctor.collect(root_flag=False, started_ms=12.34)

    assert problems == []
    assert data["stratarc"] == __version__
    assert data["start_ms"] == 12.3
    assert data["source_root"]["config_file"] is True
    assert data["source_root"]["resolved_from"] == "STRATARC_SOURCE"
    names = [runtime["name"] for runtime in data["runtimes"]]
    assert names == sorted(names) and len(names) == data["adapters"]
    claude = next(r for r in data["runtimes"] if r["name"] == "claude")
    assert claude == {"name": "claude", "enabled": True, "target": str(stratarc_home / ".claude"), "target_exists": False}


def test_a_runtime_absent_from_the_config_counts_as_enabled(stratarc_home: Path, source_root: Path):
    data, _ = doctor.collect(root_flag=False, started_ms=0)

    assert data["runtimes"] and all(r["enabled"] for r in data["runtimes"])


def test_source_root_origin_is_the_nearest_config_without_the_variable(
    stratarc_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    (root / "inner").mkdir(parents=True)
    (root / "stratarc.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv("STRATARC_SOURCE", raising=False)
    monkeypatch.chdir(root / "inner")

    data, problems = doctor.collect(root_flag=False, started_ms=0)

    assert problems == []
    assert data["source_root"]["path"] == str(root.resolve())
    assert data["source_root"]["resolved_from"] == "nearest stratarc.toml"


def test_a_missing_source_root_is_reported_not_raised(stratarc_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRATARC_SOURCE", str(tmp_path / "absent"))

    data, problems = doctor.collect(root_flag=True, started_ms=0)

    assert [p.id for p in problems] == ["msg-1001"]
    assert data["source_root"]["exists"] is False
    assert data["runtimes"] == []


def test_a_missing_home_is_reported(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "no-home"))

    data, problems = doctor.collect(root_flag=False, started_ms=0)

    assert [p.id for p in problems] == ["msg-1003"]
    assert data["home"]["exists"] is False and data["home"]["writable"] is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_render_lists_each_problem_with_its_recovery(stratarc_home: Path, source_root: Path):
    stratarc_home.chmod(0o500)
    try:
        data, problems = doctor.collect(root_flag=False, started_ms=0)
    finally:
        stratarc_home.chmod(0o700)

    text = doctor.render(data, problems)

    assert "not writable" in text
    assert "problem msg-1003" in text
    assert "--home" in text
    assert not text.rstrip().endswith("ok")
