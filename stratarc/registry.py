"""The adapter registry: manifests, version-range status, deprecation and the deploy gate.

An adapter manifest names the adapter, the runtime it translates to, the runtime versions (`supports`) and source schema versions (`schema`) it handles, the source kinds it renders and the files it writes. Bundled adapters get a manifest derived from `stratarc.adapters`; `register` stores another one under `<home>/.stratarc/adapters/<name>/manifest.json`, and a registered manifest overrides the bundled one of the same name.

Status per adapter:

- `ok`: the installed runtime and the source schema are inside the declared ranges.
- `outdated`: inside `supports` but newer than `tested`, the newest version the adapter was verified against.
- `unsupported`: the runtime version or the source schema is outside the declared range.
- `unknown`: the installed runtime version is not known.

The deploy gate (`sync_gate`) refuses `unsupported`, and warns once per run for `outdated`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from stratarc import home_layout as layout
from stratarc.home_layout import Conflict, InvalidInput, ToolError, Unavailable

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
RANGE_PATTERN = re.compile(r"^(\*|(>=|<=|==|>|<)\d+(\.\d+)*(,(>=|<=|==|>|<)\d+(\.\d+)*)*)$")
VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)*")
SOURCE_KINDS = ("rules", "hooks", "skills", "commands", "agents", "permissions", "components")
MANIFEST_KEYS = {"schema_version", "name", "runtime", "description", "supports", "tested", "schema", "source_kinds", "files_written", "module", "origin", "source"}
REQUIRED_KEYS = ("schema_version", "name", "runtime", "supports", "schema", "source_kinds", "files_written")
# The source schema generation the engine reads and writes today.
SOURCE_SCHEMA_VERSION = 1

OK, OUTDATED, UNSUPPORTED, UNKNOWN = "ok", "outdated", "unsupported", "unknown"


# ---------------------------------------------------------------------------
# versions and ranges
# ---------------------------------------------------------------------------


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """The first dotted number in `text` ("codex-cli 0.9.1" gives (0, 9, 1)), or None."""
    if not text:
        return None
    match = VERSION_PATTERN.search(str(text))
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _compare(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def in_range(version: tuple[int, ...], spec: str) -> bool:
    """True when `version` satisfies every comparator of `spec` (`>=0.4,<0.9`, `==2`, or `*`)."""
    if not RANGE_PATTERN.match(spec):
        raise InvalidInput(f"The range {spec!r} is not valid.", hint="Use comma separated comparators such as >=0.4,<0.9, or *.", param="range", code="range-invalid")
    if spec == "*":
        return True
    for comparator in spec.split(","):
        operator = re.match(r"(>=|<=|==|>|<)", comparator).group(1)
        bound = parse_version(comparator[len(operator):])
        order = _compare(version, bound)
        satisfied = {">=": order >= 0, "<=": order <= 0, "==": order == 0, ">": order > 0, "<": order < 0}[operator]
        if not satisfied:
            return False
    return True


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------


def validate_manifest(doc: Any) -> list[str]:
    """Every problem in an adapter manifest; an empty list means it is valid."""
    if not isinstance(doc, dict):
        return ["a manifest must be a JSON object"]
    errors = [f"unknown key {key!r}" for key in sorted(set(doc) - MANIFEST_KEYS)]
    errors += [f"{key} is required" for key in REQUIRED_KEYS if key not in doc]
    version = doc.get("schema_version")
    if "schema_version" in doc and (not isinstance(version, int) or isinstance(version, bool) or version < 1):
        errors.append("schema_version must be a positive integer")
    for key in ("name", "runtime"):
        if key in doc and (not isinstance(doc[key], str) or not NAME_PATTERN.match(doc[key])):
            errors.append(f"{key} must match ^[a-z][a-z0-9-]*$")
    for key in ("supports", "schema"):
        if key in doc and (not isinstance(doc[key], str) or not RANGE_PATTERN.match(doc[key])):
            errors.append(f"{key} must be a range such as >=0.4,<0.9, or *")
    if "tested" in doc and (not isinstance(doc["tested"], str) or parse_version(doc["tested"]) is None):
        errors.append("tested must be a dotted version number")
    kinds = doc.get("source_kinds")
    if "source_kinds" in doc:
        if not isinstance(kinds, list) or not all(isinstance(k, str) and NAME_PATTERN.match(k) for k in kinds) or len(set(kinds)) != len(kinds):
            errors.append("source_kinds must be a list of distinct kind names")
    files = doc.get("files_written")
    if "files_written" in doc:
        if not isinstance(files, list) or not all(isinstance(f, str) and f for f in files) or len(set(files)) != len(files):
            errors.append("files_written must be a list of distinct relative paths")
        elif any(f.startswith(("/", "~")) or ".." in Path(f).parts for f in files):
            errors.append("files_written paths must be relative to the runtime target and stay inside it")
    return errors


def _require_valid_manifest(doc: Any, where: str) -> None:
    errors = validate_manifest(doc)
    if errors:
        raise InvalidInput(f"The adapter manifest {where} is not valid: " + "; ".join(errors) + ".", hint="Fix the listed fields; see the adapter manifest schema.", param="path", code="manifest-invalid")


def bundled_manifests() -> dict[str, dict[str, Any]]:
    """A manifest per bundled adapter, derived from the adapter modules.

    The runtime and the files written come from each module's `RUNTIME` constant. `supports` and `tested` come from the table in `stratarc.adapters._supports`; an adapter with no row there (a fixture runtime) stays permissive (`*`, no `tested`).
    """
    from stratarc.adapters._common import runtime_registry
    from stratarc.adapters._supports import SUPPORTS

    manifests: dict[str, dict[str, Any]] = {}
    for name, runtime in runtime_registry().items():
        written = [f"{runtime.relative}/**"]
        if runtime.hook_registry:
            written.insert(0, f"{runtime.relative}/{runtime.hook_registry}")
        row = SUPPORTS.get(name)
        manifests[name] = {
            "schema_version": layout.SCHEMA_VERSION,
            "name": name,
            "runtime": name,
            "description": f"Translates the source into the {name} runtime.",
            "supports": row["supports"] if row else "*",
            **({"tested": row["tested"]} if row else {}),
            "schema": f">={SOURCE_SCHEMA_VERSION},<{SOURCE_SCHEMA_VERSION + 1}",
            "source_kinds": _bundled_kinds(name),
            "files_written": written,
            "module": runtime.module,
            "origin": "bundled",
        }
    return manifests


def _bundled_kinds(name: str) -> list[str]:
    from importlib import resources

    try:
        text = (resources.files("stratarc.adapters") / f"{name}.py").read_text(encoding="utf-8")
    except OSError:
        return []
    return [kind for kind in SOURCE_KINDS if re.search(rf"\b{kind}\b", text)]


def _stored(name: str) -> Path:
    return layout.adapters_dir() / name / "manifest.json"


def _load_registered() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    directory = layout.adapters_dir()
    if not directory.is_dir():
        return found
    for entry in sorted(directory.iterdir()):
        manifest = entry / "manifest.json"
        if not manifest.is_file():
            continue
        layout.check_not_newer(manifest)
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not validate_manifest(doc) and doc.get("name") == entry.name:
            found[entry.name] = doc
    return found


def list_adapters() -> dict[str, dict[str, Any]]:
    """Every adapter by name: bundled manifests, overridden by registered ones."""
    adapters = bundled_manifests()
    # A copy that `register_bundled` stored earlier is only a snapshot of the derived manifest, so it never overrides the current derivation (and its ranges).
    adapters.update({name: doc for name, doc in _load_registered().items() if doc.get("origin") != "bundled"})
    return dict(sorted(adapters.items()))


def get(name: str) -> dict[str, Any]:
    adapters = list_adapters()
    if name not in adapters:
        raise InvalidInput(f"The adapter {name} is not registered.", hint="List them with `stratarc adapter list`.", param="name", code="adapter-unknown")
    return adapters[name]


def _read_manifest_source(spec: str) -> tuple[dict[str, Any], str]:
    candidate = Path(spec).expanduser()
    if candidate.is_dir():
        candidate = candidate / "manifest.json"
    if candidate.is_file():
        try:
            return json.loads(candidate.read_text(encoding="utf-8")), str(candidate)
        except (OSError, ValueError) as error:
            raise InvalidInput(f"The manifest {candidate.name} cannot be read: {error}", hint="Fix the file.", param="path", code="manifest-unreadable") from None
    if re.match(r"^[A-Za-z_][\w.]*$", spec):
        try:
            found = importlib.util.find_spec(spec)
        except (ImportError, ValueError):
            found = None
        for location in (found.submodule_search_locations or []) if found else []:
            path = Path(location) / "manifest.json"
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8")), f"package:{spec}"
                except (OSError, ValueError) as error:
                    raise InvalidInput(f"The manifest in package {spec} cannot be read: {error}", hint="Fix the file.", param="path", code="manifest-unreadable") from None
    raise InvalidInput(f"No adapter manifest was found at {spec}.", hint="Pass a manifest.json file, a directory holding one, or an installed package that ships one.", param="path", code="manifest-missing")


def register(path_or_package: str, *, replace: bool = False) -> dict[str, Any]:
    """Read an adapter manifest, validate it and store it under `adapters/<name>/`."""
    doc, source = _read_manifest_source(str(path_or_package))
    _require_valid_manifest(doc, source)
    if doc["schema_version"] > layout.SCHEMA_VERSION:
        raise Unavailable(f"The manifest {source} declares schema version {doc['schema_version']}, newer than this stratarc understands.", hint="Upgrade stratarc.", param="path", code="newer-schema")
    name = doc["name"]
    if _stored(name).exists() and not replace:
        raise Conflict(f"The adapter {name} is already registered.", hint="Pass --replace to register it again.", param="name", code="adapter-exists")
    stored = {**doc, "origin": "registered", "source": source}
    layout.ensure_layout()
    layout.safe_write(_stored(name), json.dumps(stored, indent=2) + "\n")
    return stored


def register_bundled(*, overwrite: bool = False) -> list[str]:
    """Store the derived manifest of each bundled adapter that has none yet; the install runs this. Returns the names written."""
    layout.ensure_layout()
    written = []
    for name, doc in bundled_manifests().items():
        if _stored(name).exists() and not overwrite:
            continue
        layout.safe_write(_stored(name), json.dumps(doc, indent=2) + "\n")
        written.append(name)
    return written


def remove(name: str) -> None:
    """Drop a registered adapter's directory after backing up its files. A bundled adapter reverts to its derived manifest."""
    directory = layout.adapters_dir() / name
    if not NAME_PATTERN.match(name) or not directory.is_dir():
        raise InvalidInput(f"The adapter {name} is not registered.", hint="List them with `stratarc adapter list`.", param="name", code="adapter-unknown")
    for file in sorted(directory.glob("*.json")):
        layout._backup(file, None)
    shutil.rmtree(directory)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    runtime: str
    state: str
    detail: str
    installed: str | None
    supports: str
    deprecated: bool = False


def evaluate(manifest: Mapping[str, Any], installed: str | None, *, schema: int = SOURCE_SCHEMA_VERSION, deprecated: bool = False) -> AdapterStatus:
    """The status of one adapter against an installed runtime version and the source schema."""
    name, runtime, supports = manifest["name"], manifest["runtime"], manifest["supports"]

    def status(state: str, detail: str) -> AdapterStatus:
        return AdapterStatus(name, runtime, state, detail, installed, supports, deprecated)

    if not in_range((schema,), manifest["schema"]):
        return status(UNSUPPORTED, f"The source schema {schema} is outside the declared range {manifest['schema']}.")
    version = parse_version(installed)
    if version is None:
        return status(UNKNOWN, f"The installed {runtime} version is not known.")
    if not in_range(version, supports):
        return status(UNSUPPORTED, f"{runtime} {installed} is outside the declared range {supports}.")
    tested = parse_version(manifest.get("tested"))
    if tested is not None and _compare(version, tested) > 0:
        return status(OUTDATED, f"{runtime} {installed} is newer than {manifest['tested']}, the newest version this adapter was tested against.")
    return status(OK, f"{runtime} {installed} is inside {supports}.")


def status(installed_runtime_versions: Mapping[str, str | None], *, schema: int = SOURCE_SCHEMA_VERSION) -> list[AdapterStatus]:
    """One status per adapter, by name. Keys of `installed_runtime_versions` are runtime names."""
    deprecated = {name for name in list_adapters() if read_deprecation(name) is not None}
    return [
        evaluate(manifest, installed_runtime_versions.get(manifest["runtime"]), schema=schema, deprecated=name in deprecated)
        for name, manifest in list_adapters().items()
    ]


DETECT_TIMEOUT_SECONDS = 5

# Variables a detection command may see besides the temporary homes. Anything else, tokens included, is dropped.
_DETECT_ENV_ALLOWED = ("PATH", "LANG")
_DETECT_ENV_PREFIXES = ("LC_",)
# Variables that move a runtime's own directories, all pointed at the temporary home.
_DETECT_HOME_VARIABLES = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "CODEX_HOME",
    "GEMINI_CONFIG_DIR",
    "CURSOR_CONFIG_DIR",
    "OPENCODE_CONFIG_DIR",
    "CLAUDE_CONFIG_DIR",
)

# Detected versions for the life of the process, keyed by runtime and resolved command path. Only the real runner is cached.
_DETECTED: dict[tuple[str, str], str | None] = {}


def _detection_environment(home: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _DETECT_ENV_ALLOWED or k.startswith(_DETECT_ENV_PREFIXES)}
    env.update({"TERM": "dumb", "NO_COLOR": "1", "CI": "1"})
    env.update({name: home for name in _DETECT_HOME_VARIABLES})
    return env


def detect_versions(runner: Callable[..., Any] | None = None, which: Callable[[str], str | None] | None = None) -> dict[str, str | None]:
    """Ask each runtime's command for its version, using the table in `stratarc.adapters._supports`. `runner` and `which` are injectable.

    Each command runs in a fresh temporary home with an allowlisted environment, that directory as its working directory, stdin closed and a hard timeout, so a runtime cannot read the caller's credentials or create files in the caller's home. Any failure of one detection yields None for that runtime. Results of the real runner are cached for the process.
    """
    from stratarc.adapters._supports import DETECT

    run = runner or subprocess.run
    find = which or shutil.which
    versions: dict[str, str | None] = {}
    with tempfile.TemporaryDirectory(prefix="stratarc-detect-") as home:
        env = _detection_environment(home)
        for runtime, (command, arguments, pattern) in DETECT.items():
            versions[runtime] = None
            found = find(command)
            if found is None:
                continue
            key = (runtime, str(found))
            if runner is None and key in _DETECTED:
                versions[runtime] = _DETECTED[key]
                continue
            try:
                result = run(
                    [command, *arguments],
                    capture_output=True,
                    text=True,
                    timeout=DETECT_TIMEOUT_SECONDS,
                    env=env,
                    cwd=home,
                    stdin=subprocess.DEVNULL,
                )
                output = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
                match = re.search(pattern, output)
                parsed = parse_version(match.group(1)) if match else None
                detected = ".".join(str(p) for p in parsed) if parsed else None
            except (OSError, ValueError, subprocess.SubprocessError):
                detected = None
            versions[runtime] = detected
            if runner is None:
                _DETECTED[key] = detected
    return versions


# ---------------------------------------------------------------------------
# the deploy gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    state: str
    message: str | None = None


def sync_gate(adapter: AdapterStatus, warned: set[str] | None = None) -> GateDecision:
    """Whether a deploy may go through this adapter.

    `unsupported` blocks, every time. `outdated` is allowed and carries a warning the first time only; pass the same `warned` set for the whole run. `ok` and `unknown` are allowed without a message.
    """
    if adapter.state == UNSUPPORTED:
        return GateDecision(False, adapter.state, f'The adapter "{adapter.name}" does not support this install: {adapter.detail} Update it with `stratarc adapter register`, or pin the runtime to a supported version.')
    if adapter.state == OUTDATED:
        seen = warned if warned is not None else set()
        if adapter.name in seen:
            return GateDecision(True, adapter.state, None)
        seen.add(adapter.name)
        return GateDecision(True, adapter.state, f'The adapter "{adapter.name}" is outdated: {adapter.detail} Deploying anyway.')
    return GateDecision(True, adapter.state, None)


def require_deployable(adapter: AdapterStatus, warned: set[str] | None = None) -> GateDecision:
    """`sync_gate`, raising the unavailable error when the adapter blocks."""
    decision = sync_gate(adapter, warned)
    if not decision.allowed:
        raise Unavailable(decision.message or "The adapter is unsupported.", hint="Run `stratarc adapter status` to see the declared range.", param="adapter", code="adapter-unsupported")
    return decision


# ---------------------------------------------------------------------------
# deprecation
# ---------------------------------------------------------------------------


def _deprecation_path(name: str) -> Path:
    return layout.adapters_dir() / name / "deprecation.json"


def read_deprecation(name: str) -> dict[str, Any] | None:
    path = _deprecation_path(name)
    layout.check_not_newer(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def deprecate(name: str, reason: str, end_date: str, replacement: str | None = None) -> dict[str, Any]:
    """Mark an adapter as no longer supported from `end_date` (ISO), with a reason and an optional replacement."""
    get(name)
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidInput("A deprecation needs a reason.", hint="Pass --reason.", param="reason", code="deprecation-reason")
    try:
        date.fromisoformat(end_date)
    except (TypeError, ValueError):
        raise InvalidInput(f"The end date {end_date!r} is not an ISO date.", hint="Use YYYY-MM-DD.", param="end_date", code="deprecation-date") from None
    if replacement is not None:
        get(replacement)
    record = {"schema_version": layout.SCHEMA_VERSION, "name": name, "reason": reason.strip(), "end_date": end_date, "replacement": replacement, "answer": None, "answered_at": None}
    layout.ensure_layout()
    layout.safe_write(_deprecation_path(name), json.dumps(record, indent=2) + "\n")
    return record


def deprecation_notice(name: str) -> str:
    """The text of the notice: the adapter, what it affects, when support ends and what replaces it."""
    record = read_deprecation(name)
    if record is None:
        raise InvalidInput(f"The adapter {name} is not deprecated.", hint="Mark it with `stratarc adapter deprecate`.", param="name", code="deprecation-unknown")
    manifest = get(name)
    lines = [
        f'The adapter "{name}" is deprecated: {record["reason"]}',
        f'  It translates to {manifest["runtime"]} and writes: {", ".join(manifest["files_written"])}.',
        f'  Support ends on {record["end_date"]}.',
    ]
    lines.append(f'  Replacement: {record["replacement"]}.' if record.get("replacement") else "  There is no replacement.")
    return "\n".join(lines)


def check_deprecations(*, interactive: bool, ask: Callable[[str], str] | None = None, write: Callable[[str], None] | None = None, now: datetime | None = None) -> list[str]:
    """Show the deprecation notice for each deprecated adapter whose notice has not been answered.

    Interactive: print the notice and ask once; the answer is recorded and the question is not asked again. Noninteractive: print the notice and continue; nothing is recorded, so the first interactive command still asks. Returns the notices shown.
    """
    emit = write or print
    prompt = ask or input
    shown: list[str] = []
    for name in list_adapters():
        record = read_deprecation(name)
        if record is None or record.get("answer") is not None:
            continue
        notice = deprecation_notice(name)
        emit(notice)
        shown.append(notice)
        if not interactive:
            continue
        try:
            reply = prompt("Keep using it until then? [y/N] ").strip().lower()
        except EOFError:
            continue
        record["answer"] = "continue" if reply in ("y", "yes") else "stop"
        record["answered_at"] = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        layout.safe_write(_deprecation_path(name), json.dumps(record, indent=2) + "\n")
    return shown


# ---------------------------------------------------------------------------
# command line: `adapter <verb>`
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stratarc adapter", description="Manage adapters, the translators from the source to a runtime.")
    verbs = parser.add_subparsers(dest="verb", required=True)

    def common(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--json", action="store_true", help="print one JSON envelope instead of text")
        return sub

    common(verbs.add_parser("list", help="list adapters"))
    for verb in ("show", "remove"):
        common(verbs.add_parser(verb)).add_argument("name")
    for verb in ("register", "add"):
        sub = common(verbs.add_parser(verb, help="register an adapter from a manifest path or package"))
        sub.add_argument("source", metavar="path-or-package")
        sub.add_argument("--replace", action="store_true")
    status_parser = common(verbs.add_parser("status", help="compare each adapter with the installed runtime"))
    status_parser.add_argument("--runtime-version", action="append", default=[], metavar="RUNTIME=VERSION", help="override a detected runtime version")
    deprecate_parser = common(verbs.add_parser("deprecate", help="mark an adapter as no longer supported"))
    deprecate_parser.add_argument("name")
    deprecate_parser.add_argument("--reason", required=True)
    deprecate_parser.add_argument("--end-date", required=True, metavar="YYYY-MM-DD")
    deprecate_parser.add_argument("--replacement")
    return parser


def _describe(manifest: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f'{manifest["name"]}  runtime {manifest["runtime"]}  supports {manifest["supports"]}  schema {manifest["schema"]}  ({manifest.get("origin", "registered")})',
            "  renders: " + (", ".join(manifest["source_kinds"]) or "(none)"),
            "  writes: " + ", ".join(manifest["files_written"]),
        ]
    )


def main(argv: list[str] | None = None, *, detect: Callable[[], Mapping[str, str | None]] | None = None, interactive: bool | None = None, ask: Callable[[str], str] | None = None) -> int:
    """Run an `adapter` verb. `detect` replaces runtime version detection; `interactive` and `ask` drive the deprecation prompt."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    try:
        if interactive is None:
            import sys

            interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.json
        if args.verb != "deprecate" and not args.json:
            check_deprecations(interactive=interactive, ask=ask)
        if args.verb == "list":
            adapters = list_adapters()
            return layout.emit(args.json, data={"adapters": list(adapters.values())}, text="\n".join(_describe(m) for m in adapters.values()))
        if args.verb == "show":
            manifest = get(args.name)
            data = {**manifest, "deprecation": read_deprecation(args.name)}
            return layout.emit(args.json, data=data, text=_describe(manifest))
        if args.verb in ("register", "add"):
            stored = register(args.source, replace=args.replace)
            return layout.emit(args.json, data=stored, text=f'Registered adapter {stored["name"]}.')
        if args.verb == "remove":
            remove(args.name)
            return layout.emit(args.json, data={"name": args.name}, text=f"Removed the registered adapter {args.name}; a backup was kept.")
        if args.verb == "deprecate":
            record = deprecate(args.name, args.reason, args.end_date, args.replacement)
            return layout.emit(args.json, data=record, text=f'Marked adapter {args.name} deprecated; support ends {args.end_date}.')
        versions = dict((detect or detect_versions)())
        for item in args.runtime_version:
            runtime, _, version = item.partition("=")
            if not runtime or not version:
                raise InvalidInput(f"The runtime version {item!r} is not RUNTIME=VERSION.", hint="Pass --runtime-version codex=0.9.1.", param="runtime-version", code="runtime-version")
            versions[runtime] = version
        statuses = status(versions)
        rows = [{"name": s.name, "runtime": s.runtime, "state": s.state, "detail": s.detail, "installed": s.installed, "supports": s.supports, "deprecated": s.deprecated} for s in statuses]
        text = "\n".join(f"{s.name}: {s.state}{' (deprecated)' if s.deprecated else ''}  {s.detail}" for s in statuses)
        code = layout.emit(args.json, data={"adapters": rows}, text=text)
        return code
    except ToolError as error:
        return layout.emit(args.json, error=error)
