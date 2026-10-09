"""Shared helpers for the sync adapters."""

from __future__ import annotations

import ast
import dataclasses
import os
import re
import shutil
import stat
from contextlib import ExitStack
from importlib import resources
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from agentrc.paths import home


# ---------------------------------------------------------------------------
# The runtime registry. Each adapter module declares its runtime in
# one RUNTIME constant, and runtime_registry() lists them. The sync, component,
# session launcher and audit tooling read this instead of keeping their own lists, so
# bringing a runtime under management is one adapter file, one
# permissions.json runtimeDirectories entry and one reconciled row.
#
# The constant is read with ast.literal_eval, never by importing the module:
# listing runtimes then costs no adapter import, cannot cycle through this
# module (every adapter imports it), and works on a fixture directory whose
# modules are not importable.
# ---------------------------------------------------------------------------

_RUNTIME_KEYS = {"name", "target", "hook_registry", "hook_events"}


class RegistryError(ValueError):
    """An adapter file whose RUNTIME constant is missing or malformed."""


@dataclasses.dataclass(frozen=True)
class Runtime:
    """One managed runtime, as its adapter module declares it.

    relative is the target directory relative to the home directory
    (".claude", ".config/opencode"). hook_registry is the file under the
    target that holds hook registrations the reverse pass reads, or None for
    a runtime with no JSON hook registry (OpenCode). hook_events are the
    canonical hooks/hooks.json events the adapter handles, a deliberate drop
    included; sync.py refuses a runtime missing any event the stage declares.
    """

    name: str
    module: str
    relative: str
    hook_registry: Optional[str]
    hook_events: FrozenSet[str]

    def target(self, home_dir: Optional[Path] = None) -> Path:
        return (home_dir if home_dir is not None else home()) / self.relative


def _adapter_files(directory: Path) -> List[Path]:
    """Adapter modules: every *.py but tests, this module and __init__."""
    return sorted(
        path
        for path in directory.glob("*.py")
        if not path.stem.startswith("_") and not path.name.endswith(".test.py")
    )


def _read_runtime_constant(path: Path) -> Runtime:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise RegistryError(f"{path.name}: cannot be read: {error}") from None
    value = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "RUNTIME" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                raise RegistryError(f"{path.name}: RUNTIME must be a literal dict") from None
    if value is None:
        raise RegistryError(f"{path.name}: declares no RUNTIME constant")
    if not isinstance(value, dict):
        raise RegistryError(f"{path.name}: RUNTIME must be a dict")
    unknown = sorted(set(value) - _RUNTIME_KEYS)
    if unknown:
        raise RegistryError(f"{path.name}: RUNTIME has unknown key {', '.join(unknown)}")
    name = value.get("name")
    if name != path.stem:
        raise RegistryError(f"{path.name}: RUNTIME name {name!r} must equal the file name {path.stem!r}")
    relative = value.get("target")
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith(("/", "~"))
        or ".." in Path(relative).parts
    ):
        raise RegistryError(f"{path.name}: RUNTIME target must be a directory relative to the home directory")
    registry = value.get("hook_registry")
    if registry is not None and (not isinstance(registry, str) or not registry):
        raise RegistryError(f"{path.name}: RUNTIME hook_registry must be a file name or None")
    events = value.get("hook_events")
    if not isinstance(events, list) or not all(isinstance(e, str) and e for e in events):
        raise RegistryError(f"{path.name}: RUNTIME hook_events must be a list of event names")
    return Runtime(
        name=name,
        module=f"agentrc.adapters.{name}",
        relative=relative,
        hook_registry=registry,
        hook_events=frozenset(events),
    )


def runtime_registry(adapters_dir: Optional[Path] = None) -> Dict[str, Runtime]:
    """Every runtime an adapter declares, by name, sorted by name.

    adapters_dir defaults to this package. A file without a well-formed
    RUNTIME constant raises RegistryError naming it, so a new adapter cannot
    be silently left out of every list.
    """
    with ExitStack() as stack:
        if adapters_dir is not None:
            directory = adapters_dir
        else:
            directory = stack.enter_context(resources.as_file(resources.files("agentrc.adapters")))
        runtimes = [_read_runtime_constant(path) for path in _adapter_files(directory)]
    return {runtime.name: runtime for runtime in sorted(runtimes, key=lambda r: r.name)}


def _walk_configuration(base: Path):
    """Walk deployable files, leaving interpreter caches owned by the runtime."""
    for root, dirs, files in os.walk(base):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        yield (
            root,
            dirs,
            [name for name in files if not name.endswith((".pyc", ".pyo"))],
        )


def copy_preserving_mode(src: Path, dst: Path, dry_run: bool = False) -> str:
    """Copy a file, preserving the executable bit. Returns action description."""
    verb = "would copy" if dry_run else "copy"
    action = f"{verb} {src} -> {dst}"
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # Preserve exec bit explicitly
        src_mode = src.stat().st_mode
        if src_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            dst.chmod(dst.stat().st_mode | (src_mode & 0o111))
    return action


def mirror_dir(
    src_dir: Path,
    dst_dir: Path,
    dry_run: bool = False,
    *,
    delete_extra: bool = True,
    exclude_patterns: Optional[List[str]] = None,
    file_glob: Optional[str] = None,
) -> List[str]:
    """Mirror src_dir into dst_dir. Returns list of action descriptions.

    Args:
        delete_extra: if True, remove files in dst that are not in src.
        exclude_patterns: glob patterns to skip in source (e.g. '*.test.sh').
        file_glob: if set, only copy files matching this glob (applied per-dir).
    """
    actions: List[str] = []
    if not src_dir.is_dir():
        return actions

    # Collect source relative paths
    src_files: set[str] = set()
    for root_path, _dirs, files in _walk_configuration(src_dir):
        root = Path(root_path)
        for fname in files:
            fpath = root / fname
            rel = fpath.relative_to(src_dir)

            # Apply exclusions
            skip = False
            if exclude_patterns:
                for pat in exclude_patterns:
                    if fpath.match(pat):
                        skip = True
                        break
            if skip:
                continue

            if file_glob and not fpath.match(file_glob):
                continue

            src_files.add(str(rel))
            dst_path = dst_dir / rel

            # Only copy if changed or missing
            needs_copy = True
            if dst_path.exists():
                if dst_path.stat().st_size == fpath.stat().st_size:
                    if dst_path.read_bytes() == fpath.read_bytes():
                        needs_copy = False

            if needs_copy:
                actions.append(copy_preserving_mode(fpath, dst_path, dry_run))

    # Delete files in dst not in src
    if delete_extra and dst_dir.is_dir():
        for root_path, _dirs, files in _walk_configuration(dst_dir):
            root = Path(root_path)
            for fname in files:
                fpath = root / fname
                rel = str(fpath.relative_to(dst_dir))
                if rel not in src_files:
                    verb = "would delete" if dry_run else "delete"
                    actions.append(f"{verb} {fpath}")
                    if not dry_run:
                        fpath.unlink()

        # Clean empty dirs in dst after deletion
        if not dry_run:
            _remove_empty_dirs(dst_dir)

    return actions


def skill_excludes(src_dir: Path) -> set:
    """Skills listed in skills/sync-exclude.json are host-adapted and not synced."""
    cfg = src_dir / "sync-exclude.json"
    if not cfg.is_file():
        return set()
    try:
        import json

        return set(json.loads(cfg.read_text()).get("exclude", {}).keys())
    except Exception:
        return set()


def mirror_skills(src_dir: Path, dst_dir: Path, dry_run: bool = False) -> List[str]:
    """Mirror skill directories: add and update, never delete, here.

    A skill directory absent from source is not touched by this loop. Deletion of
    a target-only skill directory belongs to `sync.py --prune`, driven by
    each adapter's `owned_outputs`, not to this copy loop.
    """
    actions: List[str] = []
    if not src_dir.is_dir():
        return actions

    excluded = skill_excludes(src_dir)
    for skill_dir in sorted(src_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name in excluded:
            continue
        dst_skill = dst_dir / skill_dir.name
        # Mirror contents within each skill dir, but do not delete extra files
        # within a skill dir either -- only add/update
        for root_path, _dirs, files in os.walk(skill_dir):
            root = Path(root_path)
            for fname in files:
                fpath = root / fname
                rel = fpath.relative_to(skill_dir)
                dst_path = dst_skill / rel
                needs_copy = True
                if dst_path.exists():
                    if dst_path.stat().st_size == fpath.stat().st_size:
                        if dst_path.read_bytes() == fpath.read_bytes():
                            needs_copy = False
                if needs_copy:
                    actions.append(copy_preserving_mode(fpath, dst_path, dry_run))
    return actions


def _remove_empty_dirs(base: Path) -> None:
    """Remove empty subdirectories bottom-up, but not base itself."""
    for root_path, dirs, files in reversed(list(_walk_configuration(base))):
        root = Path(root_path)
        if root == base:
            continue
        if not any(root.iterdir()):
            root.rmdir()


def read_frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    """Parse leading --- YAML frontmatter into (dict, body) using only stdlib.

    Handles:
      - key: value (strings, unquoted or quoted)
      - key: [a, b, c] (inline lists)
      - multi-line values via continuation lines (indented under key)
      - folded (>, >-) and literal (|, |-) block scalars: folded lines are
        joined with spaces, literal lines keep their breaks. A live check found
        the indicator itself kept as the value, so six agents reached Codex with
        description = ">-".
      - boolean-ish and numeric values kept as strings
    """
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    end_match = re.search(r"\n---\s*\n", text[3:])
    if not end_match:
        return {}, text

    fm_block = text[3 : 3 + end_match.start()]
    body = text[3 + end_match.end() :]

    meta: Dict[str, object] = {}
    current_key: Optional[str] = None
    current_lines: List[str] = []
    current_style = ""

    def _flush() -> None:
        nonlocal current_key, current_lines, current_style
        if current_key is not None:
            if current_style == ">":
                val = " ".join(line.strip() for line in current_lines if line.strip())
            elif current_style == "|":
                val = "\n".join(line.strip() for line in current_lines).strip()
            else:
                val = "\n".join(current_lines).strip()
            meta[current_key] = val
            current_key = None
            current_lines = []
            current_style = ""

    for line in fm_block.split("\n"):
        # New key: value
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)", line)
        if m:
            _flush()
            key = m.group(1)
            val = m.group(2).strip()

            # Inline list: [a, b, c]
            list_match = re.match(r"^\[(.+)\]$", val)
            if list_match:
                items = [
                    item.strip().strip("'\"") for item in list_match.group(1).split(",")
                ]
                meta[key] = items
            elif re.fullmatch(r"[>|][+-]?", val):
                current_key = key
                current_lines = []
                current_style = val[0]
            elif val:
                meta[key] = val
            else:
                # Value on next lines
                current_key = key
                current_lines = []
        elif current_key is not None and (
            line.startswith("  ") or line.startswith("\t") or line == ""
        ):
            current_lines.append(line)
        else:
            _flush()

    _flush()
    return meta, body


# Model names a source agent carries for Claude Code. Every source agent's
# `model:` is one of these as of 2026-09-23; none names a model on another
# runtime, so an adapter drops them and the agent inherits the session model,
# unless runtime_settings.<runtime>.aliases in components.json maps the name
# (adapters/_components.py alias_for).
CLAUDE_MODEL_ALIASES = frozenset({"inherit", "default", "opus", "sonnet", "haiku", "fable", "opusplan"})


def is_claude_model(value: object) -> bool:
    """True for a Claude Code model alias or a claude-* model id."""
    text = str(value or "").strip().lower()
    return not text or text in CLAUDE_MODEL_ALIASES or text.startswith("claude-")


# ---------------------------------------------------------------------------
# Runtime output ownership and the sync.py --check / --prune reverse pass.
# This section implements the runtime output ownership design.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class OwnedDir:
    """One target directory an adapter owns, and what belongs in it.

    kind labels the reverse pass headings ("rule", "hook", "skill",
    "agent", "command", "prompt"). path is the absolute target directory.
    names is the set of relative names the current source produces here,
    computed by the same name mapping the adapter's own copy loop uses, so
    the reverse pass never reports a file sync() would itself write as an
    orphan. When per_item is True, names are immediate subdirectories
    (skills) rather than files, and the reverse pass treats each one as
    one unit instead of walking into it. exclude names a present entry
    that is never an orphan even though the current source does not
    produce it, such as a skill directory listed in
    skills/sync-exclude.json; the reverse pass reports it under
    "excluded" instead.
    """

    kind: str
    path: Path
    names: FrozenSet[str]
    per_item: bool = False
    exclude: FrozenSet[str] = frozenset()


def owned_dir_entries(base: Path, *, per_item: bool = False) -> Set[str]:
    """Names actually present under an owned directory, for the reverse pass.

    Mirrors _walk_configuration's own exclusions (__pycache__, .pyc/.pyo)
    so an interpreter cache is never reported as an orphan. Files are
    returned as paths relative to base; per_item reports only the
    immediate subdirectories, one name per unit, matching how a skill
    directory is owned as a whole rather than file by file.
    """
    if not base.is_dir():
        return set()
    if per_item:
        return {p.name for p in base.iterdir() if p.is_dir()}
    found: Set[str] = set()
    for root_path, _dirs, files in _walk_configuration(base):
        root = Path(root_path)
        for fname in files:
            found.add(str((root / fname).relative_to(base)))
    return found


def mirrored_names(
    src_dir: Path,
    *,
    exclude_patterns: Optional[List[str]] = None,
    file_glob: Optional[str] = None,
) -> FrozenSet[str]:
    """Relative names mirror_dir would place in the target for src_dir.

    Applies the same walk, exclusion and glob rules mirror_dir itself
    applies, so an adapter's owned_outputs never drifts from what its
    copy loop actually produces.
    """
    if not src_dir.is_dir():
        return frozenset()
    names: Set[str] = set()
    for root_path, _dirs, files in _walk_configuration(src_dir):
        root = Path(root_path)
        for fname in files:
            fpath = root / fname
            if exclude_patterns and any(fpath.match(pat) for pat in exclude_patterns):
                continue
            if file_glob and not fpath.match(file_glob):
                continue
            names.add(str(fpath.relative_to(src_dir)))
    return frozenset(names)


def source_hook_names(source: Path) -> Set[str]:
    """Basenames of the hook scripts the shared config ships.

    Matches every adapter's own definition of "ours" for a hook
    registration: a file directly under hooks/, suffix .sh, not a test.
    """
    hooks_dir = source / "hooks"
    if not hooks_dir.is_dir():
        return set()
    return {
        f.name
        for f in hooks_dir.iterdir()
        if f.is_file() and f.suffix == ".sh" and not f.name.endswith(".test.sh")
    }


_SCRIPT_EXTENSIONS = (".sh", ".py", ".js", ".cjs", ".mjs", ".ts")
_COMMAND_TOKEN = re.compile(r"""[^\s'"]+""")


def extract_script_paths(command: str) -> List[str]:
    """Every distinct script path a hook *command* names.

    Conservative by design: a token counts only when it contains a "/"
    and ends in a known script extension, so a bare flag or word (`-x`,
    `then`, `/bin/sh`) is never mistaken for the invoked script. $HOME and
    a leading ~/ are expanded to the real home directory so results are
    directly comparable to the filesystem. Order is preserved with
    duplicates removed, so a guard clause that tests and then runs the
    same path yields one entry.
    """
    home_dir = str(home())
    found: List[str] = []
    for match in _COMMAND_TOKEN.finditer(command):
        token = match.group(0).strip("'\";,()")
        if "/" not in token or not token.endswith(_SCRIPT_EXTENSIONS):
            continue
        if token.startswith("$HOME"):
            token = home_dir + token[len("$HOME") :]
        elif token.startswith("~/"):
            token = home_dir + token[1:]
        if token not in found:
            found.append(token)
    return found


_TEST_CLAUSE = re.compile(r"""\[\s+!?\s*-[xfe]\s+(['"]?)([^\s'"]+)\1""")


def is_existence_guarded(command: str, paths: List[str]) -> bool:
    """True when *command* tests one of *paths* for existence before use.

    A `[ -x '/path' ]` or `[ -f '/path' ]` clause guarding the very script
    a command later invokes is a deliberate, portable registration meant
    to be a no-op on a machine where that tool is not installed; it is
    never classified dangling regardless of whether the path resolves on
    this machine today. The negated spelling `[ ! -f '/path' ] || cmd
    '/path'` (run only when the script exists, rather than skip only when
    it exists) tests the same path and counts identically. A guard clause
    that tests some other path does not count.
    """
    home_dir = str(home())
    tested: Set[str] = set()
    for match in _TEST_CLAUSE.finditer(command):
        token = match.group(2)
        if token.startswith("$HOME"):
            token = home_dir + token[len("$HOME") :]
        elif token.startswith("~/"):
            token = home_dir + token[1:]
        tested.add(token)
    return bool(tested & set(paths))


# The runtime target roots sync.py manages, from the runtime registry. A
# hook command's script path is only ever "ours" to classify as mislocated
# or dangling when it falls under one of these; a path outside all of them
# names a tool this repo has no jurisdiction over, however its basename
# reads, per disposition 5 of the ownership record.
_MANAGED_RUNTIME_MARKERS: Optional[Tuple[str, ...]] = None


def _managed_runtime_markers() -> Tuple[str, ...]:
    global _MANAGED_RUNTIME_MARKERS
    if _MANAGED_RUNTIME_MARKERS is None:
        _MANAGED_RUNTIME_MARKERS = tuple(
            f"/{runtime.relative.strip('/')}/" for runtime in runtime_registry().values()
        )
    return _MANAGED_RUNTIME_MARKERS


def in_managed_runtime_tree(path: str) -> bool:
    """True when *path* falls under one of the managed runtime roots."""
    return any(marker in path for marker in _managed_runtime_markers())


def classify_registration(command: str, *, source_names: Set[str], hooks_dir: Path) -> str:
    """Classify one hook registration's command for the reverse pass.

    Returns "ok" (nothing to report), "mislocated" (names a source-shipped
    script from the wrong path; repointed on the next sync), "dangling"
    (a script this repo manages that exists nowhere; pruned by --prune),
    or "foreign" (left to the ordinary foreign-hook-group report).

    hooks_dir is this runtime's own owned hooks directory, e.g.
    target / "hooks". A command with no extractable script path, one that
    tests its own script's existence first, or one whose script lies
    outside every managed runtime root, is never dangling: it is either
    genuinely foreign or beyond what this mechanical classifier can safely
    judge on its own, so it reports as foreign instead.
    """
    paths = extract_script_paths(command)
    if not paths:
        return "foreign"
    for p in paths:
        name = Path(p).name
        if name in source_names and in_managed_runtime_tree(p):
            return "ok" if p == str(hooks_dir / name) else "mislocated"
    if is_existence_guarded(command, paths):
        return "foreign"
    in_jurisdiction = [p for p in paths if in_managed_runtime_tree(p)]
    if in_jurisdiction and all(not Path(p).exists() for p in in_jurisdiction):
        return "dangling"
    return "foreign"


def repoint_command(
    command: str, *, source_names: Set[str], hooks_prefix: str, hooks_dir: Path
) -> str:
    """Rewrite a preserved foreign group's command onto this runtime's own
    hooks directory when it names one of our shipped scripts from
    somewhere else in the managed runtime tree (the mislocated case).

    Only a path whose basename is source-shipped AND that lies under one
    of the five managed runtime roots is rewritten, so a source name that
    happens to appear in an unrelated absolute path is never touched.
    Everything else in the command string, including an existence guard,
    survives unchanged.
    """
    result = command
    home_dir = str(home())
    for p in extract_script_paths(command):
        name = Path(p).name
        if name not in source_names or not in_managed_runtime_tree(p):
            continue
        if p == str(hooks_dir / name):
            continue
        correct = f"{hooks_prefix.rstrip('/')}/{name}"
        result = result.replace(p, correct)
        if p.startswith(home_dir):
            result = result.replace("$HOME" + p[len(home_dir) :], correct)
    return result
