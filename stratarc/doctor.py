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


# ----- what each command may touch -----

_SOURCE = "the source root"
_HOME = "the home (.stratarc)"
_TARGETS = "the runtime target directories"
_CHECKOUTS = "the managed project checkouts"


def _row(command: str, reads: list[str], writes: list[str], network: bool = False, runs: list[str] | None = None) -> dict:
    return {"command": command, "reads": reads, "writes": writes, "network": network, "runs": runs or []}


def permissions_table() -> list[dict]:
    """What each command reads, writes, whether it uses the network and what program it may run."""
    read_all = [_SOURCE, _HOME, _TARGETS]
    return [
        _row("init", ["the packaged template"], ["the new source root directory"]),
        _row("doctor", read_all, []),
        _row("doctor --clean --yes", [_HOME], ["old backups and the cache in " + _HOME]),
        _row("doctor --report", read_all, ["a redacted bundle under " + _HOME + "/state/debug"]),
        _row("sync", read_all + [_CHECKOUTS], [_TARGETS, _CHECKOUTS, "derived files in " + _SOURCE, _HOME], runs=["the control plane reconciler (child process)", "the hooks an adapter declares, listed before they run"]),
        _row("sync --verify --rollback-on-drift", read_all + [_CHECKOUTS], [_TARGETS, _CHECKOUTS, "derived files in " + _SOURCE, _HOME + " (backups, reports, log)"], runs=["the control plane reconciler (child process)"]),
        _row("check", read_all + [_CHECKOUTS], []),
        _row("diff", read_all + [_CHECKOUTS], []),
        _row("prune", read_all, ["files a runtime holds that the source no longer owns"]),
        _row("reconcile", [_SOURCE, _CHECKOUTS], ["control-plane.md", "the control plane snapshot in each active project"]),
        _row("validate", [_SOURCE], []),
        _row("projects", [_SOURCE, _CHECKOUTS], [_CHECKOUTS], runs=["git, to read a checkout's state"]),
        _row("gen-rules-digest", [_SOURCE], ["the marked block in the files named"]),
        _row("components", [_SOURCE], [], runs=["a declared launch agent, only with --install-service"]),
        _row("config get|list|explain", [_SOURCE], []),
        _row("log show|tail|explain|export", [_HOME], ["the file named by --output"]),
        _row("log enable|disable|prune", [_HOME], [_HOME]),
        _row("verify run", read_all + [_CHECKOUTS], [_HOME + " (report and log); never a deployed file"]),
        _row("verify last|show", [_HOME], []),
        _row("provider list|show", [_HOME, _SOURCE], []),
        _row("provider add|edit|remove", [_HOME], [_HOME]),
        _row("provider test", [_HOME], [], network=True),
        _row("api schema", [], []),
        _row("api serve", [_HOME, _SOURCE], [_HOME + " (the socket file only; a --port binds 127.0.0.1 and writes nothing)"]),
        _row("adapter list|show|status", [_HOME, _TARGETS], [], runs=["each runtime's --version, for status"]),
        _row("adapter register|remove|deprecate", [_HOME], [_HOME]),
    ]


def render_permissions(rows: list[dict]) -> str:
    width = max(len(r["command"]) for r in rows)
    lines = ["permissions: what each command may touch", f"  {'command':<{width}}  reads | writes | network | runs"]
    for r in rows:
        parts = [
            ", ".join(r["reads"]) or "nothing",
            ", ".join(r["writes"]) or "nothing",
            "yes" if r["network"] else "no",
            ", ".join(r["runs"]) or "nothing",
        ]
        lines.append(f"  {r['command']:<{width}}  " + " | ".join(parts))
    lines.append("A command that would write outside the source root, the home or a registered target stops with exit 3.")
    return "\n".join(lines)


# ----- cleaning the home -----


def clean(yes: bool) -> dict:
    """Plan the removal of old backups and the cache; remove them only when ``yes`` is true."""
    from stratarc import home_layout

    result = home_layout.clean(yes=yes)
    root = home_layout.layout_root()

    def shown(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    return {
        "applied": result.applied,
        "would_remove": [shown(p) for p in result.plan.removals],
        "removed": [shown(p) for p in result.removed],
        "kept": len(result.plan.kept),
    }


def render_clean(report: dict) -> str:
    names = report["removed"] if report["applied"] else report["would_remove"]
    if not names:
        return f"clean: nothing to remove ({report['kept']} kept)"
    head = f"clean: removed {len(names)}" if report["applied"] else f"clean: would remove {len(names)}; pass --yes to remove them"
    return "\n".join([head, *(f"  {name}" for name in names), f"  {report['kept']} kept"])


# ----- the issue report -----


def write_report(data: dict, problems: list[CliError]) -> Path:
    """Write a redacted bundle (versions, the doctor data, the problems, the STRATARC_ variables) under state/debug and return its path."""
    import datetime
    import json

    from stratarc import changelog, home_layout

    now = datetime.datetime.now(datetime.timezone.utc)
    bundle = {
        "schema_version": home_layout.SCHEMA_VERSION,
        "created": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "stratarc": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "doctor": data,
        "problems": [{"code": p.id, "message": p.problem, "hint": p.recovery} for p in problems],
        "environment": {name: value for name, value in sorted(os.environ.items()) if name.startswith("STRATARC_") and name != "STRATARC_LOG"},
    }
    text = json.dumps(bundle, indent=2, default=str).replace(str(paths.home()), "~")
    text = changelog.redact(text)
    path = home_layout.state_dir() / "debug" / f"doctor-{now.strftime('%Y%m%dT%H%M%S')}.json"
    home_layout.safe_write(path, text + "\n")
    return path
