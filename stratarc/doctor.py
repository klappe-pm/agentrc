"""`stratarc doctor`: report the health of the install, the home and the source root.

The checks only read. `collect` returns plain data and the problems it found as `CliError` values; `render` turns the data into the human report. The command line wraps the same data in the JSON envelope.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from stratarc import __version__, paths
from stratarc.messages import CliError


def _writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _source_origin(root_flag: bool, root: Path) -> str:
    if root_flag:
        return "--root"
    if os.environ.get(paths.SOURCE_VARIABLE, "").strip():
        return paths.SOURCE_VARIABLE
    if (root / paths.CONFIG_NAME).is_file():
        return f"nearest {paths.CONFIG_NAME}"
    return "current directory"


def collect(*, root_flag: bool, started_ms: float) -> tuple[dict, list[CliError]]:
    """Gather the report data and the problems found; never raises for a bad install or source."""
    from stratarc.config import ConfigError, load_config

    problems: list[CliError] = []

    home = paths.home()
    home_writable = _writable(home)
    if not home_writable:
        problems.append(CliError("msg-1003", param="home", path=home))

    root = paths.source_root()
    root_ok = root.is_dir()
    if not root_ok:
        problems.append(CliError("msg-1001", param="root", path=root))
    config_file = root / paths.CONFIG_NAME

    config = None
    config_error = ""
    if root_ok:
        try:
            config = load_config(root)
        except ConfigError as error:
            config_error = str(error)
            problems.append(CliError("msg-1002", param=paths.CONFIG_NAME, detail=config_error))

    runtimes: list[dict] = []
    adapters = 0
    if config is not None:
        from stratarc.adapters._common import runtime_registry
        from stratarc.sync import load_runtimes

        adapters = len(runtime_registry())
        for name, (_module, target) in load_runtimes(root=root).items():
            declared = config.runtimes.get(name)
            runtimes.append(
                {
                    "name": name,
                    "enabled": declared.enabled if declared is not None else True,
                    "target": str(target),
                    "target_exists": target.is_dir(),
                }
            )

    data = {
        "python": platform.python_version(),
        "stratarc": __version__,
        "home": {"path": str(home), "exists": home.is_dir(), "writable": home_writable},
        "source_root": {
            "path": str(root),
            "exists": root_ok,
            "resolved_from": _source_origin(root_flag, root),
            "config_file": config_file.is_file(),
            "config_error": config_error,
        },
        "runtimes": runtimes,
        "adapters": adapters,
        "start_ms": round(started_ms, 1),
    }
    return data, problems


def render(data: dict, problems: list[CliError]) -> str:
    """The human report: one line per fact, then each problem with its recovery."""
    home = data["home"]
    source = data["source_root"]
    lines = [
        f"python        {data['python']}",
        f"stratarc      {data['stratarc']}",
        f"home          {home['path']} ({'writable' if home['writable'] else 'not writable'})",
        f"source root   {source['path']} (from {source['resolved_from']})",
        f"adapters      {data['adapters']} registered",
        f"start time    {data['start_ms']} ms",
    ]
    if data["runtimes"]:
        lines.append("runtimes")
        for runtime in data["runtimes"]:
            state = "enabled" if runtime["enabled"] else "disabled"
            target = "present" if runtime["target_exists"] else "absent"
            lines.append(f"  {runtime['name']:<10} {state:<9} {runtime['target']} ({target})")
    for problem in problems:
        lines.append(f"problem {problem.id}  {problem.problem}")
        lines.append(f"  {problem.recovery}")
    if not problems:
        lines.append("ok")
    return "\n".join(lines)
