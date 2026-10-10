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


def test_source_root_origin_is_the_active_source_when_nothing_nearer_names_one(
    stratarc_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    registered = tmp_path / "registered"
    registered.mkdir()
    (registered / "stratarc.toml").write_text("", encoding="utf-8")
    (stratarc_home / ".stratarc").mkdir()
    (stratarc_home / ".stratarc" / "sources.toml").write_text(f'active = "main"\n[sources.main]\npath = "{registered}"\n', encoding="utf-8")
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.delenv("STRATARC_SOURCE", raising=False)
    monkeypatch.chdir(bare)

    data, problems = doctor.collect(root_flag=False, started_ms=0)

    assert problems == []
    assert data["source_root"]["path"] == str(registered.resolve())
    assert data["source_root"]["resolved_from"] == "active source (sources.toml)"


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


def test_the_permissions_table_covers_every_command_and_only_provider_test_uses_the_network():
    from stratarc import cli

    rows = doctor.permissions_table()
    named = " ".join(row["command"] for row in rows)
    for command in ("init", "doctor", "sync", "check", "diff", "prune", "reconcile", "validate", "projects", "gen-rules-digest", "components", "config", "log", "verify", "provider", "adapter"):
        assert command in named, command
    assert set(cli.PASSTHROUGH) | set(cli.RESOURCES) <= set(named.replace("|", " ").split())
    assert [row["command"] for row in rows if row["network"]] == ["provider test"]
    for command in ("source init", "source use|move", "project add|edit|enable|disable", "project remove", "runtime enable|disable|target", "agent edit", "account add|edit|remove", "ui"):
        assert command in [row["command"] for row in rows], command
    ui_row = next(row for row in rows if row["command"] == "ui")
    assert ui_row["network"] is False and any("only what the commands it starts write" in item for item in ui_row["writes"])
    assert all(set(row) == {"command", "reads", "writes", "network", "runs"} for row in rows)
    text = doctor.render_permissions(rows)
    assert text.splitlines()[0].startswith("permissions:")
    assert "provider test" in text and "exit 3" in text


def test_the_permissions_table_lists_exactly_these_commands_in_order():
    assert [row["command"] for row in doctor.permissions_table()] == [
        "init", "doctor", "doctor --clean --yes", "doctor --report", "sync", "sync --verify --rollback-on-drift", "check", "diff", "prune",
        "reconcile", "validate", "projects", "gen-rules-digest", "components", "config get|list|explain", "log show|tail|explain|export",
        "log enable|disable|prune", "verify run", "verify last|show", "provider list|show", "provider add|edit|remove", "provider test",
        "api schema", "api serve", "adapter list|show|status", "adapter register|remove|deprecate", "source show|list", "source init",
        "source use|move", "project list|show", "project add|edit|enable|disable", "project remove", "runtime list|show",
        "runtime enable|disable|target", "agent list|show|explain", "agent edit", "account list|show", "account add|edit|remove", "ui",
    ]


def make_backup_versions(home: Path, count: int) -> list[Path]:
    directory = home / ".stratarc" / "backups" / "key"
    directory.mkdir(parents=True)
    made = []
    for n in range(count):
        path = directory / f"20200101T000000{n:06d}Z"
        path.write_text("x")
        made.append(path)
    return made


def test_clean_without_yes_removes_nothing(stratarc_home: Path):
    made = make_backup_versions(stratarc_home, 23)

    report = doctor.clean(False)

    assert report["applied"] is False and report["removed"] == []
    assert sorted(report["would_remove"]) == [f"backups/key/{p.name}" for p in made[:3]]
    assert all(path.exists() for path in made)
    assert "pass --yes" in doctor.render_clean(report)


def test_clean_with_yes_removes_the_old_versions_and_the_cache_only(stratarc_home: Path):
    from stratarc import home_layout

    made = make_backup_versions(stratarc_home, 21)
    home_layout.ensure_layout()
    (home_layout.cache_dir() / "scratch").write_text("x")
    (home_layout.providers_dir() / "p.json").write_text("{}")

    report = doctor.clean(True)

    assert report["applied"] is True
    assert sorted(report["removed"]) == sorted([f"backups/key/{made[0].name}", "cache/scratch"])
    assert not made[0].exists() and made[1].exists()
    assert (home_layout.providers_dir() / "p.json").is_file()
    assert (home_layout.layout_root() / "sources.toml").is_file()
    assert doctor.render_clean(report).startswith("clean: removed 2")


def test_a_clean_of_a_fresh_home_has_nothing_to_remove(stratarc_home: Path):
    assert doctor.render_clean(doctor.clean(True)) == "clean: nothing to remove (0 kept)"


def test_the_report_is_redacted_private_and_names_no_home_path(stratarc_home: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch):
    import json

    secret = "ghp_" + "a1B2c3D4e5" * 4
    monkeypatch.setenv("STRATARC_NOTE", secret)
    data, problems = doctor.collect(root_flag=False, started_ms=1)

    path = doctor.write_report(data, problems)

    assert path.parent == stratarc_home / ".stratarc" / "state" / "debug"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    text = path.read_text(encoding="utf-8")
    assert secret not in text and "[REDACTED" in text
    assert str(stratarc_home) not in text
    bundle = json.loads(text)
    assert bundle["stratarc"] == __version__ and bundle["problems"] == []
    assert "STRATARC_HOME" in bundle["environment"]
