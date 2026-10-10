"""The `.stratarc` directory under the home: layout, versioned files, backups and cleaning.

The layout root is `<paths.home()>/.stratarc`. It holds the machine, not the project:

```text
config.toml  sources.toml  providers/  adapters/  state/  backups/  logs/  cache/
```

Rules this module enforces (see the CLI design, the-home-directory):

- Directories in the home are created mode 0700 and files 0600. A file `safe_write` puts in a source root (`private=False`) keeps the mode of the file it replaces, and a new one is 0644.
- Every file carries `schema_version`. A file whose version is newer than `SCHEMA_VERSION` is never rewritten; `safe_write` raises `NewerSchemaError`, which maps to the unavailable exit code.
- `safe_write` copies the file it replaces into `backups/` first and replaces it atomically.
- A clean removes only old backups and the disposable cache. `config.toml`, `sources.toml`, `providers/` and `adapters/` are never candidates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from stratarc.paths import home

SCHEMA_VERSION = 1
LAYOUT_NAME = ".stratarc"
DIR_MODE = 0o700
FILE_MODE = 0o600

DIRECTORIES = ("providers", "adapters", "state", "backups", "logs", "cache")
STAMPED_FILES = ("config.toml", "sources.toml")
USER_DATA = ("config.toml", "sources.toml", "providers", "adapters")

BACKUP_KEEP_VERSIONS = 20
BACKUP_KEEP_DAYS = 30

OK = 0
FAILURE = 1
INVALID_INPUT = 2
DENIED = 3
CONFLICT = 4
UNAVAILABLE = 5


class ToolError(Exception):
    """A failure a command reports with a stable code, an exit status and a recovery sentence."""

    exit = FAILURE
    code = "failure"

    def __init__(self, message: str, *, hint: str | None = None, param: str | None = None, code: str | None = None, exit: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.param = param
        if code is not None:
            self.code = code
        if exit is not None:
            self.exit = exit


class InvalidInput(ToolError):
    exit = INVALID_INPUT
    code = "invalid-input"


class Conflict(ToolError):
    exit = CONFLICT
    code = "conflict"


class Unavailable(ToolError):
    exit = UNAVAILABLE
    code = "unavailable"


class HomeError(ToolError):
    """The home cannot be used."""

    exit = DENIED
    code = "home-unwritable"


class NewerSchemaError(Unavailable):
    """A file in the home was written by a newer tool. It is left untouched."""

    code = "newer-schema"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def layout_root() -> Path:
    """`<home>/.stratarc`, read from the environment on every call."""
    return home() / LAYOUT_NAME


def providers_dir() -> Path:
    return layout_root() / "providers"


def adapters_dir() -> Path:
    return layout_root() / "adapters"


def state_dir() -> Path:
    return layout_root() / "state"


def backups_dir() -> Path:
    return layout_root() / "backups"


def cache_dir() -> Path:
    return layout_root() / "cache"


def _mkdir(path: Path) -> None:
    try:
        root = layout_root()
        created = [p for p in (path, *path.parents) if not p.exists() and (p == root or root in p.parents)]
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(path, DIR_MODE)
        for parent in created:
            os.chmod(parent, DIR_MODE)
    except OSError as error:
        raise HomeError(f"The home directory {path} cannot be created or written to: {error.strerror or error}.", hint="Make it writable, or point at another one with STRATARC_HOME.", param="home") from None


# ---------------------------------------------------------------------------
# versioned files
# ---------------------------------------------------------------------------

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as a TOML value")


def dump_toml(data: Mapping[str, Any]) -> str:
    """A small TOML writer for scalars, string lists and nested tables, which is all the home files hold."""
    lines: list[str] = []

    def table(prefix: tuple[str, ...], values: Mapping[str, Any]) -> None:
        scalars = {k: v for k, v in values.items() if not isinstance(v, Mapping)}
        tables = {k: v for k, v in values.items() if isinstance(v, Mapping)}
        if prefix and (scalars or not tables):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_toml_key(p) for p in prefix) + "]")
        for key, value in scalars.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in tables.items():
            table((*prefix, key), value)

    table((), data)
    return "\n".join(lines) + "\n"


def _declared_version(path: Path, raw: bytes) -> int | None:
    """The `schema_version` a file declares, or None when it declares none or cannot be parsed."""
    try:
        if path.suffix == ".toml":
            value = tomllib.loads(raw.decode("utf-8")).get("schema_version")
        elif path.suffix == ".json":
            document = json.loads(raw.decode("utf-8"))
            value = document.get("schema_version") if isinstance(document, dict) else None
        else:
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def check_not_newer(path: Path) -> None:
    """Raise `NewerSchemaError` when the file exists and declares a schema newer than this tool knows."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError:
        return
    version = _declared_version(path, raw)
    if version is not None and version > SCHEMA_VERSION:
        raise NewerSchemaError(
            f"{path} declares schema version {version}, newer than the {SCHEMA_VERSION} this stratarc understands.",
            hint="Upgrade stratarc. The file was left unchanged.",
            param=str(path),
        )


def read_toml(path: Path) -> dict[str, Any]:
    """Read a home TOML file; a missing file is an empty mapping and a newer schema raises."""
    check_not_newer(path)
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise InvalidInput(f"{path} cannot be read: {error}", hint="Fix or remove the file.", param=str(path)) from None


def write_toml(path: Path, data: Mapping[str, Any]) -> Path | None:
    """Write a stamped TOML file through `safe_write`; returns the backup it made, if any."""
    stamped = {"schema_version": SCHEMA_VERSION, **{k: v for k, v in data.items() if k != "schema_version"}}
    return safe_write(path, dump_toml(stamped))


def ensure_layout() -> Path:
    """Create the home layout (idempotent) and stamp `config.toml` and `sources.toml` when absent."""
    root = layout_root()
    _mkdir(root)
    for name in DIRECTORIES:
        _mkdir(root / name)
    for name in STAMPED_FILES:
        path = root / name
        if not path.exists():
            write_toml(path, {})
    return root


# ---------------------------------------------------------------------------
# backup-before-write
# ---------------------------------------------------------------------------

_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_STAMP_PATTERN = re.compile(r"^(\d{8}T\d{12}Z)(?:-\d+)?$")


def backup_key(path: Path) -> str:
    """The directory name under `backups/` that holds every version of one file."""
    root = layout_root()
    try:
        relative = path.resolve().relative_to(root.resolve())
        return "__".join(relative.parts)
    except ValueError:
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
        return f"external-{digest}-{path.name}"


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _backup(path: Path, now: datetime | None) -> Path | None:
    if not path.is_file():
        return None
    directory = backups_dir() / backup_key(path)
    _mkdir(directory)
    stamp = _now(now).strftime(_STAMP_FORMAT)
    target = directory / stamp
    counter = 1
    while target.exists():
        target = directory / f"{stamp}-{counter}"
        counter += 1
    target.write_bytes(path.read_bytes())
    os.chmod(target, FILE_MODE)
    return target


def _public_mode(path: Path) -> int:
    """The mode a source-root file gets: the mode of the file it replaces, or 0644 subject to the umask for a new one."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mask = os.umask(0)
        os.umask(mask)
        return 0o644 & ~mask


def safe_write(path: Path, data: str | bytes, *, now: datetime | None = None, private: bool = True) -> Path | None:
    """Replace `path` with `data`, keeping a copy of what was there.

    A file that declares a newer schema than this tool understands is refused before anything is touched. The new content is written to a temporary file in the same directory and moved into place, so a reader never sees a partial file. Returns the backup path, or None when the file did not exist.

    With `private` (the default) the file is mode 0600 and a missing parent is made 0700, as everything in the home is. With `private=False`, for a file in a source root, the file keeps the mode of the file it replaces, a new file is 0644 and a missing parent directory 0755 (both subject to the umask), and an existing directory is left alone. The backup copy is always 0600.
    """
    path = Path(path)
    check_not_newer(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    if private:
        _mkdir(path.parent)
        file_mode = FILE_MODE
    else:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise HomeError(f"{path.parent} cannot be created or written to: {error.strerror or error}.", hint="Make it writable.", param=str(path.parent)) from None
        file_mode = _public_mode(path)
    backup = _backup(path, now)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.chmod(temporary, file_mode)
        os.replace(temporary, path)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise HomeError(f"{path} cannot be written: {error.strerror or error}.", hint="Make the directory writable.", param=str(path)) from None
    return backup


# ---------------------------------------------------------------------------
# cleaning
# ---------------------------------------------------------------------------


@dataclass
class CleanPlan:
    """What a clean would remove, and what it keeps."""

    removals: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)


@dataclass
class CleanResult:
    plan: CleanPlan
    removed: list[Path]
    applied: bool


def _backup_time(name: str) -> datetime | None:
    match = _STAMP_PATTERN.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def plan_clean(*, now: datetime | None = None, include_cache: bool = True) -> CleanPlan:
    """The backups and cache entries a clean would remove.

    Per file, the newest 20 versions are kept, and so is any version newer than 30 days: a version is removed only when it is both beyond the 20th and 30 days old or more. Unparseable names under `backups/` are kept. User data is never listed.
    """
    plan = CleanPlan()
    cutoff = _now(now) - timedelta(days=BACKUP_KEEP_DAYS)
    root = backups_dir()
    if root.is_dir():
        for directory in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
            versions: list[tuple[datetime, str, Path]] = []
            for entry in directory.iterdir():
                stamp = _backup_time(entry.name)
                if stamp is None or not entry.is_file() or entry.is_symlink():
                    plan.kept.append(entry)
                else:
                    versions.append((stamp, entry.name, entry))
            versions.sort(reverse=True)
            for index, (stamp, _name, entry) in enumerate(versions):
                if index < BACKUP_KEEP_VERSIONS or stamp > cutoff:
                    plan.kept.append(entry)
                else:
                    plan.removals.append(entry)
    cache = cache_dir()
    if include_cache and cache.is_dir():
        for entry in sorted(cache.iterdir()):
            plan.removals.append(entry)
    return plan


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.parent.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            _remove(child)
        path.rmdir()
    else:
        path.unlink()


def clean(yes: bool = False, *, now: datetime | None = None, include_cache: bool = True) -> CleanResult:
    """Return the plan; remove it only when `yes` is true."""
    plan = plan_clean(now=now, include_cache=include_cache)
    removed: list[Path] = []
    if not yes:
        return CleanResult(plan, removed, False)
    allowed = (backups_dir(), cache_dir())
    for path in plan.removals:
        if not any(_inside(path, parent) for parent in allowed):
            continue
        _remove(path)
        removed.append(path)
    for directory in sorted(backups_dir().glob("*")) if backups_dir().is_dir() else []:
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return CleanResult(plan, removed, True)


# ---------------------------------------------------------------------------
# command output shared by the provider and adapter commands
# ---------------------------------------------------------------------------


def emit(json_mode: bool, *, data: Any = None, text: str = "", error: ToolError | None = None, out: Callable[[str], None] | None = None, err: Callable[[str], None] | None = None) -> int:
    """Print one result and return the exit status: the JSON envelope with `--json`, otherwise text on stdout and the error on stderr."""
    import sys

    write_out = out or (lambda s: print(s))
    write_err = err or (lambda s: print(s, file=sys.stderr))
    if json_mode:
        body = None
        if error is not None:
            body = {"code": error.code, "message": error.message, "param": error.param, "hint": error.hint}
        write_out(json.dumps({"ok": error is None, "data": data if error is None else None, "error": body}, indent=2))
    else:
        if error is not None:
            write_err(f"error {error.code}  {error.message}")
            if error.hint:
                write_err(f"  {error.hint}")
        elif text:
            write_out(text)
    return error.exit if error is not None else OK
