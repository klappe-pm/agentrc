"""Where stratarc finds the home directory, the source root and the projects root.

This module and `stratarc.config` are the only places that read `STRATARC_HOME`, `STRATARC_SOURCE`, `LLM_ROOT_PROJECTS_DIR`, `STRATARC_NAME` and `STRATARC_GITHUB_OWNER`. Every function reads the environment when it is called, never at import, so a test or a command line flag can redirect them after the module is loaded.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_NAME = "stratarc.toml"
HOME_VARIABLE = "STRATARC_HOME"
SOURCE_VARIABLE = "STRATARC_SOURCE"
PROJECTS_VARIABLE = "LLM_ROOT_PROJECTS_DIR"
OWNER_VARIABLE = "STRATARC_GITHUB_OWNER"
NAME_VARIABLE = "STRATARC_NAME"
DEFAULT_NAME = "stratarc"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def home() -> Path:
    """The home directory every runtime target and `~` expansion hangs under.

    `STRATARC_HOME` wins when set and non-empty, then `HOME`, then the home directory the platform reports. The override lets a test or a clean-room run redirect every target without touching the real home.
    """
    for name in (HOME_VARIABLE, "HOME"):
        value = _env(name)
        if value:
            return Path(value).expanduser()
    return Path.home()


def source_root(explicit: Path | None = None) -> Path:
    """The source root to read from.

    Precedence: the explicit argument, then `STRATARC_SOURCE`, then the nearest directory at or above the current directory that holds `stratarc.toml`, then the current directory.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    value = _env(SOURCE_VARIABLE)
    if value:
        return Path(value).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    return cwd


def projects_root(explicit: Path | None = None, root: Path | None = None) -> Path:
    """The directory that holds project checkouts.

    Precedence: the explicit argument (a command line flag), then `LLM_ROOT_PROJECTS_DIR`, then `projects_root` in the source root's `stratarc.toml`, then `<home>/projects/active`.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    value = _env(PROJECTS_VARIABLE)
    if value:
        return Path(value).expanduser()
    from stratarc.config import load_config

    configured = load_config(source_root(root)).projects_root
    if configured is not None:
        return configured
    return home() / "projects" / "active"


def projects_dir(root: Path | None = None, explicit: Path | None = None) -> Path:
    """The directory whose children are the status directories (`active/`, `archived/`, and so on).

    `projects_root` defaults to `<home>/projects/active`, the directory that holds the checkouts; the status directories are its siblings, so that default maps one level up to `<home>/projects`. A value from the explicit argument, `LLM_ROOT_PROJECTS_DIR` or `projects_root` in `stratarc.toml` names this directory itself and is used as given. Note the argument order differs from `projects_root`: the source root comes first.
    """
    resolved = projects_root(explicit, root)
    if resolved == home() / "projects" / "active":
        return home() / "projects"
    return resolved


def engine_name(root: Path | None = None) -> str:
    """The name the engine puts on everything it generates and records.

    Precedence: `STRATARC_NAME`, then `name` in the source root's `stratarc.toml`, then `stratarc`. Marker comments, permission profile names, ledger and stamp file names and git config keys are all derived from it. A malformed `STRATARC_NAME` raises `ConfigError`.
    """
    from stratarc.config import ConfigError, load_config, validate_name

    value = _env(NAME_VARIABLE)
    if value:
        if not validate_name(value):
            raise ConfigError(f"{NAME_VARIABLE}: name must match ^[a-z][a-z0-9-]*$")
        return value
    return load_config(source_root(root)).name


def github_owner(root: Path | None = None) -> str:
    """The GitHub owner whose repositories count as managed: `STRATARC_GITHUB_OWNER`, else the `owner` in the source root's `stratarc.toml`, else an empty string."""
    value = _env(OWNER_VARIABLE)
    if value:
        return value
    from stratarc.config import load_config

    return load_config(source_root(root)).owner
