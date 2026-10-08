"""Reads `agentrc.toml` from a source root.

The file holds three things: a `[runtimes.<name>]` table per runtime with `enabled` and `target`, a `projects_root` directory and a GitHub `owner`. A missing file yields the defaults; a malformed one raises `ConfigError` naming the file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agentrc.paths import CONFIG_NAME, home


class ConfigError(Exception):
    """`agentrc.toml` exists but cannot be used."""


@dataclass(frozen=True)
class RuntimeConfig:
    enabled: bool = False
    target: Path | None = None


@dataclass(frozen=True)
class Config:
    runtimes: dict[str, RuntimeConfig] = field(default_factory=dict)
    projects_root: Path | None = None
    owner: str = ""


def _expand(value: str) -> Path:
    """Expand a leading `~` against the agentrc home, not the process's."""
    if value == "~":
        return home()
    if value.startswith("~/"):
        return home() / value[2:]
    return Path(value)


def _fail(path: Path, message: str) -> ConfigError:
    return ConfigError(f"{path}: {message}")


def _runtime(path: Path, name: str, table: object) -> RuntimeConfig:
    if not isinstance(table, dict):
        raise _fail(path, f"[runtimes.{name}] must be a table")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _fail(path, f"runtimes.{name}.enabled must be true or false")
    target = table.get("target")
    if target is not None and (not isinstance(target, str) or not target):
        raise _fail(path, f"runtimes.{name}.target must be a non-empty string")
    return RuntimeConfig(enabled=enabled, target=_expand(target) if target else None)


def load_config(root: Path) -> Config:
    """Read `<root>/agentrc.toml`; return the defaults when the file is absent."""
    path = Path(root) / CONFIG_NAME
    if not path.is_file():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise _fail(path, f"cannot read configuration: {exc}") from exc

    runtimes_table = data.get("runtimes", {})
    if not isinstance(runtimes_table, dict):
        raise _fail(path, "runtimes must be a table of tables")
    runtimes = {name: _runtime(path, name, table) for name, table in runtimes_table.items()}

    projects = data.get("projects_root")
    if projects is not None and (not isinstance(projects, str) or not projects):
        raise _fail(path, "projects_root must be a non-empty string")

    owner = data.get("owner", "")
    if not isinstance(owner, str):
        raise _fail(path, "owner must be a string")

    return Config(
        runtimes=runtimes,
        projects_root=_expand(projects) if projects else None,
        owner=owner.strip(),
    )
