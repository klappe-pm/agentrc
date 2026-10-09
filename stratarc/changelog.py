"""The change log: a human readable file always, and an optional SQLite database with a JSONL mirror.

The files live under the stratarc state home, `<home>/.stratarc/` (see `state_home`), in `logs/`:

    stratarc.log      one plain line per event, always written
    changes.db        SQLite, written only while the log is enabled
    stratarc.jsonl    the same events in a fixed envelope, written only while the log is enabled

The log is off until `logging = true` is set in the state home's `config.toml`, `STRATARC_LOG=1` is in the environment, or a caller passes `enabled=True`. The environment variable wins over the file. Every text value passes the packaged token-shaped detector before it is written, and a change is stored as digests, never content.

A locked or unusable database never loses an event and never fails the command that recorded it: the event is kept in the human log (and the JSONL mirror), one message says so on standard error, and the call returns normally.

    from stratarc import changelog
    change_id = changelog.record({"actor_kind": "human", "command": "sync apply", "file": "AGENTS.md", "layer": "base", "status": "written", "projects": ["notes-cli"]})
    changelog.update_status(change_id, "verified")
    print(changelog.explain(change_id))
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from importlib.resources import as_file
from pathlib import Path

from stratarc import paths
from stratarc.resources import data_dir

SCHEMA_VERSION = 1
HOME_DIRECTORY = ".stratarc"
LOG_VARIABLE = "STRATARC_LOG"
ACTOR_KINDS = ("human", "agent", "hook", "ci")
STATUSES = ("written", "propagated", "partial", "failed", "verified", "drift")
LAYERS = ("base", "runtime", "account", "project", "agent", "environment")

# How a locked database is handled: wait BUSY_TIMEOUT seconds per attempt, make RETRIES attempts, pause RETRY_DELAY between them.
BUSY_TIMEOUT = 1.0
RETRIES = 3
RETRY_DELAY = 0.1

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_SEPARATOR = "\n@@stratarc-field-separator@@\n"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  date TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  command TEXT NOT NULL DEFAULT '',
  file TEXT NOT NULL DEFAULT '',
  layer TEXT NOT NULL DEFAULT '',
  key TEXT NOT NULL DEFAULT '',
  before_digest TEXT NOT NULL DEFAULT '',
  after_digest TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  source_ref TEXT NOT NULL DEFAULT '',
  cause_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS targets (
  change_id TEXT NOT NULL REFERENCES changes(id) ON DELETE CASCADE,
  runtime TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS projects (
  change_id TEXT NOT NULL REFERENCES changes(id) ON DELETE CASCADE,
  project TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS changes_date ON changes(date);
CREATE INDEX IF NOT EXISTS changes_status ON changes(status);
CREATE INDEX IF NOT EXISTS changes_cause ON changes(cause_id);
CREATE INDEX IF NOT EXISTS targets_change ON targets(change_id);
CREATE INDEX IF NOT EXISTS projects_change ON projects(change_id);
"""

_TEXT_FIELDS = ("actor", "command", "file", "layer", "key", "source_ref")


class ChangeLogError(Exception):
    """A request the change log cannot honor (an unknown status, an unknown id)."""


class DatabaseUnavailable(Exception):
    """The database cannot be used right now; the event is kept in the files."""


class DatabaseLocked(DatabaseUnavailable):
    """Another process holds the database after every retry."""


def notify(message: str) -> None:
    """Tell the operator once, on standard error. Replaceable so a caller can route it."""
    print(f"stratarc: {message}", file=sys.stderr)


# ----- locations -----


def state_home() -> Path:
    """The stratarc state home: `.stratarc` under the engine home (`STRATARC_HOME`, else `HOME`)."""
    return paths.home() / HOME_DIRECTORY


def logs_dir() -> Path:
    return state_home() / "logs"


def human_log_path() -> Path:
    return logs_dir() / "stratarc.log"


def database_path() -> Path:
    return logs_dir() / "changes.db"


def jsonl_path() -> Path:
    return logs_dir() / "stratarc.jsonl"


def config_path() -> Path:
    return state_home() / "config.toml"


def ensure_directory(path: Path) -> Path:
    """Create path and its missing parents under the state home with mode 0700."""
    home = state_home()
    chain = [path, *path.parents]
    for directory in reversed(chain):
        if directory != home and home not in directory.parents:
            continue
        if not directory.exists():
            directory.mkdir(mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            directory.chmod(0o700)
    if not home.exists():
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            home.chmod(0o700)
    return path


def ensure_logs() -> Path:
    return ensure_directory(logs_dir())


# ----- the switch -----


def _config_flag() -> bool | None:
    try:
        data = tomllib.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    value = data.get("logging")
    return value if isinstance(value, bool) else None


def is_enabled(explicit: bool | None = None) -> bool:
    """Whether the database log is on: the argument, then `STRATARC_LOG`, then `logging` in config.toml, else off."""
    if explicit is not None:
        return explicit
    value = os.environ.get(LOG_VARIABLE, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return bool(_config_flag())


def set_enabled(on: bool) -> Path:
    """Write `logging = true|false` to config.toml, keeping every other line. Returns the file."""
    ensure_directory(state_home())
    path = config_path()
    line = f"logging = {'true' if on else 'false'}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for index, existing in enumerate(lines):
        if re.match(r"\s*logging\s*=", existing):
            lines[index] = line
            break
    else:
        lines.append(line)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


# ----- redaction -----


def redact_many(values: list[str]) -> list[str]:
    """Each value with every token-shaped substring replaced by `[REDACTED:<kind>]`. Fails closed."""
    values = [str(v) for v in values]
    if not any(values):
        return values
    blob = _SEPARATOR.join(values)
    try:
        with as_file(data_dir("hooks/lib/secret-scan.sh")) as detector:
            result = subprocess.run(
                ["bash", str(detector), "redact-labeled-stdin"],
                input=blob,
                capture_output=True,
                text=True,
                timeout=15,
            )
    except (OSError, subprocess.SubprocessError):
        return ["[REDACTED:detector-unavailable]" if v else "" for v in values]
    if result.returncode != 0:
        return ["[REDACTED:detector-unavailable]" if v else "" for v in values]
    parts = result.stdout.rstrip("\n").split(_SEPARATOR.strip("\n"))
    parts = [p.strip("\n") for p in parts]
    if len(parts) != len(values):
        return [redact_many([v])[0] if v else "" for v in values] if len(values) > 1 else ["[REDACTED:detector-unavailable]"]
    return parts


def redact(text: str) -> str:
    return redact_many([text])[0]


# ----- events -----


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_id(ts: str) -> str:
    stamp = re.sub(r"[^0-9]", "", ts)[:14]
    return f"chg-{stamp}-{secrets.token_hex(4)}"


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value if str(item)]


def _target_pairs(value: object) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in value or []:
        if isinstance(item, Mapping):
            pairs.append((str(item.get("runtime", "")), str(item.get("path", ""))))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
        else:
            pairs.append((str(item), ""))
    return pairs


def normalize(event: Mapping[str, object]) -> dict:
    """The complete, validated, redacted event dict that is stored. Does not write anything."""
    kind = str(event.get("actor_kind") or "human")
    if kind not in ACTOR_KINDS:
        raise ChangeLogError(f"actor_kind must be one of {', '.join(ACTOR_KINDS)}, not {kind}")
    status = str(event.get("status") or "written")
    if status not in STATUSES:
        raise ChangeLogError(f"status must be one of {', '.join(STATUSES)}, not {status}")
    ts = str(event.get("ts") or _now())
    date = str(event.get("date") or ts[:10])
    projects = _strings(event.get("projects"))
    targets = _target_pairs(event.get("targets"))
    flat = [str(event.get(name) or "") for name in _TEXT_FIELDS]
    flat += projects
    flat += [item for pair in targets for item in pair]
    clean = redact_many(flat)
    out: dict = {"id": str(event.get("id") or _new_id(ts)), "ts": ts, "date": date, "actor_kind": kind}
    for name, value in zip(_TEXT_FIELDS, clean):
        out[name] = value
    cursor = len(_TEXT_FIELDS)
    out["before_digest"] = str(event.get("before_digest") or "")
    out["after_digest"] = str(event.get("after_digest") or "")
    out["status"] = status
    out["cause_id"] = str(event.get("cause_id") or "")
    out["projects"] = clean[cursor : cursor + len(projects)]
    rest = clean[cursor + len(projects) :]
    out["targets"] = [{"runtime": rest[i], "path": rest[i + 1]} for i in range(0, len(rest), 2)]
    return out


def envelope(change: Mapping[str, object]) -> dict:
    """The fixed JSONL envelope for a change: the same keys in the same order, always."""
    return {
        "schema": SCHEMA_VERSION,
        "event": "change",
        "id": change["id"],
        "ts": change["ts"],
        "date": change["date"],
        "actor": {"kind": change["actor_kind"], "name": change["actor"]},
        "command": change["command"],
        "target": {"file": change["file"], "layer": change["layer"], "key": change["key"]},
        "digests": {"before": change["before_digest"], "after": change["after_digest"]},
        "status": change["status"],
        "projects": list(change["projects"]),
        "targets": list(change["targets"]),
        "source_ref": change["source_ref"],
        "cause_id": change["cause_id"],
    }


def _from_envelope(body: Mapping[str, object]) -> dict:
    actor = body.get("actor") or {}
    target = body.get("target") or {}
    digests = body.get("digests") or {}
    return {
        "id": body["id"],
        "ts": body["ts"],
        "date": body["date"],
        "actor_kind": actor.get("kind", ""),
        "actor": actor.get("name", ""),
        "command": body.get("command", ""),
        "file": target.get("file", ""),
        "layer": target.get("layer", ""),
        "key": target.get("key", ""),
        "before_digest": digests.get("before", ""),
        "after_digest": digests.get("after", ""),
        "status": body.get("status", ""),
        "source_ref": body.get("source_ref", ""),
        "cause_id": body.get("cause_id", ""),
        "projects": list(body.get("projects") or []),
        "targets": list(body.get("targets") or []),
    }


# ----- files -----


def _append(path: Path, text: str) -> None:
    ensure_logs()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _human_line(change: Mapping[str, object]) -> str:
    parts = [str(change["ts"]), str(change["id"]), f"{change['actor_kind']}:{change['actor'] or '-'}", str(change["status"])]
    if change["file"]:
        parts.append(f"file={change['file']}")
    if change["layer"]:
        parts.append(f"layer={change['layer']}")
    if change["key"]:
        parts.append(f"key={change['key']}")
    if change["projects"]:
        parts.append("projects=" + ",".join(change["projects"]))
    if change["cause_id"]:
        parts.append(f"cause={change['cause_id']}")
    line = " ".join(parts)
    if change["command"]:
        line += " :: " + " ".join(str(change["command"]).split())
    return line + "\n"


def _write_files(change: Mapping[str, object], enabled: bool) -> None:
    _append(human_log_path(), _human_line(change))
    if enabled:
        _append(jsonl_path(), json.dumps(envelope(change), separators=(",", ":")) + "\n")


# ----- database -----


def _connect() -> sqlite3.Connection:
    ensure_logs()
    path = database_path()
    existed = path.exists()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DatabaseUnavailable(f"{path} is schema version {version}, newer than this tool understands ({SCHEMA_VERSION})")
        connection.executescript(_SCHEMA)
        if version == 0:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except (sqlite3.Error, DatabaseUnavailable) as error:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
        if isinstance(error, DatabaseUnavailable):
            raise
        if isinstance(error, sqlite3.OperationalError) and _is_busy(error):
            raise DatabaseLocked(str(error)) from error
        raise DatabaseUnavailable(str(error)) from error
    if not existed:
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    return connection


def _is_busy(error: sqlite3.Error) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _with_database(work: Callable[[sqlite3.Connection], object]) -> object:
    """Run work in a transaction, retrying a locked database RETRIES times, then raise DatabaseLocked."""
    last: Exception | None = None
    for attempt in range(max(1, RETRIES)):
        if attempt:
            time.sleep(RETRY_DELAY)
        connection = None
        try:
            connection = _connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = work(connection)
                connection.execute("COMMIT")
                return result
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        except DatabaseLocked as error:
            last = error
        except sqlite3.OperationalError as error:
            if not _is_busy(error):
                raise DatabaseUnavailable(str(error)) from error
            last = DatabaseLocked(str(error))
        except sqlite3.DatabaseError as error:
            raise DatabaseUnavailable(str(error)) from error
        finally:
            if connection is not None:
                with contextlib.suppress(sqlite3.Error):
                    connection.close()
    raise DatabaseLocked(str(last))


def _insert(connection: sqlite3.Connection, change: Mapping[str, object]) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO changes (id, ts, date, actor_kind, actor, command, file, layer, key, before_digest, after_digest, status, source_ref, cause_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(
            change[name]
            for name in ("id", "ts", "date", "actor_kind", "actor", "command", "file", "layer", "key", "before_digest", "after_digest", "status", "source_ref", "cause_id")
        ),
    )
    connection.execute("DELETE FROM targets WHERE change_id = ?", (change["id"],))
    connection.execute("DELETE FROM projects WHERE change_id = ?", (change["id"],))
    connection.executemany("INSERT INTO targets (change_id, runtime, path) VALUES (?, ?, ?)", [(change["id"], t["runtime"], t["path"]) for t in change["targets"]])
    connection.executemany("INSERT INTO projects (change_id, project) VALUES (?, ?)", [(change["id"], p) for p in change["projects"]])


def _degraded(error: Exception, change_ids: Iterable[str]) -> None:
    ids = list(change_ids)
    reason = "is locked" if isinstance(error, DatabaseLocked) else "cannot be used"
    notify(
        f"the change log database {reason} ({error}); {len(ids)} event(s) were kept in {human_log_path()} only. "
        "Close the other process using the database, or run `stratarc log enable` again after it is free."
    )
    with contextlib.suppress(OSError):
        _append(human_log_path(), f"{_now()} NOTE database {reason}; events {', '.join(ids[:5])}{' ...' if len(ids) > 5 else ''} are in this log and the jsonl only\n")


# ----- public api -----


def record_many(events: Iterable[Mapping[str, object]], *, enabled: bool | None = None) -> list[str]:
    """Record events in one pass. Returns their ids. Never raises for a locked or broken database."""
    changes = [normalize(event) for event in events]
    if not changes:
        return []
    on = is_enabled(enabled)
    for change in changes:
        _write_files(change, on)
    if on:
        try:
            _with_database(lambda connection: [_insert(connection, change) for change in changes])
        except DatabaseUnavailable as error:
            _degraded(error, (c["id"] for c in changes))
    return [change["id"] for change in changes]


def record(event: Mapping[str, object], *, enabled: bool | None = None) -> str:
    """Record one change and return its id. The human log always gets the line; the database and JSONL only while enabled."""
    return record_many([event], enabled=enabled)[0]


def update_status(change_id: str, status: str, *, enabled: bool | None = None) -> bool:
    """Set a change's status (for example `verified` or `drift`). Returns whether a stored change was updated."""
    if status not in STATUSES:
        raise ChangeLogError(f"status must be one of {', '.join(STATUSES)}, not {status}")
    on = is_enabled(enabled)
    ts = _now()
    _append(human_log_path(), f"{ts} {change_id} status {status}\n")
    updated = False
    if on:
        _append(jsonl_path(), json.dumps({"schema": SCHEMA_VERSION, "event": "status", "id": change_id, "ts": ts, "status": status}, separators=(",", ":")) + "\n")
        try:
            updated = bool(_with_database(lambda c: c.execute("UPDATE changes SET status = ? WHERE id = ?", (status, change_id)).rowcount))
        except DatabaseUnavailable as error:
            _degraded(error, [change_id])
    return updated


def _jsonl_changes() -> list[dict]:
    path = jsonl_path()
    changes: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if not isinstance(body, dict) or "id" not in body:
            continue
        if body.get("event") == "status":
            if body["id"] in changes:
                changes[body["id"]]["status"] = body.get("status", "")
        elif body.get("event") == "change":
            changes[body["id"]] = _from_envelope(body)
    return list(changes.values())


def _row_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    change = dict(row)
    change["projects"] = [r[0] for r in connection.execute("SELECT project FROM projects WHERE change_id = ? ORDER BY rowid", (change["id"],))]
    change["targets"] = [{"runtime": r[0], "path": r[1]} for r in connection.execute("SELECT runtime, path FROM targets WHERE change_id = ? ORDER BY rowid", (change["id"],))]
    return change


_COLUMN_FILTERS = ("id", "actor_kind", "actor", "layer", "key", "status", "cause_id", "source_ref")


def query(filters: Mapping[str, object] | None = None, *, limit: int | None = 100) -> list[dict]:
    """Changes newest first. Filters: any of id, actor_kind, actor, layer, key, status, cause_id, source_ref (exact), file and command (substring), project, since and until (a date or timestamp)."""
    filters = dict(filters or {})
    if database_path().exists():
        try:
            return _query_database(filters, limit)  # type: ignore[return-value]
        except DatabaseUnavailable as error:
            notify(f"the change log database could not be read ({error}); reading {jsonl_path()} instead.")
    return _query_memory(filters, limit)


def _query_database(filters: dict, limit: int | None) -> list[dict]:
    where: list[str] = []
    params: list[object] = []
    for name in _COLUMN_FILTERS:
        if filters.get(name):
            where.append(f"changes.{name} = ?")
            params.append(filters[name])
    for name in ("file", "command"):
        if filters.get(name):
            where.append(f"instr(changes.{name}, ?) > 0")
            params.append(filters[name])
    if filters.get("project"):
        where.append("changes.id IN (SELECT change_id FROM projects WHERE project = ?)")
        params.append(filters["project"])
    if filters.get("since"):
        where.append("changes.ts >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        where.append("changes.ts <= ?")
        params.append(str(filters["until"]) + ("T23:59:59Z" if len(str(filters["until"])) == 10 else ""))
    sql = "SELECT * FROM changes" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC, rowid DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    def work(connection: sqlite3.Connection) -> list[dict]:
        connection.row_factory = sqlite3.Row
        return [_row_dict(connection, row) for row in connection.execute(sql, params)]

    return _with_database(work)  # type: ignore[return-value]


def _query_memory(filters: dict, limit: int | None) -> list[dict]:
    rows = _jsonl_changes()

    def keep(change: dict) -> bool:
        for name in _COLUMN_FILTERS:
            if filters.get(name) and change.get(name) != filters[name]:
                return False
        for name in ("file", "command"):
            if filters.get(name) and str(filters[name]) not in change.get(name, ""):
                return False
        if filters.get("project") and filters["project"] not in change["projects"]:
            return False
        if filters.get("since") and change["ts"] < str(filters["since"]):
            return False
        if filters.get("until"):
            until = str(filters["until"]) + ("T23:59:59Z" if len(str(filters["until"])) == 10 else "")
            if change["ts"] > until:
                return False
        return True

    rows = [c for c in rows if keep(c)]
    rows.sort(key=lambda c: c["ts"], reverse=True)
    return rows[: int(limit)] if limit else rows


def get(change_id: str) -> dict | None:
    found = query({"id": change_id}, limit=1)
    return found[0] if found else None


def effects(change_id: str) -> list[dict]:
    """The changes this one caused (their `cause_id` is its id), oldest first."""
    return sorted(query({"cause_id": change_id}, limit=None), key=lambda c: c["ts"])


def explain_data(change_id: str) -> dict:
    change = get(change_id)
    if change is None:
        raise ChangeLogError(f"no change {change_id} in the log")
    return {"change": change, "caused": [c["id"] for c in effects(change_id)], "story": _story(change, effects(change_id))}


def explain(change_id: str) -> str:
    """A change as a short story: who asked, what layer, which projects and runtimes, and whether it was verified."""
    return explain_data(change_id)["story"]


_VERIFIED_TEXT = {
    "verified": "A verify run confirmed that the deployed files match.",
    "drift": "A verify run found the deployed files differ from what the source renders.",
    "failed": "The change failed and was not verified.",
    "partial": "The change landed in part and was not verified.",
}


def _story(change: Mapping[str, object], caused: list[dict]) -> str:
    who = {"human": "a person", "agent": "an agent", "hook": "a hook", "ci": "a CI job"}.get(str(change["actor_kind"]), str(change["actor_kind"]))
    if change["actor"]:
        who += f" ({change['actor']})"
    what = f"`{change['command']}`" if change["command"] else "a change"
    sentences = [f"{change['id']} on {change['ts']}: {who} ran {what}."]
    touched = []
    if change["file"]:
        touched.append(f"the file {change['file']}")
    if change["key"]:
        touched.append(f"the key {change['key']}")
    layer = f"the {change['layer']} layer" if change["layer"] else "an unrecorded layer"
    sentences.append(f"It touched {layer}" + (f", {' and '.join(touched)}." if touched else "."))
    if change["projects"]:
        sentences.append("It reached the projects " + ", ".join(change["projects"]) + ".")
    else:
        sentences.append("It reached no project.")
    runtimes = sorted({t["runtime"] for t in change["targets"] if t["runtime"]})
    if runtimes:
        sentences.append("It reached the runtimes " + ", ".join(runtimes) + ".")
    if change["cause_id"]:
        sentences.append(f"It was caused by {change['cause_id']}.")
    if caused:
        sentences.append(f"It caused {len(caused)} further change(s): " + ", ".join(c["id"] for c in caused[:5]) + (" ..." if len(caused) > 5 else "") + ".")
    sentences.append(_VERIFIED_TEXT.get(str(change["status"]), f"Its status is {change['status']}; it has not been verified."))
    return " ".join(sentences)


def tail(count: int = 20) -> list[str]:
    """The last count lines of the human log."""
    try:
        lines = human_log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-count:] if count > 0 else lines


def export_text(fmt: str, filters: Mapping[str, object] | None = None, limit: int | None = None) -> str:
    """The matching changes (oldest first) as `json`, `jsonl` or `csv` text."""
    changes = list(reversed(query(filters, limit=limit)))
    if fmt == "json":
        return json.dumps([envelope(c) for c in changes], indent=2)
    if fmt == "jsonl":
        return "".join(json.dumps(envelope(c), separators=(",", ":")) + "\n" for c in changes)
    if fmt == "csv":
        import csv
        import io

        buffer = io.StringIO()
        columns = ("id", "ts", "date", "actor_kind", "actor", "command", "file", "layer", "key", "before_digest", "after_digest", "status", "source_ref", "cause_id")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow([*columns, "projects"])
        for c in changes:
            writer.writerow([c[name] for name in columns] + [";".join(c["projects"])])
        return buffer.getvalue()
    raise ChangeLogError(f"export format must be json, jsonl or csv, not {fmt}")


def prune(before: str, *, dry_run: bool = False) -> int:
    """Remove stored changes dated before `before` (YYYY-MM-DD) from the database and the JSONL mirror. The human log is never trimmed. Returns the count."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", before):
        raise ChangeLogError("the cutoff must be a date as YYYY-MM-DD")
    count = 0
    if database_path().exists():
        try:
            def work(connection: sqlite3.Connection) -> int:
                found = connection.execute("SELECT COUNT(*) FROM changes WHERE date < ?", (before,)).fetchone()[0]
                if not dry_run:
                    connection.execute("DELETE FROM changes WHERE date < ?", (before,))
                return found

            count = int(_with_database(work))  # type: ignore[arg-type]
        except DatabaseUnavailable as error:
            _degraded(error, [])
    path = jsonl_path()
    if path.exists():
        kept, dropped = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                body = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if isinstance(body, dict) and body.get("event") == "change" and str(body.get("date", "")) < before:
                dropped += 1
            else:
                kept.append(line)
        if not database_path().exists():
            count = dropped
        if dropped and not dry_run:
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
            temporary.replace(path)
    if not dry_run and count:
        _append(human_log_path(), f"{_now()} NOTE pruned {count} change(s) dated before {before}\n")
    return count
