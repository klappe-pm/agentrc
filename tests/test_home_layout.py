"""The `.stratarc` home layout: creation, stamping, newer-schema refusal, backups and cleaning."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stratarc import home_layout as layout

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ensure_layout_creates_every_directory_mode_0700(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    assert root == stratarc_home / ".stratarc"
    for name in layout.DIRECTORIES:
        assert (root / name).is_dir()
        assert mode(root / name) == 0o700
    assert mode(root) == 0o700


def test_ensure_layout_stamps_files_and_is_idempotent(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    for name in layout.STAMPED_FILES:
        assert tomllib.loads((root / name).read_text())["schema_version"] == layout.SCHEMA_VERSION
        assert mode(root / name) == 0o600
    (root / "config.toml").write_text('schema_version = 1\nlogging = true\n')
    layout.ensure_layout()
    assert "logging = true" in (root / "config.toml").read_text()


def test_dump_toml_round_trips_tables_and_lists() -> None:
    data = {"schema_version": 1, "active": "main", "sources": {"main": {"path": "/srv/src", "tags": ["a", "b"]}, "other": {"path": "x"}}}
    assert tomllib.loads(layout.dump_toml(data)) == data


def test_newer_schema_toml_is_never_rewritten(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    path = root / "config.toml"
    path.write_text("schema_version = 99\nfuture = true\n")
    before = path.read_bytes()
    with pytest.raises(layout.NewerSchemaError) as caught:
        layout.write_toml(path, {"logging": True})
    assert caught.value.exit == layout.UNAVAILABLE == 5
    assert path.read_bytes() == before
    assert not list((root / "backups").iterdir())


def test_newer_schema_json_is_never_rewritten(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    path = root / "providers" / "future.json"
    path.write_text(json.dumps({"schema_version": 2}))
    with pytest.raises(layout.NewerSchemaError):
        layout.safe_write(path, "{}")
    assert json.loads(path.read_text()) == {"schema_version": 2}


def test_safe_write_backs_up_the_replaced_file(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    path = root / "config.toml"
    first = layout.safe_write(path, 'schema_version = 1\na = "one"\n', now=NOW)
    assert first is not None  # the stamped file made at ensure_layout was replaced
    second = layout.safe_write(path, 'schema_version = 1\na = "two"\n', now=NOW + timedelta(seconds=1))
    assert second is not None and 'a = "one"' in second.read_text()
    assert 'a = "two"' in path.read_text()
    assert mode(path) == 0o600
    assert second.parent.name == "config.toml"


def test_safe_write_of_a_new_file_makes_no_backup(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    assert layout.safe_write(root / "state" / "new.json", "{}") is None
    assert not list(layout.backups_dir().glob("state__new.json/*"))


def under_umask(value: int):
    class Scope:
        def __enter__(self):
            self.old = os.umask(value)

        def __exit__(self, *exc):
            os.umask(self.old)

    return Scope()


def test_safe_write_keeps_home_files_private_by_default(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    with under_umask(0o022):
        layout.safe_write(root / "state" / "private.json", "{}")
        layout.safe_write(root / "state" / "private.json", "[]")
    assert mode(root / "state" / "private.json") == 0o600


def test_safe_write_to_a_source_root_keeps_the_mode_of_the_file_it_replaces(stratarc_home: Path, tmp_path: Path) -> None:
    path = tmp_path / "src" / "run.sh"
    path.parent.mkdir()
    path.write_text("one")
    path.chmod(0o755)
    with under_umask(0o022):
        backup = layout.safe_write(path, "two", private=False)
    assert path.read_text() == "two"
    assert mode(path) == 0o755
    assert backup is not None and mode(backup) == 0o600


def test_safe_write_to_a_source_root_creates_new_files_0644_and_dirs_0755(stratarc_home: Path, tmp_path: Path) -> None:
    existing = tmp_path / "src"
    existing.mkdir()
    existing.chmod(0o755)
    path = existing / "nested" / "AGENTS.md"
    with under_umask(0o022):
        assert layout.safe_write(path, "# x\n", private=False) is None
    assert mode(path) == 0o644
    assert mode(path.parent) == 0o755
    assert mode(existing) == 0o755


def test_safe_write_same_instant_does_not_overwrite_a_backup(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    path = root / "state" / "x.json"
    layout.safe_write(path, "1", now=NOW)
    layout.safe_write(path, "2", now=NOW)
    layout.safe_write(path, "3", now=NOW)
    names = sorted(p.read_text() for p in (layout.backups_dir() / "state__x.json").iterdir())
    assert names == ["1", "2"]


def make_backups(count: int, key: str = "state__x.json", *, newest_age_days: float = 40.0) -> list[Path]:
    directory = layout.backups_dir() / key
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for index in range(count):
        stamp = (NOW - timedelta(days=newest_age_days + index)).strftime(layout._STAMP_FORMAT)
        path = directory / stamp
        path.write_text(str(index))
        made.append(path)
    return made  # newest first


def test_clean_keeps_last_20_versions_even_when_old(stratarc_home: Path) -> None:
    layout.ensure_layout()
    made = make_backups(25)
    plan = layout.plan_clean(now=NOW)
    assert sorted(plan.removals) == sorted(made[20:])
    assert set(made[:20]) <= set(plan.kept)


def test_clean_keeps_anything_under_30_days_even_beyond_20(stratarc_home: Path) -> None:
    layout.ensure_layout()
    made = make_backups(30, newest_age_days=0.0)  # ages 0..29 days: all recent
    plan = layout.plan_clean(now=NOW)
    assert plan.removals == []
    made = make_backups(5, key="state__y.json", newest_age_days=29.0)  # 29..33 days, under 20 versions
    assert layout.plan_clean(now=NOW).removals == []


def test_clean_removes_only_with_yes(stratarc_home: Path) -> None:
    layout.ensure_layout()
    made = make_backups(25)
    dry = layout.clean(now=NOW)
    assert not dry.applied and dry.removed == []
    assert all(p.exists() for p in made)
    done = layout.clean(yes=True, now=NOW)
    assert done.applied and sorted(done.removed) == sorted(made[20:])
    assert not any(p.exists() for p in made[20:]) and all(p.exists() for p in made[:20])


def test_clean_never_touches_user_data(stratarc_home: Path) -> None:
    root = layout.ensure_layout()
    (root / "providers" / "p.json").write_text("{}")
    (root / "adapters" / "a").mkdir()
    (root / "adapters" / "a" / "manifest.json").write_text("{}")
    (root / "cache" / "scratch").write_text("x")
    make_backups(25)
    result = layout.clean(yes=True, now=NOW)
    protected = {root / "config.toml", root / "sources.toml", root / "providers" / "p.json", root / "adapters" / "a" / "manifest.json"}
    assert all(p.exists() for p in protected)
    assert not (root / "cache" / "scratch").exists()
    assert not any(r.parts[len(root.parts)] in layout.USER_DATA for r in result.plan.removals)


def test_unparseable_backup_names_are_kept(stratarc_home: Path) -> None:
    layout.ensure_layout()
    odd = layout.backups_dir() / "state__x.json" / "notes.txt"
    odd.parent.mkdir(parents=True)
    odd.write_text("keep")
    layout.clean(yes=True, now=NOW)
    assert odd.exists()


def test_emit_json_envelope_for_error(capsys: pytest.CaptureFixture[str]) -> None:
    error = layout.Unavailable("nope", hint="try later", param="x", code="down")
    assert layout.emit(True, error=error) == 5
    body = json.loads(capsys.readouterr().out)
    assert body == {"ok": False, "data": None, "error": {"code": "down", "message": "nope", "param": "x", "hint": "try later"}}
