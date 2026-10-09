"""Tests for the change log: the switch, the three files, redaction, locked-database degradation, queries and the story."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from stratarc import changelog


@pytest.fixture(autouse=True)
def isolated(stratarc_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("STRATARC_LOG", raising=False)
    monkeypatch.setattr(changelog, "BUSY_TIMEOUT", 0.05)
    monkeypatch.setattr(changelog, "RETRY_DELAY", 0.01)
    return stratarc_home


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATARC_LOG", "1")


def token() -> str:
    """A token-shaped value built at run time so no literal one sits in the source."""
    return "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"


EVENT = {
    "actor_kind": "agent",
    "actor": "reviewer",
    "command": "stratarc sync apply",
    "file": "permissions.json",
    "layer": "project",
    "key": "permissions.network.allow",
    "before_digest": "sha256:aa",
    "after_digest": "sha256:bb",
    "status": "written",
    "projects": ["notes-cli", "other"],
    "targets": [{"runtime": "claude", "path": "/x/.claude/settings.json"}, {"runtime": "codex", "path": "/x/.codex/config.toml"}],
    "source_ref": "uncommitted",
}


class TestSwitch:
    def test_off_by_default_but_the_human_log_is_written(self, isolated: Path) -> None:
        change_id = changelog.record(EVENT)
        logs = isolated / ".stratarc" / "logs"
        assert change_id.startswith("chg-")
        assert change_id in (logs / "stratarc.log").read_text()
        assert not (logs / "changes.db").exists()
        assert not (logs / "stratarc.jsonl").exists()
        assert changelog.is_enabled() is False

    def test_environment_turns_it_on_and_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRATARC_LOG", "1")
        assert changelog.is_enabled() is True
        monkeypatch.setenv("STRATARC_LOG", "off")
        assert changelog.is_enabled() is False

    def test_explicit_argument_wins(self, monkeypatch: pytest.MonkeyPatch, isolated: Path) -> None:
        monkeypatch.setenv("STRATARC_LOG", "0")
        changelog.record(EVENT, enabled=True)
        assert (isolated / ".stratarc" / "logs" / "changes.db").is_file()

    def test_enable_and_disable_edit_config_toml_and_keep_other_lines(self, isolated: Path) -> None:
        config = isolated / ".stratarc" / "config.toml"
        changelog.ensure_directory(config.parent)
        config.write_text('active_source = "x"\nlogging = false\n')
        changelog.set_enabled(True)
        assert changelog.is_enabled() is True
        assert config.read_text() == 'active_source = "x"\nlogging = true\n'
        changelog.set_enabled(False)
        assert changelog.is_enabled() is False
        assert 'active_source = "x"' in config.read_text()

    def test_enable_creates_the_config_when_absent(self, isolated: Path) -> None:
        changelog.set_enabled(True)
        assert (isolated / ".stratarc" / "config.toml").read_text() == "logging = true\n"

    def test_environment_beats_the_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        changelog.set_enabled(True)
        monkeypatch.setenv("STRATARC_LOG", "0")
        assert changelog.is_enabled() is False


class TestFiles:
    def test_the_home_is_created_private(self, isolated: Path, on) -> None:
        changelog.record(EVENT)
        for directory in (isolated / ".stratarc", isolated / ".stratarc" / "logs"):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_database_rows_carry_the_designed_columns(self, isolated: Path, on) -> None:
        change_id = changelog.record(EVENT)
        connection = sqlite3.connect(isolated / ".stratarc" / "logs" / "changes.db")
        columns = [r[1] for r in connection.execute("PRAGMA table_info(changes)")]
        assert columns == ["id", "ts", "date", "actor_kind", "actor", "command", "file", "layer", "key", "before_digest", "after_digest", "status", "source_ref", "cause_id"]
        assert sorted(r[0] for r in connection.execute("SELECT project FROM projects WHERE change_id = ?", (change_id,))) == ["notes-cli", "other"]
        assert sorted(r[0] for r in connection.execute("SELECT runtime FROM targets WHERE change_id = ?", (change_id,))) == ["claude", "codex"]
        row = connection.execute("SELECT ts, date, actor_kind, status FROM changes WHERE id = ?", (change_id,)).fetchone()
        assert row[1] == row[0][:10] and row[2:] == ("agent", "written")
        assert connection.execute("PRAGMA user_version").fetchone()[0] == changelog.SCHEMA_VERSION

    def test_the_jsonl_envelope_is_fixed(self, isolated: Path, on) -> None:
        change_id = changelog.record(EVENT)
        changelog.record({"command": "x"})
        lines = (isolated / ".stratarc" / "logs" / "stratarc.jsonl").read_text().splitlines()
        first, second = json.loads(lines[0]), json.loads(lines[1])
        assert list(first) == ["schema", "event", "id", "ts", "date", "actor", "command", "target", "digests", "status", "projects", "targets", "source_ref", "cause_id"]
        assert list(second) == list(first)
        assert first["id"] == change_id and first["actor"] == {"kind": "agent", "name": "reviewer"}
        assert first["digests"] == {"before": "sha256:aa", "after": "sha256:bb"}
        assert first["target"] == {"file": "permissions.json", "layer": "project", "key": "permissions.network.allow"}

    def test_content_is_never_stored_only_digests(self, isolated: Path, on) -> None:
        changelog.record({**EVENT, "content": "the whole file body"})
        assert "the whole file body" not in (isolated / ".stratarc" / "logs" / "stratarc.jsonl").read_text()

    def test_bad_values_are_refused(self) -> None:
        with pytest.raises(changelog.ChangeLogError):
            changelog.record({"status": "great"})
        with pytest.raises(changelog.ChangeLogError):
            changelog.record({"actor_kind": "robot"})
        with pytest.raises(changelog.ChangeLogError):
            changelog.update_status("chg-x", "great")


class TestRedaction:
    def test_a_token_in_a_command_never_reaches_any_file(self, isolated: Path, on) -> None:
        secret = token()
        change_id = changelog.record({**EVENT, "command": f"stratarc provider add --key {secret} --name x", "key": f"TOKEN={secret}"})
        logs = isolated / ".stratarc" / "logs"
        for name in ("stratarc.log", "stratarc.jsonl"):
            text = (logs / name).read_text()
            assert secret not in text
            assert "[REDACTED:" in text
        raw = (logs / "changes.db").read_bytes()
        assert secret.encode() not in raw
        stored = changelog.get(change_id)
        assert "[REDACTED:github_token]" in stored["command"]
        assert "--name x" in stored["command"]
        assert secret not in changelog.explain(change_id)

    def test_other_fields_keep_their_alignment_when_one_is_redacted(self, on) -> None:
        change_id = changelog.record({**EVENT, "actor": "kevin", "command": f"run {token()}", "file": "a.json"})
        stored = changelog.get(change_id)
        assert (stored["actor"], stored["file"], stored["layer"]) == ("kevin", "a.json", "project")

    def test_the_detector_failing_closed(self, monkeypatch: pytest.MonkeyPatch, isolated: Path, on) -> None:
        import subprocess

        def boom(*a, **k):
            raise OSError("no bash")

        monkeypatch.setattr(subprocess, "run", boom)
        changelog.record({**EVENT, "command": "ordinary"})
        assert "ordinary" not in (isolated / ".stratarc" / "logs" / "stratarc.log").read_text()
        assert "detector-unavailable" in (isolated / ".stratarc" / "logs" / "stratarc.log").read_text()


class TestLockedDatabase:
    def hold_lock(self, isolated: Path) -> sqlite3.Connection:
        holder = sqlite3.connect(isolated / ".stratarc" / "logs" / "changes.db", isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        return holder

    def test_a_locked_database_degrades_to_the_human_log_and_never_raises(self, isolated: Path, on, capsys: pytest.CaptureFixture[str]) -> None:
        first = changelog.record(EVENT)
        holder = self.hold_lock(isolated)
        try:
            second = changelog.record({**EVENT, "command": "while locked"})
            assert changelog.update_status(first, "verified") is False
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        err = capsys.readouterr().err
        assert "is locked" in err and "stratarc.log" in err
        human = (isolated / ".stratarc" / "logs" / "stratarc.log").read_text()
        assert second in human and "while locked" in human and "NOTE database is locked" in human
        assert second in (isolated / ".stratarc" / "logs" / "stratarc.jsonl").read_text()
        assert changelog.get(second) is None
        assert changelog.get(first)["status"] == "written"

    def test_it_recovers_once_the_lock_is_gone(self, isolated: Path, on) -> None:
        changelog.record(EVENT)
        holder = self.hold_lock(isolated)
        holder.execute("ROLLBACK")
        holder.close()
        change_id = changelog.record(EVENT)
        assert changelog.get(change_id) is not None

    def test_a_short_lock_is_waited_out_by_the_retries(self, isolated: Path, on, monkeypatch: pytest.MonkeyPatch) -> None:
        changelog.record(EVENT)
        holder = self.hold_lock(isolated)
        real_sleep = changelog.time.sleep
        calls: list[float] = []

        def release_then_sleep(seconds: float) -> None:
            calls.append(seconds)
            if len(calls) == 1:
                holder.execute("ROLLBACK")
                holder.close()
            real_sleep(seconds)

        monkeypatch.setattr(changelog.time, "sleep", release_then_sleep)
        change_id = changelog.record(EVENT)
        assert calls and changelog.get(change_id) is not None

    def test_a_newer_schema_is_not_rewritten(self, isolated: Path, on, capsys: pytest.CaptureFixture[str]) -> None:
        changelog.record(EVENT)
        db = isolated / ".stratarc" / "logs" / "changes.db"
        connection = sqlite3.connect(db)
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()
        change_id = changelog.record(EVENT)
        assert "newer than this tool understands" in capsys.readouterr().err
        assert change_id in (isolated / ".stratarc" / "logs" / "stratarc.log").read_text()
        reread = sqlite3.connect(db)
        assert reread.execute("PRAGMA user_version").fetchone()[0] == 99
        assert reread.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1

    def test_a_corrupt_database_degrades(self, isolated: Path, on, capsys: pytest.CaptureFixture[str]) -> None:
        changelog.record(EVENT)
        (isolated / ".stratarc" / "logs" / "changes.db").write_bytes(b"not a database" * 100)
        change_id = changelog.record(EVENT)
        assert "cannot be used" in capsys.readouterr().err
        assert change_id in (isolated / ".stratarc" / "logs" / "stratarc.log").read_text()


class TestQueries:
    @pytest.fixture
    def three(self, on) -> list[str]:
        a = changelog.record({**EVENT, "ts": "2026-10-01T10:00:00Z", "file": "a.json", "projects": ["notes-cli"]})
        b = changelog.record({**EVENT, "ts": "2026-10-05T10:00:00Z", "file": "b.json", "actor_kind": "human", "projects": ["other"], "status": "failed"})
        c = changelog.record({**EVENT, "ts": "2026-10-09T10:00:00Z", "file": "c.json", "cause_id": a, "projects": ["notes-cli"]})
        return [a, b, c]

    def test_newest_first_and_limit(self, three) -> None:
        assert [c["id"] for c in changelog.query()] == [three[2], three[1], three[0]]
        assert [c["id"] for c in changelog.query(limit=1)] == [three[2]]

    def test_filters(self, three) -> None:
        a, b, c = three
        ids = lambda **f: [x["id"] for x in changelog.query(f)]  # noqa: E731
        assert ids(project="notes-cli") == [c, a]
        assert ids(status="failed") == [b]
        assert ids(actor_kind="human") == [b]
        assert ids(file="b.") == [b]
        assert ids(since="2026-10-04T00:00:00Z", until="2026-10-05") == [b]
        assert ids(cause_id=a) == [c]
        assert ids(command="sync") == [c, b, a]
        assert ids(id=a) == [a]

    def test_update_status_changes_the_row_and_the_mirror(self, three, isolated: Path) -> None:
        assert changelog.update_status(three[0], "verified") is True
        assert changelog.get(three[0])["status"] == "verified"
        assert '"event":"status"' in (isolated / ".stratarc" / "logs" / "stratarc.jsonl").read_text()

    def test_queries_fall_back_to_the_jsonl_without_a_database(self, three, isolated: Path) -> None:
        changelog.update_status(three[0], "drift")
        (isolated / ".stratarc" / "logs" / "changes.db").unlink()
        assert [c["id"] for c in changelog.query({"project": "notes-cli"})] == [three[2], three[0]]
        assert changelog.get(three[0])["status"] == "drift"
        assert changelog.get(three[0])["targets"][0]["runtime"] == "claude"

    def test_explain_tells_who_what_where_and_whether_verified(self, three) -> None:
        a, _, c = three
        story = changelog.explain(a)
        assert "an agent (reviewer)" in story and "`stratarc sync apply`" in story
        assert "the project layer" in story and "permissions.network.allow" in story
        assert "notes-cli" in story and "claude, codex" in story
        assert "caused 1 further" in story and c in story
        assert "has not been verified" in changelog.explain(a)
        changelog.update_status(a, "verified")
        assert "confirmed that the deployed files match" in changelog.explain(a)
        assert f"caused by {a}" in changelog.explain(c)
        changelog.update_status(c, "drift")
        assert "differ" in changelog.explain(c)

    def test_explain_of_an_unknown_id_is_an_error(self, three) -> None:
        with pytest.raises(changelog.ChangeLogError):
            changelog.explain("chg-nothing")

    def test_export_formats(self, three) -> None:
        rows = [json.loads(line) for line in changelog.export_text("jsonl").splitlines()]
        assert [r["id"] for r in rows] == three
        assert [r["id"] for r in json.loads(changelog.export_text("json", {"project": "other"}))] == [three[1]]
        csv_lines = changelog.export_text("csv").splitlines()
        assert csv_lines[0].startswith("id,ts,date") and csv_lines[0].endswith(",projects")
        assert len(csv_lines) == 4
        with pytest.raises(changelog.ChangeLogError):
            changelog.export_text("xml")

    def test_prune_removes_old_rows_from_database_and_mirror(self, three, isolated: Path) -> None:
        assert changelog.prune("2026-10-06", dry_run=True) == 2
        assert len(changelog.query()) == 3
        assert changelog.prune("2026-10-06") == 2
        assert [c["id"] for c in changelog.query()] == [three[2]]
        mirror = (isolated / ".stratarc" / "logs" / "stratarc.jsonl").read_text()
        assert "a.json" not in mirror and "b.json" not in mirror and "c.json" in mirror
        assert three[0] in (isolated / ".stratarc" / "logs" / "stratarc.log").read_text()
        with pytest.raises(changelog.ChangeLogError):
            changelog.prune("yesterday")

    def test_tail_reads_the_human_log(self, three) -> None:
        assert changelog.tail(1)[0].startswith("2026-10-09T10:00:00Z " + three[2])
        assert len(changelog.tail(2)) == 2
