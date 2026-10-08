"""Where agentrc finds the home directory, the source root and the projects root.

This module and `agentrc.config` are the only places that read `AGENTRC_HOME`, `AGENTRC_SOURCE`, `LLM_ROOT_PROJECTS_DIR` and `AGENTRC_GITHUB_OWNER`. Every function reads the environment when it is called, never at import, so a test or a command line flag can redirect them after the module is loaded.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_NAME = "agentrc.toml"
HOME_VARIABLE = "AGENTRC_HOME"
SOURCE_VARIABLE = "AGENTRC_SOURCE"
PROJECTS_VARIABLE = "LLM_ROOT_PROJECTS_DIR"
OWNER_VARIABLE = "AGENTRC_GITHUB_OWNER"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def home() -> Path:
    """The home directory every runtime target and `~` expansion hangs under.

    `AGENTRC_HOME` wins when set and non-empty, then `HOME`, then the home directory the platform reports. The override lets a test or a clean-room run redirect every target without touching the real home.
    """
    for name in (HOME_VARIABLE, "HOME"):
        value = _env(name)
        if value:
            return Path(value).expanduser()
    return Path.home()


def source_root(explicit: Path | None = None) -> Path:
    """The source root to read from.

    Precedence: the explicit argument, then `AGENTRC_SOURCE`, then the nearest directory at or above the current directory that holds `agentrc.toml`, then the current directory.
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


def projects_root() -> Path:
    """The directory that holds project checkouts: `LLM_ROOT_PROJECTS_DIR`, else `<home>/projects/active`."""
    value = _env(PROJECTS_VARIABLE)
    if value:
        return Path(value).expanduser()
    return home() / "projects" / "active"


def github_owner(root: Path | None = None) -> str:
    """The GitHub owner whose repositories count as managed: `AGENTRC_GITHUB_OWNER`, else the `owner` in the source root's `agentrc.toml`, else an empty string."""
    value = _env(OWNER_VARIABLE)
    if value:
        return value
    from agentrc.config import load_config

    return load_config(source_root(root)).owner
