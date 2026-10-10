"""Project-level sync: projects-root/<name>/ -> <projects root>/active/<project>/.

Each project's own instruction file, rules, hooks, agents, commands, and
scripts are authored under projects-root/<name>/ and pushed into the checkout
in each runtime's project-scoped layout. This is the project-tier twin of the
global sync; same adapters, different roots.

Config is delivered to active projects only. A checkout under <projects root>/
inactive/, archived/, or external/ is skipped, so shelving a project by moving
its directory also stops config delivery to it. That is the intended policy.
A project named in projects-root/public-targets.json is a public repository
and is skipped wherever it sits (public_targets). The projects root itself
is stratarc.paths.projects_root(): $LLM_ROOT_PROJECTS_DIR, then projects_root
in stratarc.toml, then <home>/projects/active (see projects_dir).

Source layout, per project:
  projects-root/<name>/
    AGENTS.md            project instruction file, master
    rules/*.md           project rules (paths: frontmatter allowed)
    hooks/*.sh, lib/     project hook scripts
    hooks/hooks.json     project hook registration, Claude Code shape,
                         paths as $CLAUDE_PROJECT_DIR/.claude/hooks/
    agents/*.md
    commands/*.md
    scripts/*            project helper scripts, copied to .claude/scripts/
    skills/<name>/       project skills, copied to .claude/skills/<name>/

Target layout, per project checkout <projects>/active/<name>/:
  AGENTS.md, CLAUDE.md, CODEX.md, GEMINI.md
                          byte copies of the master, its rules digest rendered
                          as binding plus pointers (_surface_bodies)
  .claude/rules/ .claude/hooks/ .claude/agents/ .claude/commands/ .claude/scripts/
  .claude/skills/<name>/  each skill: cell opted in for the project column, and
                          each skill under projects-root/<name>/skills/ when
                          its project:skills row is on
  .claude/settings.json   hooks key replaced, everything else preserved
  .claude/<name>-delivered.json  the paths the last sync delivered, with the
                          sha256 of each file's delivered content (<name> is
                          the engine name, stratarc.paths.engine_name())
  .codex/hooks.json       Codex shape, paths rewritten to .codex/hooks/
  .codex/hooks/

Every delivered tree is pruned: a file at a path the engine could ship there
or recorded as delivered, that the stage no longer ships (its control-plane
cell was cleared, or its source was deleted or renamed), is removed with any
hook registration that runs it, so --check stays stale until the removal
lands. A path the engine never shipped is left alone, and so is a delivered
file whose content no longer matches the hash the manifest recorded for it:
the project edited it, so it is kept, noted on stderr, and reported by
--verify, never deleted.

Carried guards: every active project also receives hooks/no-attribution-guard.sh,
hooks/env-dump-guard.sh and the library they source (CARRIED_GUARDS,
CARRIED_GUARD_FILES) under .claude/hooks/, each registered in
.claude/settings.json with no matcher beside the project's own hooks, so a
clone or a container that has no ~/.claude/ still runs them. A carried copy
defers to an identical, registered user-level guard, so a session on this
machine logs one deny, not two (see each guard's header). core.hooksPath
is set to .githooks only where the project already tracks that directory.
--verify reports a carried file or registration that differs from source.

Server attribution check: a project whose
origin is on GitHub under github_owner() also receives
.github/workflows/attribution-check.yml (pull requests and pushes),
.github/workflows/attribution-check-comment.yml (comments) and
.github/attribution-check/attribution-check.py, rendered from the packaged
stratarc/data/ci/, as tracked files the project commits in its ordinary flow. The check reads
the detector the carried guard delivers. --verify reports them too.

Per-project renderers: a project may opt into a delivery the public engine
does not know by carrying a file under projects-root/<name>/. A renderer
extension lives at <source root>/scripts/private/ and registers itself with
register_renderer() (see load_renderer_extensions); a missing directory
registers nothing and every other delivery runs unchanged.

Usage:
  python3 -m stratarc.projects                    every project under projects-root
  python3 -m stratarc.projects --only my-project
  python3 -m stratarc.projects --check            dry run, exit 1 if stale
  python3 -m stratarc.projects --verify           manifest, surface, carried guard and project skill drift
  python3 -m stratarc.projects --adopt <name>     pull an existing checkout's config
                                                 INTO projects-root (first-time capture)
"""

from __future__ import annotations

import argparse
import atexit
from collections.abc import Callable, Collection
import contextlib
import functools
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from importlib import util as importlib_util
from importlib.resources import as_file

from stratarc import paths
from stratarc.adapters import _components
from stratarc.control_plane import ControlPlane, _cells, _option_id
from stratarc.messages import CliError
from stratarc.paths import home
from stratarc.reconcile import main as reconcile_main
from stratarc.public_targets import PublicTargetsUnreadable, is_public_checkout, load_public_targets
from stratarc.resources import data_dir
from stratarc.rules_digest import DIGEST_BEGIN, digest_or_note, embed_digest
from stratarc.staging import PROJECT_DIRECTORIES, build_stage, cleanup_all

# The source root: --root when given (configure_root), else STRATARC_SOURCE,
# else the nearest stratarc.toml at or above the current directory (the order
# stratarc.paths.source_root resolves it). main() always calls configure_root,
# so the values below are only the import-time defaults for a caller that
# uses the helpers directly.
SOURCE_VARIABLE = paths.SOURCE_VARIABLE


def source_root(flag: str | None = None, default: pathlib.Path | None = None) -> pathlib.Path:
    """The source root to deliver from: --root, then STRATARC_SOURCE, then stratarc.toml discovery, then *default*."""
    if flag:
        return paths.source_root(pathlib.Path(flag))
    if not os.environ.get(SOURCE_VARIABLE, "").strip() and default is not None:
        return default
    try:
        return paths.source_root()
    except OSError:
        return pathlib.Path(".").resolve()


_ROOT_CONFIGURED = False
ROOT = source_root()
PROJECTS_SRC = ROOT / "projects-root"
RETIRED_RULES = ROOT / "rules" / "retired.json"
RETIRED_HOOKS = ROOT / "hooks" / "retired.json"
# Projects that are public repositories receiving nothing from this one; the
# same file the reconciler reads, resolved under PROJECTS_SRC, through
# stratarc.public_targets.
PUBLIC_TARGETS = "public-targets.json"
# The name the engine puts on what it records in a checkout.
PROG = "projects"


def delivered_manifest() -> pathlib.Path:
    """The checkout-relative file that records what the last sync delivered: .claude/<engine name>-delivered.json."""
    return pathlib.Path(f".claude/{paths.engine_name(ROOT)}-delivered.json")


def projects_dir() -> pathlib.Path:
    """The directory that holds the status folders (active, inactive, ...) the project checkouts sit in: stratarc.paths.projects_dir for this run's root."""
    return paths.projects_dir(root=ROOT)


def _home() -> pathlib.Path:
    return home()


def configure_root(root: pathlib.Path) -> None:
    """Point the module at *root* (the --root flag), rebinding every path derived from ROOT."""
    global ROOT, PROJECTS_SRC, RETIRED_RULES, RETIRED_HOOKS, _ROOT_CONFIGURED
    _ROOT_CONFIGURED = True
    ROOT = pathlib.Path(root).expanduser().resolve()
    PROJECTS_SRC = ROOT / "projects-root"
    RETIRED_RULES = ROOT / "rules" / "retired.json"
    RETIRED_HOOKS = ROOT / "hooks" / "retired.json"


def public_targets() -> frozenset[str]:
    """The entries of projects-root/public-targets.json: project names and owner/repo slugs.

    A public target is a repository extracted from this one (stratarc first).
    It must receive nothing from this private tree, so resolve() refuses it
    as a sync target and verify() does not report its checkout as
    unregistered. A missing file declares nothing. One that exists and
    cannot be read raises, before any delivery: reading it as empty would
    deliver into the one checkout the file exists to protect.
    """
    try:
        return load_public_targets(PROJECTS_SRC.parent)
    except PublicTargetsUnreadable as error:
        raise RuntimeError(str(error)) from None


def _public_checkout(path: pathlib.Path) -> bool:
    """True when the checkout at *path* is a public target by its main
    worktree's name or its origin slug, whatever the directory is called."""
    return is_public_checkout(path, public_targets())


def _git_env() -> dict[str, str]:
    """Environment for a git subprocess call, with repository-location
    variables removed. See the matching helper in stratarc.reconcile:
    a git hook (this module can run from `post-commit`) inherits GIT_DIR and
    friends from the commit that triggered it, which makes `-C <path>` read
    the wrong repository unless those variables are stripped first.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def canonical_checkout() -> pathlib.Path:
    """Return the primary checkout for this repository, resolved via git.

    A source root that is a linked worktree is the worktree path, not the
    canonical checkout. `git rev-parse --git-common-dir` always names the
    shared `.git` directory, which sits inside the primary checkout even when
    the source root is a linked worktree, so its parent is the primary
    checkout. Falls back to ROOT when git cannot answer, which keeps a plain
    (non-worktree) checkout behaving exactly as before.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ROOT
    if result.returncode != 0:
        return ROOT
    common_dir = pathlib.Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = ROOT / common_dir
    return common_dir.resolve().parent


def _is_source_tree(path: pathlib.Path) -> bool:
    """True when *path* is itself an engine source tree.

    Identified by content, so the answer holds when git cannot name the
    canonical checkout and when the running copy is a second clone. A source
    tree carries stratarc.toml, or a control-plane.md beside a projects-root/
    directory.
    """
    if (path / paths.CONFIG_NAME).is_file():
        return True
    return (path / "control-plane.md").is_file() and (path / "projects-root").is_dir()


def _refuse_canonical(
    path: pathlib.Path, name: str
) -> tuple[pathlib.Path | None, str]:
    """Refuse to hand back a source-tree checkout as a sync target.

    Excluding the canonical checkout from discovery (the reconciler) stops it
    being registered as an ordinary project, but a stale manifest row from
    before that fix would still route writes here if this were not also
    checked at the point a target is resolved.

    The path comparison alone is not enough: canonical_checkout() falls back
    to ROOT when git cannot answer, and from a linked worktree or a second
    clone ROOT is not the checkout being written into. A post-commit sync from
    a worktree branch once wrote the instruction surfaces into the canonical
    checkout, where every later session there loaded their superseded digest.
    A target that is itself a source tree is therefore refused by content as
    well.
    """
    canonical = canonical_checkout()
    if path.resolve() in (ROOT.resolve(), canonical.resolve()):
        return (
            None,
            f"skip: {name} resolves to the canonical {paths.engine_name(ROOT)} checkout, config is not delivered there",
        )
    if _is_source_tree(path):
        return (
            None,
            f"skip: {name} is a {paths.engine_name(ROOT)} source tree, config is not delivered there",
        )
    # The name was checked in resolve(); the checkout itself may still be a
    # public repository under another name (a worktree, a renamed clone).
    if _public_checkout(path):
        return (
            None,
            f"skip: {name} at {short(path)} is a checkout of a public repository, config is not delivered there",
        )
    return path, ""


def resolve(name: str, cp) -> tuple[pathlib.Path | None, str]:
    """Locate a project's checkout, or explain why config is not delivered.

    The manifest in control-plane.md is authoritative. `status` decides whether
    a project receives config; the directory tree only says where the files
    are. That separation is deliberate: a folder can be reorganised for human
    convenience without changing what any runtime is given.

    Returns (path, reason). Path is None whenever reason is non-empty.
    """
    if name in public_targets():
        return None, f"skip: {name} is a public repository, config is not delivered there"
    projects = projects_dir()
    if cp is None:
        hits = [
            p
            for p in (projects.glob(f"*/{name}"), projects.glob(f"*/*/{name}"))
            for p in p
            if p.is_dir() and (p / ".git").exists()
        ]
        if len(hits) != 1:
            return None, f"skip: {name} not resolvable without a control plane"
        return _refuse_canonical(hits[0], name)

    if name not in cp.manifest:
        return None, f"skip: {name} is not in the control-plane manifest"

    status = cp.status(name)
    if status != "active":
        return None, f"skip: {name} is {status}, only active projects receive config"

    hits = cp.find_checkout(name, projects)
    if not hits:
        return (
            None,
            f"skip: {name} is active but has no checkout under {short(projects)}",
        )
    if len(hits) > 1:
        found = ", ".join(short(h) for h in hits)
        return None, f"skip: {name} is ambiguous, {len(hits)} checkouts: {found}"
    return _refuse_canonical(hits[0], name)


def verify(cp) -> list[str]:
    """Report disagreement between the manifest and the directory tree.

    Neither side is corrected automatically. The manifest governs policy and
    the tree is where a human put things; when they diverge, that is a fact to
    show, not a conflict for a script to settle.
    """
    out: list[str] = []
    projects = projects_dir()
    canonical = canonical_checkout()
    conventional = {
        "active": "active",
        "new": "new",
        "inactive": "inactive",
        "archived": "archived",
        "external": "external",
    }
    for name in cp.manifest:
        status = cp.status(name)
        if status == "unmanaged":
            # F-36: an unmanaged project has no .git, so find_checkout (which
            # requires one) never finds it. Its existence is checked directly
            # instead of reporting every such row as a missing checkout.
            if not (projects / "active" / name).is_dir():
                out.append(f"{name}: listed as unmanaged, directory not found")
            continue
        hits = cp.find_checkout(name, projects)
        if not hits:
            if (PROJECTS_SRC / name).is_dir():
                continue
            out.append(f"{name}: listed as {status}, no checkout found")
            continue
        if len(hits) > 1:
            out.append(
                f"{name}: {len(hits)} checkouts, {', '.join(short(h) for h in hits)}"
            )
            continue
        want = conventional.get(status)
        actual = hits[0].relative_to(projects).parts[0]
        if want and actual != want:
            out.append(f"{name}: listed as {status} but sits under {actual}/")
    # checkouts on disk that no manifest row claims, and nested repositories.
    # A public target has no row by design, so its checkout is not drift.
    listed = set(cp.manifest) | public_targets()
    for pattern in ("*/*", "*/*/*", "*/*/*/*"):
        for p in sorted(projects.glob(pattern)):
            rel = p.relative_to(projects)
            # skip trash, worktree pools, and any other dot or underscore tree
            if any(part.startswith((".", "_")) for part in rel.parts):
                continue
            if not p.is_dir() or not (p / ".git").exists():
                continue
            if p.resolve() in (ROOT.resolve(), canonical.resolve()):
                continue
            if rel.parts[0] == "active" and len(rel.parts) == 2:
                if rel.parts[1].endswith("-trust"):
                    continue  # legacy active tier directory
            nested_under = _enclosing_repo(p)
            if nested_under is not None:
                continue
            if p.name not in listed and not _public_checkout(p):
                out.append(f"{p.name}: checkout at {short(p)} is not in the manifest")
    return out


def surface_drift(cp) -> tuple[list[str], list[str]]:
    """Compare each active project's opted-in instruction surfaces to its master.

    Mirrors the source sync_project would write from: the staged view when the
    control plane carries option rows, the raw projects-root/<name>/ otherwise.
    Returns (divergent, shims). divergent lines name a surface that differs
    from the master or is missing from the checkout entirely; shims name a
    surface the project has opted out of by leaving it off surfaces.json's
    "copy" list, so it is hand-authored and is not compared.
    """
    divergent: list[str] = []
    shims: list[str] = []
    for name in cp.manifest:
        if cp.status(name) != "active":
            continue
        dst, why = resolve(name, cp)
        if dst is None:
            continue
        if cp.rows:
            src, _notes = build_stage(ROOT, cp, name)
        else:
            src = PROJECTS_SRC / name
        body, wanted, shim_surfaces = _surface_plan(src)
        for s in shim_surfaces:
            shims.append(f"{name}: {s} is a shim, not compared")
        if body is None:
            continue
        bodies = _surface_bodies(_embed_project_digest(body, name), name, src)
        # Compare only the surfaces sync_project can write. A name in the copy
        # list that is not a known surface is ignored by the sync, so it is
        # never drift.
        for s in SURFACES:
            if s not in wanted:
                continue
            target = dst / s
            if not target.is_file():
                divergent.append(f"{name}: {s} is missing")
            elif target.read_bytes() != bodies[s]:
                divergent.append(f"{name}: {s} diverges from the master")
    return divergent, shims


def _enclosing_repo(path: pathlib.Path) -> pathlib.Path | None:
    """The nearest ancestor that is itself a git repository, if any.

    Stops at the projects directory so a repository placed directly under a
    status folder is never reported as nested.
    """
    projects = projects_dir()
    for parent in path.parents:
        if parent == projects or parent == parent.parent:
            return None
        if (parent / ".git").exists():
            return parent
    return None


def retired_rule_names() -> set[str]:
    """Rule file names the source root once shipped and has since renamed or removed.

    The prune in sync_project only removes a checkout file whose name the engine
    knows, so a rule renamed or deleted at the source would otherwise survive
    in every checkout as a stale duplicate of its successor. rules/retired.json
    keeps those names known. Append a name there whenever a rule file is
    renamed or removed, and never take a name off the list.
    """
    if not RETIRED_RULES.is_file():
        return set()
    data = json.loads(RETIRED_RULES.read_text())
    return {str(n) for n in data.get("retired", [])}


def retired_hook_names() -> set[str]:
    """Hook file names the source root once shipped under hooks/ and has since
    renamed or removed, relative to hooks/ (e.g. "lib/git-event.sh").

    The generic prune in sync_project only removes a checkout file whose
    name the engine knows: with a hashed delivered-manifest, that means
    exactly the paths recorded under this checkout's .claude/hooks/ (or
    .codex/hooks/) prefix. A hook delivered before that prefix was ever
    recorded there (an old delivery, or a project that never opted back in
    since) leaves no such record, so a rename or deletion at the source
    would otherwise survive forever as a stale duplicate, still runnable
    and still registered, pointed at a library path that no longer exists.
    hooks/retired.json keeps those names known regardless of what the manifest
    recorded. Append a name here whenever a hook file is renamed or
    removed, and never take a name off the list.
    """
    if not RETIRED_HOOKS.is_file():
        return set()
    data = json.loads(RETIRED_HOOKS.read_text())
    return {str(n) for n in data.get("retired", [])}


SURFACES = ("AGENTS.md", "CLAUDE.md", "CODEX.md", "GEMINI.md")
# The project directories delivered under .claude/, each selected by its
# project:<dir> row, defined once in staging and shared with the reconciler.
# The tuple includes "scripts": a project's own helper scripts under
# projects-root/<name>/scripts/ are delivered to <checkout>/.claude/scripts/.
# It never refers to the engine's own scripts directory, which is not read.
CLAUDE_DIRS = PROJECT_DIRECTORIES
# Where a project's staged skills land, each as <target><skill>/. Claude Code
# reads .claude/skills/; Codex and Gemini CLI read .agents/skills/ and not
# .claude/skills/; Cursor and OpenCode read both. Both trees carry the same
# files.
SKILL_TARGETS = (".claude/skills/", ".agents/skills/")
GENERATED_PARTS = {"__pycache__", ".pytest_cache"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}

# Each carried guard and every library file it reads at run time, as paths
# under hooks/. Carried into every active project's tracked .claude/hooks/ at
# the same relative paths. Both guards back global-tier rules:
# no-attribution-guard.sh (no-agent-attribution) and env-dump-guard.sh
# (no-secret-exposure).
CARRIED_GUARDS = {
    "no-attribution-guard.sh": (
        "no-attribution-guard.sh",
        "lib/log.sh",
        "lib/guard-utils.sh",
        "lib/guard-log.sh",
        "lib/attribution-detect.py",
    ),
    "env-dump-guard.sh": (
        "env-dump-guard.sh",
        "lib/log.sh",
        "lib/guard-utils.sh",
        "lib/guard-log.sh",
        "lib/env-dump-detect.py",
    ),
}
# Every carried path once, in first-seen order.
CARRIED_GUARD_FILES = tuple(dict.fromkeys(rel for files in CARRIED_GUARDS.values() for rel in files))
# Files a carried guard reads when present and runs without when absent:
# carried when the source root's own hooks/ has them, never required, and never
# part of the packaged data. A source root without hooks/lib/private/ is
# supported, so a missing one must not skip the carry. The pattern file keeps
# the carried env-dump detector's platform patterns identical to the source
# root's detector.
CARRIED_GUARD_OPTIONAL_FILES = ("lib/private/env-dump-patterns.json",)
# Every path the carry can write, for the shadow, skip and prune checks.
CARRIED_GUARD_ALL_FILES = CARRIED_GUARD_FILES + CARRIED_GUARD_OPTIONAL_FILES


def _carried_guard_group(guard: str) -> dict:
    # No matcher: the guard runs on every tool call, as the user-level
    # registration in hooks/hooks.json does.
    command = f"$CLAUDE_PROJECT_DIR/.claude/hooks/{guard}"
    return {"hooks": [{"type": "command", "command": command, "timeout": 3}]}


CARRIED_GUARD_GROUPS = tuple(_carried_guard_group(guard) for guard in CARRIED_GUARDS)
TRACKED_GIT_HOOKS = ".githooks"

# The server-side attribution check, rendered into every active project whose
# origin is on GitHub under the managed owner. The check reads its detector
# from the carried guard's .claude/hooks/lib/, so only the workflow and the
# script travel. Target path -> file name under the packaged stratarc/data/ci/.
#
# The account is configuration, resolved by github_owner() at call time: the
# owner every slug in projects-root/remotes.json shares, else
# stratarc.paths.github_owner() ($STRATARC_GITHUB_OWNER, then `owner` in
# stratarc.toml, else empty, which delivers nothing).
GITHUB_OWNER_VARIABLE = paths.OWNER_VARIABLE
REMOTES = "remotes.json"
CI_DATA = "ci"
CHECK_SCRIPT_SOURCE = "attribution-check.py"
CHECK_SCRIPT_TARGET = ".github/attribution-check/attribution-check.py"
# Two workflows, so a comment run never reports the pull request check name.
CHECK_WORKFLOW_TARGETS = (
    ".github/workflows/attribution-check.yml",
    ".github/workflows/attribution-check-comment.yml",
)
RENDERED_CHECK_FILES = {
    CHECK_WORKFLOW_TARGETS[0]: "attribution-check.yml",
    CHECK_WORKFLOW_TARGETS[1]: "attribution-check-comment.yml",
    CHECK_SCRIPT_TARGET: CHECK_SCRIPT_SOURCE,
}

# Packaged data is opened once per process; the stack is closed at exit so a
# resource that had to be extracted (a zipped install) is cleaned up.
_RESOURCES = contextlib.ExitStack()
atexit.register(_RESOURCES.close)


@functools.cache
def _packaged(name: str) -> pathlib.Path:
    """The packaged stratarc/data/<name> directory as a real path."""
    return pathlib.Path(_RESOURCES.enter_context(as_file(data_dir(name))))


def _hook_dirs() -> tuple[pathlib.Path, ...]:
    """The hook directories a carried guard is read from, packaged first; the source root's own hooks/ overlays it file by file."""
    return (_packaged("hooks"), ROOT / "hooks")


def carried_source(rel: str) -> pathlib.Path | None:
    """The file a carried path *rel* is copied from, or None when no hook directory holds it.

    The source root's hooks/ wins over the packaged copy. An optional file is
    read from the source root only: the packaged data never ships one.
    """
    if rel in CARRIED_GUARD_OPTIONAL_FILES:
        candidate = ROOT / "hooks" / rel
        return candidate if candidate.is_file() else None
    for directory in reversed(_hook_dirs()):
        candidate = directory / rel
        if candidate.is_file():
            return candidate
    return None


def _is_carried_guard_hook(hook) -> bool:
    command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
    return command.startswith("$CLAUDE_PROJECT_DIR") and any(
        command.endswith(f"/.claude/hooks/{guard}") for guard in CARRIED_GUARDS
    )


def _with_carried_guards(hooks: dict) -> dict:
    """*hooks* with exactly one group per carried guard first under PreToolUse.

    Any earlier carried guard hook is removed from whatever group holds it
    before the current groups are added, so a stale registration is replaced
    and repeated runs are byte-stable. A group is dropped only when carried
    hooks were all it held; every other hook and group, the project's own
    included, is kept as it was.
    """
    merged = json.loads(json.dumps(hooks)) if hooks else {}
    pre = []
    for group in merged.get("PreToolUse", []):
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            pre.append(group)
            continue
        kept = [h for h in group["hooks"] if not _is_carried_guard_hook(h)]
        if len(kept) == len(group["hooks"]):
            pre.append(group)
        elif kept:
            pre.append({**group, "hooks": kept})
    merged["PreToolUse"] = [json.loads(json.dumps(group)) for group in CARRIED_GUARD_GROUPS] + pre
    return merged


def _carry_guard(dst: pathlib.Path, dry: bool, acts: list[str]) -> None:
    """Copy the guard and its library into dst/.claude/hooks/.

    The source is the packaged guard overlaid by the source root's hooks/,
    never the staged view: no-agent-attribution is global tier, so no
    control-plane cell opts a project out of it. copy2 keeps the executable
    bit, which the registered command needs.
    """
    missing = [rel for rel in CARRIED_GUARD_FILES if carried_source(rel) is None]
    if missing:
        acts.append(f"skip carry: {', '.join(missing)} missing under {short(ROOT / 'hooks')} and the packaged hooks")
        return
    for rel in CARRIED_GUARD_OPTIONAL_FILES:
        # The source dropped an optional file: a copy carried earlier would
        # keep deciding with stale patterns, and the pruner skips carried
        # paths, so the carry removes it.
        stale = dst / ".claude" / "hooks" / rel
        if carried_source(rel) is None and stale.is_file():
            acts.append(f"remove {short(stale)}")
            if not dry:
                stale.unlink()
                try:
                    stale.parent.rmdir()
                except OSError:
                    pass
    for rel in CARRIED_GUARD_FILES + tuple(r for r in CARRIED_GUARD_OPTIONAL_FILES if carried_source(r) is not None):
        src = carried_source(rel)
        out = dst / ".claude" / "hooks" / rel
        if out.is_file() and out.read_bytes() == src.read_bytes() and os.access(out, os.X_OK) == os.access(src, os.X_OK):
            continue
        acts.append(f"write {short(out)}")
        if not dry:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)


def _git_out(dst: pathlib.Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(dst), *args],
            capture_output=True,
            text=True,
            timeout=10,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout.strip()


# Prefixes of a delivered path's manifest key that PROJECT_DIRECTORIES and
# SKILL_TARGETS deliver into: the directories a project tracks and commits,
# as opposed to .codex/, .gemini/, opencode.jsonc and similar, which this
# check does not cover.
_GITIGNORE_CHECKED_PREFIXES = tuple(f".claude/{d}/" for d in PROJECT_DIRECTORIES) + SKILL_TARGETS


def _project_gitignored_paths(dst: pathlib.Path, paths: set[str]) -> set[str]:
    """The subset of *paths* (checkout-relative, forward-slash) dst's own
    .gitignore or .git/info/exclude excludes, checked in one git call via
    --stdin so N delivered paths cost one process, not N. Global excludes
    are disabled (core.excludesFile=/dev/null): only the project's own
    ignore rules are relevant here, never this machine's. A git failure
    (no repository, no git on PATH) reports no matches rather than raising,
    the same fail-quiet posture every other optional git probe in this
    module takes.
    """
    if not paths:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(dst), "-c", "core.excludesFile=/dev/null", "check-ignore", "-z", "--stdin"],
            input="\0".join(sorted(paths)) + "\0",
            capture_output=True,
            text=True,
            timeout=10,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    # check-ignore exits 1 when none of the paths given matched, which is
    # the ordinary "nothing to warn about" outcome here, not a failure; a
    # git or usage error (2) or worse is what actually means "could not
    # check", so only stdout is read and only for 0 or 1.
    if result.returncode not in (0, 1):
        return set()
    return {p for p in result.stdout.split("\0") if p}


def _warn_on_gitignored_delivered_paths(name: str, dst: pathlib.Path, delivered: dict[str, str]) -> None:
    """Note on stderr every path this run delivered under a tracked project
    directory that the project's own .gitignore excludes: a
    plain `git add -A` in that project silently drops it, so the engine
    writes it to disk successfully but it never reaches a commit, and a
    fresh clone or CI is missing a file a delivered guard script may source.
    Caught here, at delivery time, rather than discovered later as a
    missing file after a clone.
    """
    checked = {p for p in delivered if p.startswith(_GITIGNORE_CHECKED_PREFIXES)}
    for path in sorted(_project_gitignored_paths(dst, checked)):
        _note(f"{name}: {path} is delivered but {short(dst)}'s .gitignore excludes it; it will not reach a commit")


def _activate_tracked_git_hooks(dst: pathlib.Path, dry: bool, acts: list[str]) -> str | None:
    """Point core.hooksPath at the project's tracked .githooks/.

    Only a project that already tracks that directory is touched. The
    effective value is read across every scope, so a core.hooksPath the
    user set to anything else, locally or globally, is left as it is, and
    that is a note on stderr rather than a pending action (an action would
    keep --check stale forever). A checkout git cannot read is left alone too.
    """
    code, tracked = _git_out(dst, "ls-files", "--", TRACKED_GIT_HOOKS)
    if code != 0 or not tracked:
        return None
    _code, current = _git_out(dst, "config", "--get", "core.hooksPath")
    if current == TRACKED_GIT_HOOKS:
        return None
    if current:
        print(
            f"{PROG}: note: {short(dst)} keeps core.hooksPath {current}, not {TRACKED_GIT_HOOKS}",
            file=sys.stderr,
        )
        return None
    acts.append(f"git config core.hooksPath {TRACKED_GIT_HOOKS} in {short(dst)}")
    if not dry:
        _git_out(dst, "config", "--local", "core.hooksPath", TRACKED_GIT_HOOKS)
    # Returned so a later step can predict this value under --check, where it
    # has been reported as a pending action but not written to the config yet.
    return TRACKED_GIT_HOOKS


# Renderer state is its own manifest field rather than an entry in the generic
# "paths"/"hashes" map: those name real checkout-relative files, and a JSON
# sub-object inside a config file is not a file of its own. The same
# separation _read_managed_components uses for project_plugins and
# project_mcp_servers.


def hash_provider_entry(entry: dict | None) -> str:
    """A stable digest of a config block's content, for ownership tracking
    the same way _sha256 hashes a delivered file's bytes. Renderer extensions
    use it to record the state they return."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()


def provider_locally_edited(entry: dict | None, recorded: str | None) -> bool:
    """True when a delivered config block no longer matches the hash it
    was recorded with, the same shape as _locally_edited for a whole file."""
    return recorded is not None and hash_provider_entry(entry) != recorded


# A per-project renderer is a callable (name, dst, dry, acts, recorded) -> state.
# It delivers something a project opts into by carrying a file under
# projects-root/<name>/ that the public engine does not know. *recorded* is the
# state it returned on the previous successful sync (a string, or None the
# first time); the state it returns is stored under that renderer's key in the
# delivered manifest's "renderers" map and handed back next time. Returning
# *recorded* unchanged means "nothing new resolved, retry later".
Renderer = Callable[[str, pathlib.Path, bool, list[str], str | None], str | None]
RENDERERS: dict[str, Renderer] = {}
RENDERER_EXTENSION = "scripts/private/project_renderers.py"
RENDERERS_FIELD = "renderers"


def register_renderer(key: str, render: Renderer) -> None:
    """Add *render* under *key*; a later registration under the same key replaces the earlier one."""
    RENDERERS[key] = render


def load_renderer_extensions(root: pathlib.Path | None = None) -> list[str]:
    """Load <source root>/scripts/private/project_renderers.py and let it register renderers.

    The module is loaded by path, so nothing is imported by name and an
    installed package never needs the extension on sys.path. It must define
    `register(register_renderer)`. A missing file is no extension and returns
    an empty list; a file that fails to load or has no `register` is reported
    on stderr and skipped, so one broken extension never stops the sync.
    Returns the keys registered by this load.
    """
    path = (root or ROOT) / RENDERER_EXTENSION
    if not path.is_file():
        return []
    before = set(RENDERERS)
    try:
        spec = importlib_util.spec_from_file_location("stratarc_private_project_renderers", path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot build a module spec")
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        register = module.register
    except (OSError, ImportError, SyntaxError, AttributeError) as error:
        print(f"{PROG}: note: renderer extension {short(path)} not loaded: {error}", file=sys.stderr)
        return []
    register(register_renderer)
    return sorted(set(RENDERERS) - before)


def _run_renderers(
    name: str, dst: pathlib.Path, dry: bool, acts: list[str], recorded: dict[str, str]
) -> dict[str, str]:
    """Run every registered renderer; the returned map is the state to persist (a key with no state is dropped)."""
    state: dict[str, str] = {}
    for key in sorted(RENDERERS):
        value = RENDERERS[key](name, dst, dry, acts, recorded.get(key))
        if isinstance(value, str):
            state[key] = value
    return state


def _read_renderer_state(path: pathlib.Path) -> dict[str, str]:
    """The renderer state the previous successful sync recorded in *path*, or an empty map."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    value = document.get(RENDERERS_FIELD) if isinstance(document, dict) else None
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def _relative_to(path: pathlib.Path, dst: pathlib.Path) -> str:
    """The manifest key for a delivered path, or its absolute form when it
    lands outside the checkout (an absolute core.hooksPath)."""
    try:
        return path.relative_to(dst).as_posix()
    except ValueError:
        return path.as_posix()


_GITHUB_ORIGIN = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https?://(?:[^@/]+@)?github\.com/)"
    r"([^/]+)/[^/]+?(?:\.git)?/?$",
    re.IGNORECASE,
)


def _remotes_owner() -> str | None:
    """The one GitHub account every slug in projects-root/remotes.json names,
    or None when the file is absent, unreadable, empty, or names more than
    one account. The file is a flat {directory: "owner/repo"} map
    (the validator's remotes check), so the owner it carries is
    the prefix its values share."""
    path = PROJECTS_SRC / REMOTES
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    owners = {
        value.split("/", 1)[0]
        for value in document.values()
        if isinstance(value, str) and "/" in value and value.split("/", 1)[0]
    }
    return owners.pop() if len(owners) == 1 else None


def github_owner() -> str:
    """The GitHub account whose repositories receive the rendered check: the owner projects-root/remotes.json names, else stratarc.paths.github_owner(), which is empty when none is configured."""
    owner = _remotes_owner()
    if owner:
        return owner
    return paths.github_owner(ROOT)


def _origin_is_owned(dst: pathlib.Path) -> bool:
    """True when dst's origin is a GitHub repository under github_owner(). No owner configured means nothing is owned."""
    owner = github_owner()
    if not owner:
        return False
    code, url = _git_out(dst, "remote", "get-url", "origin")
    if code != 0 or not url:
        return False
    match = _GITHUB_ORIGIN.match(url.strip())
    return bool(match) and match.group(1).lower() == owner.lower()


def _rendered_check(target: str) -> bytes | None:
    """The bytes a project carries at *target*, or None when the source is
    missing. The packaged workflows already name the script at its project
    path (CHECK_SCRIPT_TARGET), so the bytes are rendered as packaged."""
    source = _packaged(CI_DATA) / RENDERED_CHECK_FILES[target]
    if not source.is_file():
        return None
    return source.read_bytes()


def _render_attribution_check(dst: pathlib.Path, dry: bool, acts: list[str]) -> None:
    """Render the server check into an owned GitHub project."""
    if not _origin_is_owned(dst):
        return
    for target in RENDERED_CHECK_FILES:
        data = _rendered_check(target)
        if data is None:
            acts.append(f"skip render: {RENDERED_CHECK_FILES[target]} missing from the packaged {CI_DATA} data")
            continue
        _write_if_changed(dst / target, data, dry, acts)


def rendered_check_drift(name: str, dst: pathlib.Path) -> list[str]:
    """The rendered check files in dst that are missing or differ, for --verify."""
    if not _origin_is_owned(dst):
        return []
    out = []
    for target in RENDERED_CHECK_FILES:
        want = _rendered_check(target)
        path = dst / target
        if not path.is_file():
            out.append(f"{name}: {target} is missing")
        elif want is not None and path.read_bytes() != want:
            out.append(f"{name}: {target} differs from source")
    return out


def carried_guard_drift(cp) -> list[str]:
    """Each active project's carried guard files and registration that do not
    match source, for --verify."""
    out: list[str] = []
    for name in cp.manifest:
        if cp.status(name) != "active":
            continue
        dst, _why = resolve(name, cp)
        if dst is None:
            continue
        for rel in CARRIED_GUARD_ALL_FILES:
            carried = dst / ".claude" / "hooks" / rel
            label = f".claude/hooks/{rel}"
            source = carried_source(rel)
            if rel in CARRIED_GUARD_OPTIONAL_FILES and source is None:
                # Absent from source: nothing to carry. A carried copy left
                # from an earlier delivery is stale (the next carry removes
                # it); both absent is silent.
                if carried.is_file():
                    out.append(f"{name}: {label} is stale: source no longer carries {rel}")
                continue
            if not carried.is_file():
                out.append(f"{name}: {label} is missing")
            elif source is not None and carried.read_bytes() != source.read_bytes():
                out.append(f"{name}: {label} differs from source")
            elif source is not None and os.access(source, os.X_OK) and not os.access(carried, os.X_OK):
                out.append(f"{name}: {label} is not executable")
        settings_path = dst / ".claude" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
        except (OSError, ValueError):
            settings = {}
        hooks = settings.get("hooks") if isinstance(settings, dict) else None
        pre = hooks.get("PreToolUse", []) if isinstance(hooks, dict) else []
        for guard, group in zip(CARRIED_GUARDS, CARRIED_GUARD_GROUPS):
            if group not in pre:
                out.append(f"{name}: .claude/settings.json does not register the carried {guard}")
        out.extend(rendered_check_drift(name, dst))
    return out


def skill_drift(cp) -> list[str]:
    """Compare each active project's skill trees (SKILL_TARGETS) with its
    stage, for --verify. Returns (drift, notes).

    drift is what a sync would change: a staged file that is missing or
    changed from source, and an extra file the stage does not ship at a path
    the prune removes (the engine delivered it, or, before a hashed manifest,
    could have). notes is what no sync changes and so never fails --verify:
    a formerly delivered file kept because the project edited it, and a file
    the project added inside a delivered skill directory. A skill directory
    the engine never delivered is the project's own and is not compared.
    """
    drift: list[str] = []
    notes: list[str] = []
    for name in cp.manifest:
        if cp.status(name) != "active":
            continue
        dst, _why = resolve(name, cp)
        if dst is None:
            continue
        src = build_stage(ROOT, cp, name)[0] if cp.rows else PROJECTS_SRC / name
        previous, hashed = _read_delivered(dst / delivered_manifest())
        skills_src, _excluded, staged, claude_known = _skill_plan(
            name, src, _under(previous, SKILL_TARGETS[0]), hashed,
            cp.enabled(name, "project:skills"),
        )
        for prefix in SKILL_TARGETS:
            recorded = _under(previous, prefix)
            known = claude_known if prefix == SKILL_TARGETS[0] else set(recorded)
            _compare_skills(name, prefix, dst / prefix, skills_src, staged, known, recorded, drift, notes)
    return drift, notes


def _compare_skills(
    name: str,
    prefix: str,
    target: pathlib.Path,
    skills_src: pathlib.Path,
    staged: set[str],
    known: set[str],
    recorded: dict[str, str | None],
    drift: list[str],
    notes: list[str],
) -> None:
    """One skill target tree of skill_drift, appended to *drift* and *notes*."""
    for rel in sorted(staged):
        path = target / rel
        if not path.is_file():
            drift.append(f"{name}: {prefix}{rel} is missing")
        elif rel not in recorded:
            notes.append(f"{name}: {prefix}{rel} is a protected project collision, kept")
        elif path.read_bytes() != (skills_src / rel).read_bytes():
            drift.append(f"{name}: {prefix}{rel} is changed from source")
    staged_dirs = {rel.split("/", 1)[0] for rel in staged}
    for rel in sorted(_files_under(target) - staged):
        if _locally_edited(target / rel, recorded.get(rel)):
            notes.append(f"{name}: {prefix}{rel} is locally edited since delivery, kept")
        elif rel in known:
            drift.append(f"{name}: {prefix}{rel} is extra, not in source")
        elif rel.split("/", 1)[0] in staged_dirs:
            notes.append(f"{name}: {prefix}{rel} is the project's own, inside a delivered skill")


def short(p: pathlib.Path) -> str:
    return str(p).replace(str(_home()), "~")


def _copy_tree(
    src: pathlib.Path,
    dst: pathlib.Path,
    dry: bool,
    acts: list[str],
    exclude: tuple[str, ...] = (),
    skip_rel: Collection[str] = (),
) -> None:
    if not src.is_dir():
        return
    for f in sorted(src.rglob("*")):
        if (
            f.is_dir()
            or _generated(f)
            or any(f.match(e) for e in exclude)
            or f.relative_to(src).as_posix() in skip_rel
        ):
            continue
        rel = f.relative_to(src)
        out = dst / rel
        if out.exists() and out.read_bytes() == f.read_bytes():
            continue
        acts.append(f"write {short(out)}")
        if not dry:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)


def _generated(path: pathlib.Path) -> bool:
    return bool(GENERATED_PARTS.intersection(path.parts)) or path.suffix in GENERATED_SUFFIXES


def _files_under(base: pathlib.Path, exclude: tuple[str, ...] = ()) -> set[str]:
    """Relative POSIX paths of the files under *base*, as _copy_tree sees them."""
    if not base.is_dir():
        return set()
    return {
        f.relative_to(base).as_posix()
        for f in base.rglob("*")
        if f.is_file() and not _generated(f) and not any(f.match(e) for e in exclude)
    }


def _skill_excludes(skills: pathlib.Path) -> set[str]:
    """Skill names skills/sync-exclude.json marks host-adapted, never synced."""
    cfg = skills / "sync-exclude.json"
    if not cfg.is_file():
        return set()
    try:
        return set(json.loads(cfg.read_text()).get("exclude", {}))
    except (OSError, ValueError, AttributeError):
        return set()


def _skill_files(skills: pathlib.Path, excluded: set[str]) -> set[str]:
    """<skill>/<path> for every file of every skill directory under *skills*."""
    if not skills.is_dir():
        return set()
    return {
        f"{d.name}/{rel}"
        for d in skills.iterdir()
        if d.is_dir() and d.name not in excluded
        for rel in _files_under(d)
    }


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# A bare 64-character hex digest sitting next to a "hashes" key whose path
# contains "secret" or "token" (every delivered rule under that name) matches
# gitleaks' default generic-api-key rule. A 7-character prefix clears that
# rule, verified against a full real manifest (4 leaks before, 0 after). The
# mechanism is a length boundary on the rule's matched value, not gitleaks
# recognizing "sha256" by name, so a future gitleaks release that widens that
# window could re-match; re-verify against the version in use if that ever
# happens.
# Internal comparisons stay on the raw digest throughout; the prefix is added
# only where a manifest is written and stripped immediately where one is
# read, so an older manifest (bare hex, from before this change, or from a
# project not yet re-synced) still reads back correctly.
MANIFEST_HASH_PREFIX = "sha256-"


def _manifest_hash(digest: str) -> str:
    """The form a raw sha256 hex digest takes in a manifest's "hashes" map."""
    return f"{MANIFEST_HASH_PREFIX}{digest}"


def _manifest_digest(value: str) -> str:
    """The raw sha256 hex digest behind a manifest "hashes" value, whether it
    carries the current sha256-<hex> form or the bare <hex> form a manifest
    written before this change (or by a project not yet re-synced) has."""
    return value[len(MANIFEST_HASH_PREFIX) :] if value.startswith(MANIFEST_HASH_PREFIX) else value


def _read_delivered(path: pathlib.Path) -> tuple[dict[str, str | None], bool]:
    """(entries, hashed) from this checkout's last successful sync.

    entries maps each recorded path to the sha256 of the content delivered
    there, or None where the manifest predates hashes (a list-only "paths"
    manifest from before hashes). hashed is True when the manifest carries a
    "hashes" map, which makes it the complete record of what the engine
    delivered: the prune then removes only recorded paths, never a file the
    project wrote at a path the engine merely could have shipped. Every digest
    is normalized to its raw hex form here (see _manifest_digest), so every
    other reader of this dict compares like with like regardless of which
    manifest form is on disk.
    """
    if not path.is_file():
        return {}, False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    paths = data.get("paths")
    out: dict[str, str | None] = (
        {p: None for p in paths if isinstance(p, str)} if isinstance(paths, list) else {}
    )
    hashes = data.get("hashes")
    if isinstance(hashes, dict):
        out.update({p: _manifest_digest(h) for p, h in hashes.items() if isinstance(p, str) and isinstance(h, str)})
    return out, isinstance(hashes, dict)


def _under(recorded: dict[str, str | None], prefix: str) -> dict[str, str | None]:
    """The manifest entries under *prefix*, keyed relative to it."""
    return {p[len(prefix):]: h for p, h in recorded.items() if p.startswith(prefix)}


def _locally_edited(path: pathlib.Path, recorded: str | None) -> bool:
    """True when *path* was delivered with a recorded hash and no longer matches it."""
    return recorded is not None and path.is_file() and _sha256(path) != recorded


def _skill_plan(
    name: str,
    src: pathlib.Path,
    recorded: dict[str, str | None],
    hashed: bool,
    own_enabled: bool,
) -> tuple[pathlib.Path, set[str], set[str], set[str]]:
    """What sync_project delivers to .claude/skills/ and what --verify
    compares there, computed once for both: (skills_src, excluded, staged,
    known).

    excluded is the enabled project's own skills/sync-exclude.json plus
    the source root's, less the enabled project's own skill names: the source root's list
    marks shared skills host-adapted and never hides an opted-in project skill
    of the same name. staged is <skill>/<path> for each file the stage ships less
    excluded skills.
    known is what the prune may remove: with a hashed manifest, exactly the
    paths it records (*recorded*, relative to .claude/skills/); before one
    exists, also every skill file the source root or the project's own source
    could deliver, excluded or not.
    """
    own = PROJECTS_SRC / name / "skills"
    own_names = (
        {d.name for d in own.iterdir() if d.is_dir()}
        if own_enabled and own.is_dir()
        else set()
    )
    skills_src = src / "skills"
    excluded = (
        (_skill_excludes(own) if own_enabled else set())
        | (_skill_excludes(ROOT / "skills") - own_names)
    )
    staged = _skill_files(skills_src, excluded)
    known = set(recorded)
    if not hashed:
        known |= _skill_files(ROOT / "skills", set()) | _skill_files(own, set())
    return skills_src, excluded, staged, known


def _read_managed_components(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """Project component keys owned by the previous successful sync."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set()
    if not isinstance(document, dict):
        return set(), set()

    def keys(field: str) -> set[str]:
        value = document.get(field)
        return {key for key in value if isinstance(key, str)} if isinstance(value, list) else set()

    return keys("project_plugins"), keys("project_mcp_servers")


def _known_shared(d: str) -> set[str]:
    """Paths under .claude/<d>/ that a shared control-plane cell could deliver.

    Mirrors staging._stage_shared: agents and commands flat, hooks as
    <name>.sh plus lib/** and the opencode module. scripts has no shared
    cell, so the engine's own tooling is never known. The hooks a project can
    receive are the packaged guards overlaid by the source root's hooks/.
    """
    base = ROOT / d
    if d in ("agents", "commands"):
        return {p.name for p in base.glob("*.md")} if base.is_dir() else set()
    if d == "hooks":
        known: set[str] = set()
        for directory in _hook_dirs():
            if not directory.is_dir():
                continue
            known |= {p.name for p in directory.glob("*.sh")}
            known |= {f"lib/{rel}" for rel in _files_under(directory / "lib")}
            if (directory / "opencode-runtime-hooks.ts").is_file():
                known.add("opencode-runtime-hooks.ts")
        return known
    return set()


def _prune(
    target: pathlib.Path,
    known: set[str],
    staged: set[str],
    dry: bool,
    acts: list[str],
    recorded: dict[str, str | None],
) -> tuple[set[str], dict[str, str]]:
    """Remove each file under *target* that the engine could ship there (a path
    in *known*) and the stage does not ship now. A cleared control-plane cell
    is therefore a removal, not a file left behind while --check says current.
    Files at paths the engine has never known are the project's own and stay.

    *recorded* maps a path relative to *target* to the hash the manifest
    recorded when it was delivered. A file whose content no longer matches
    that hash was edited in the project: it is kept, noted on stderr, and
    returned with its hash so the next manifest still carries it and the
    next run keeps it too. It is a note and never an action, which would
    keep --check stale forever.

    Returns (known paths that are not staged, which registrations must drop;
    kept locally edited paths with their recorded hashes).
    """
    gone = known - staged
    kept: dict[str, str] = {}
    if not target.is_dir():
        return gone, kept
    for f in sorted(target.rglob("*")):
        if not f.is_file() or _generated(f):
            continue
        rel = f.relative_to(target).as_posix()
        if rel not in gone:
            continue
        if _locally_edited(f, recorded.get(rel)):
            print(
                f"{PROG}: note: keep {short(f)}, locally edited since the engine delivered it",
                file=sys.stderr,
            )
            kept[rel] = recorded[rel]
            continue
        acts.append(f"remove {short(f)}")
        if dry:
            continue
        f.unlink()
        parent = f.parent
        while parent != target and parent.is_dir() and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except OSError as exc:
                print(f"{PROG}: note: could not remove empty directory {short(parent)}: {exc}", file=sys.stderr)
                break
            parent = parent.parent
    return gone, kept


def _drop_registrations(hooks: dict, prefix: str, gone: set[str]) -> dict:
    """*hooks* without any hook entry whose command runs a file in *gone*.

    A registration left pointing at a removed file fails on every call it
    matches. A group is dropped only when nothing else is left in it, and an
    event only when it has no group left.
    """
    patterns = [re.compile(re.escape(prefix + rel) + r"(?![\w./-])") for rel in sorted(gone)]

    def stale(hook) -> bool:
        command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
        return any(p.search(command) for p in patterns)

    out: dict = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"] if not stale(h)]
            if len(kept) == len(group["hooks"]):
                kept_groups.append(group)
            elif kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups or not groups:
            out[event] = kept_groups
    return out


def _registered_hook_paths(hooks: dict, prefix: str) -> set[str]:
    """Paths named by project hook commands, including deleted source paths."""
    pattern = re.compile(re.escape(prefix) + r"([\w./-]+)")
    return {
        match.group(1)
        for groups in hooks.values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for hook in group["hooks"]
        if isinstance(hook, dict)
        for match in pattern.finditer(str(hook.get("command", "")))
    }


def _write_if_changed(
    path: pathlib.Path, data: bytes, dry: bool, acts: list[str]
) -> None:
    if path.exists() and path.read_bytes() == data:
        return
    acts.append(f"write {short(path)}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _surface_plan(src: pathlib.Path) -> tuple[bytes | None, list[str], list[str]]:
    """Return (master_body, wanted, shims) for one staged or raw project source.

    A project may opt out of byte-identical surfaces by listing which ones it
    wants generated in <src>/surfaces.json: {"copy": [...]}. Surfaces not
    listed are shims: left alone because they are hand-authored. Absent the
    file, every surface is wanted. master_body is None when the source has no
    AGENTS.md master to copy from.
    """
    master = src / "AGENTS.md"
    if not master.exists():
        return None, [], []
    body = master.read_bytes()
    cfg = src / "surfaces.json"
    wanted = (
        json.loads(cfg.read_text()).get("copy", list(SURFACES))
        if cfg.exists()
        else list(SURFACES)
    )
    shims = [s for s in SURFACES if s not in wanted]
    return body, wanted, shims


def _embed_project_digest(body: bytes, name: str) -> bytes:
    """Replace body's RULES-DIGEST marker region with *name*'s column digest.

    F-23: the digest is rendered per control-plane.md column, in tier order,
    from that column's rule opt-ins. A master with no marker pair is
    returned unchanged, the same way the digest generator never appends
    markers to a file that has not opted into carrying the digest; a project
    that wants the digest adds the marker pair to its own
    projects-root/<name>/AGENTS.md once, by hand.

    The digest is rendered for ROOT: a project's instruction files are what
    a session in that checkout reads, so every `${STRATARC_SOURCE}` token in
    the rule text becomes this source tree's path and the `Full rule:`
    pointers name its rules/ directory, the same rendering staging gives
    the master body. Only the committed AGENTS.md of the source repository
    itself keeps the token (stratarc.sync regenerates it).
    """
    text = body.decode("utf-8")
    if DIGEST_BEGIN not in text:
        return body
    digest = digest_or_note(
        ROOT / "rules",
        column=name,
        control_plane_path=ROOT / "control-plane.md",
        source_root=ROOT,
    )
    return embed_digest(text, digest).encode("utf-8")


def _surface_bodies(body: bytes, name: str, src: pathlib.Path) -> dict[str, bytes]:
    """Each surface's bytes, given the master body with its column digest embedded.

    Every surface carries the same body: the column digest already renders
    each rule as its binding section plus a pointer to the full text, on
    every runtime.
    """
    return {s: body for s in SURFACES}


LOCAL_SETTINGS = ".claude/settings.local.json"


def _note(message: str) -> None:
    """A fact on stderr, never a pending action (an action keeps --check stale)."""
    print(f"{PROG}: note: {message}", file=sys.stderr)


def _declared_for_claude() -> tuple[set[str], set[str]] | None:
    """(plugin keys, MCP server names) components.json declares for Claude
    Code, whatever each entry's wanted value, or None when it cannot be read.

    These are the names a project's local scope owns: a selected one is
    written, and a declared one no longer selected (its cell cleared, or the
    entry marked unwanted) is removed. Every other key is the user's.
    """
    path = ROOT / "components.json"
    if not path.is_file():
        return set(), set()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    plugins = document.get("plugins") if isinstance(document.get("plugins"), list) else []
    servers = document.get("mcp_servers") if isinstance(document.get("mcp_servers"), list) else []
    return (
        {
            _components.plugin_key(entry)
            for entry in plugins
            if isinstance(entry, dict) and entry.get("name") and "claude" in (entry.get("runtimes") or [])
        },
        {str(entry["name"]) for entry in servers if _components.renders(entry, "claude", ROOT)},
    )


def _read_object(path: pathlib.Path, label: str) -> dict | None:
    """The JSON object at path, {} when absent, or None (with a note) when it cannot be read."""
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = None
    if not isinstance(document, dict):
        _note(f"{short(path)} is not a JSON object; {label} left as it is")
        return None
    return document


def _merged_by_key(current: object, owned: set[str], wanted: dict) -> dict | None:
    """current with every owned key not wanted removed and wanted set, or
    None when current is not an object."""
    if current is None:
        current = {}
    if not isinstance(current, dict):
        return None
    merged = {key: value for key, value in current.items() if key not in owned}
    merged.update(wanted)
    return merged


def _render_local_plugins(name: str, dst: pathlib.Path, owned: set[str], wanted: set[str], dry: bool, acts: list[str]) -> bool:
    """enabledPlugins in dst/.claude/settings.local.json, merged by key.

    Only an untracked file is written: component configuration never enters
    a tracked file of a project repository, since that commits one
    machine's choices into another repository. A checkout git cannot read is
    left alone rather than guessed about.
    """
    code, tracked = _git_out(dst, "ls-files", "--", LOCAL_SETTINGS)
    if code != 0:
        _note(f"{name}: git cannot read {short(dst)}; {LOCAL_SETTINGS} left as it is")
        return False
    if tracked:
        _note(f"{name}: {LOCAL_SETTINGS} is tracked in {short(dst)}; its plugin selection is not written")
        return False
    ignored, _ = _git_out(dst, "-c", "core.excludesFile=/dev/null", "check-ignore", "-q", "--", LOCAL_SETTINGS)
    if ignored != 0:
        code, location = _git_out(dst, "rev-parse", "--git-path", "info/exclude")
        if code != 0 or not location:
            _note(f"{name}: git cannot locate info/exclude in {short(dst)}; {LOCAL_SETTINGS} left as it is")
            return False
        exclude = pathlib.Path(location)
        if not exclude.is_absolute():
            exclude = dst / exclude
        try:
            original = exclude.read_bytes() if exclude.exists() else b""
        except OSError:
            _note(f"{name}: git cannot read {short(exclude)}; {LOCAL_SETTINGS} left as it is")
            return False
        rule = b"/.claude/settings.local.json"
        present = rule in original.splitlines()
        if not present:
            newline = b"" if not original or original.endswith(b"\n") else b"\n"
            _write_if_changed(exclude, original + newline + rule + b"\n", dry, acts)
        if present or not dry:
            ignored, _ = _git_out(dst, "-c", "core.excludesFile=/dev/null", "check-ignore", "-q", "--", LOCAL_SETTINGS)
            if ignored != 0:
                _note(f"{name}: {LOCAL_SETTINGS} is still not ignored in {short(dst)}; its plugin selection is not written")
                return False
    path = dst / LOCAL_SETTINGS
    document = _read_object(path, "its plugin selection")
    if document is None:
        return False
    current = document.get("enabledPlugins")
    merged = _merged_by_key(current, owned, {key: True for key in sorted(wanted)})
    if merged is None:
        _note(f"{short(path)} enabledPlugins is not an object; left as it is")
        return False
    if merged == (current or {}):
        return True
    updated = dict(document)
    if merged:
        updated["enabledPlugins"] = merged
    else:
        updated.pop("enabledPlugins", None)
    _write_if_changed(path, (json.dumps(updated, indent=2) + "\n").encode(), dry, acts)
    return True


def _render_local_servers(name: str, dst: pathlib.Path, owned: set[str], wanted: dict, dry: bool, acts: list[str]) -> bool:
    """mcpServers under this checkout's projects entry in ~/.claude.json,
    Claude Code's local MCP scope, merged by key.

    The file is Claude Code's own and holds much besides; only that one key
    of that one entry changes, and the write replaces the file atomically so
    a concurrent reader never sees half of it.
    """
    account = _home() / ".claude.json"
    document = _read_object(account, f"{name}'s MCP servers")
    if document is None:
        return False
    projects = document.get("projects")
    key = str(dst.resolve())
    entry = projects.get(key) if isinstance(projects, dict) else None
    if (projects is not None and not isinstance(projects, dict)) or (entry is not None and not isinstance(entry, dict)):
        _note(f"{short(account)} projects is not an object; {name}'s MCP servers left as they are")
        return False
    current = (entry or {}).get("mcpServers")
    merged = _merged_by_key(current, owned, wanted)
    if merged is None:
        _note(f"{short(account)} projects.{key}.mcpServers is not an object; left as it is")
        return False
    if merged == (current or {}):
        return True
    updated = dict(document)
    updated["projects"] = dict(projects or {})
    updated["projects"][key] = dict(entry or {}, mcpServers=merged)
    acts.append(f"write {short(account)} projects.{key}.mcpServers")
    if not dry:
        # The replacement keeps the account file's mode (0600 by default when
        # it is new): the file holds account state, and a umask-default new
        # file would widen it.
        mode = account.stat().st_mode & 0o777 if account.exists() else 0o600
        temporary = account.with_name(f".{account.name}.{paths.engine_name(ROOT)}-{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(updated, indent=2) + "\n")
            os.chmod(temporary, mode)
            os.replace(temporary, account)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return True


def _render_project_components(
    name: str, src: pathlib.Path, dst: pathlib.Path, previous: tuple[set[str], set[str]], dry: bool, acts: list[str]
) -> tuple[set[str], set[str]]:
    """The project column's plugin: and mcp: cells -> Claude Code's local,
    untracked scope.

    The stage's components.json holds the column's own selection
    (stratarc.staging). The user scope already writes every unselected
    plugin false (stratarc.adapters.claude), so only the project's
    selections are written here, as true. Codex, Gemini CLI and OpenCode get
    the global selection until their untracked project scopes are confirmed
    on a live install.
    """
    declared = _declared_for_claude()
    if declared is None:
        _note(f"{name}: components.json cannot be read; project components left as they are")
        return previous
    plugin_keys, server_names = declared
    owned_plugins = plugin_keys | previous[0]
    owned_servers = server_names | previous[1]
    managed_plugins = plugin_keys
    managed_servers = server_names
    servers, notes = _components.selected_servers(src, "claude", project=True)
    for line in notes:
        _note(f"{name}: {line}")
    if owned_plugins:
        wanted = set(_components.selected_plugins(src, "claude")) & plugin_keys
        if not _render_local_plugins(name, dst, owned_plugins, wanted, dry, acts):
            managed_plugins = owned_plugins
            acts.append(f"component render blocked: {name} plugin selection")
    if owned_servers:
        wanted_servers = {key: _components.claude_server(entry) for key, entry in sorted(servers.items()) if key in server_names}
        if not _render_local_servers(name, dst, owned_servers, wanted_servers, dry, acts):
            managed_servers = owned_servers
            acts.append(f"component render blocked: {name} MCP server selection")
    return managed_plugins, managed_servers


class ProjectRefused(Exception):
    """One project cannot be delivered; the message says why and names the file."""


def _read_settings(settings_path: pathlib.Path, name: str) -> dict:
    """The project's .claude/settings.json, {} when absent; a file that does not parse refuses the project."""
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        refusal = CliError("msg-1120", param="settings", name=name, path=short(settings_path), detail=error)
        raise ProjectRefused(f"{refusal.id}  {refusal.problem} {refusal.recovery}") from error


def sync_project(name: str, dry: bool, cp=None) -> list[str]:
    """Sync one project. If a control plane is given, source is a staged view
    filtered by that project's column; otherwise projects-root/<name>/ raw.

    Raises ProjectRefused, before anything is written, when the project's own
    .claude/settings.json is not valid JSON."""
    dst, why = resolve(name, cp)
    acts: list[str] = []
    if dst is None:
        return [why]
    _read_settings(dst / ".claude" / "settings.json", name)
    delivered_path = dst / delivered_manifest()
    previous, hashed = _read_delivered(delivered_path)
    managed_components = _read_managed_components(delivered_path)
    renderer_state = _read_renderer_state(delivered_path)
    retired_hooks = retired_hook_names()
    # Manifest path -> sha256 of the content delivered there, for every file
    # this run ships; retained holds formerly delivered files kept because the
    # project edited them, with the hash they were delivered with.
    delivered: dict[str, str] = {}
    retained: dict[str, str] = {}

    def record(prefix: str, base: pathlib.Path, rels: set[str]) -> None:
        delivered.update({prefix + rel: _sha256(base / rel) for rel in rels})

    def prune(
        target: pathlib.Path,
        prefix: str,
        could: set[str],
        staged: set[str],
        always_known: frozenset[str] | set[str] = frozenset(),
    ) -> set[str]:
        # With a hashed manifest the prune removes exactly what it records
        # as delivered; before one exists it also removes what the engine
        # could have shipped there (*could*), the only record there was.
        # always_known is never discarded either way: a name in it (a
        # retired hook, from retired_hook_names()) is one the engine is
        # certain it once delivered, so it stays prunable even when this
        # prefix's record predates that delivery or never covered it.
        recorded = _under(previous, prefix)
        known = (set(recorded) if hashed else could | set(recorded)) | always_known
        gone, kept = _prune(target, known, staged, dry, acts, recorded)
        retained.update({prefix + rel: digest for rel, digest in kept.items()})
        return gone

    if cp is not None:
        src, notes = build_stage(ROOT, cp, name)
        for n in notes:
            acts.append(f"control-plane: {n}")
    else:
        src = PROJECTS_SRC / name

    body, wanted, _shims = _surface_plan(src)
    if body is not None:
        bodies = {s: body for s in SURFACES}
        if cp is not None:
            bodies = _surface_bodies(_embed_project_digest(body, name), name, src)
        for s in SURFACES:
            if s in wanted:
                _write_if_changed(dst / s, bodies[s], dry, acts)

    # Per-project renderers registered by source-root extensions (see
    # load_renderer_extensions); none are registered in a plain install.
    new_renderer_state = _run_renderers(name, dst, dry, acts, renderer_state)

    for d in CLAUDE_DIRS:
        if d == "skills":
            continue  # delivered per skill directory below, less excluded skills
        # A project file at a carried path would be overwritten by the carry
        # on every run, so the carried guard's paths are never copied from the
        # project's own tree.
        # Staging an opted-in shared hook copies hooks/lib into the stage, so
        # an identical copy is expected and silent; a differing one is a note
        # on stderr, never a pending action that would keep --check stale.
        if d == "hooks":
            for rel in CARRIED_GUARD_ALL_FILES:
                own, carried = src / d / rel, carried_source(rel)
                if own.is_file() and not (carried is not None and own.read_bytes() == carried.read_bytes()):
                    print(
                        f"{PROG}: note: {name} hooks/{rel} differs from source and is shadowed by the carried guard",
                        file=sys.stderr,
                    )
        _copy_tree(
            src / d,
            dst / ".claude" / d,
            dry,
            acts,
            exclude=("hooks.json",),
            skip_rel=CARRIED_GUARD_ALL_FILES if d == "hooks" else (),
        )
    _carry_guard(dst, dry, acts)

    # Skill cells in a project column, and the project's own skills when its
    # project:skills row is on, are staged under skills/<name>/ and delivered
    # to each of SKILL_TARGETS/<name>/, less any skill sync-exclude.json marks
    # host-adapted.
    skills_src, skill_excluded, staged_skills, known_skills = _skill_plan(
        name, src, _under(previous, SKILL_TARGETS[0]), hashed,
        cp is None or cp.enabled(name, "project:skills"),
    )
    skill_collisions: dict[str, set[str]] = {target: set() for target in SKILL_TARGETS}
    if skills_src.is_dir():
        for skill in sorted(p for p in skills_src.iterdir() if p.is_dir()):
            if skill.name not in skill_excluded:
                for target in SKILL_TARGETS:
                    collided = set()
                    for rel in _files_under(skill):
                        staged_rel = f"{skill.name}/{rel}"
                        out = dst / target / staged_rel
                        if out.is_file() and target + staged_rel not in previous:
                            skill_collisions[target].add(staged_rel)
                            collided.add(rel)
                            print(
                                f"{PROG}: note: collision {short(out)}: project file kept",
                                file=sys.stderr,
                            )
                    _copy_tree(
                        skill, dst / target / skill.name, dry, acts,
                        skip_rel=collided,
                    )

    # Prune the other delivered trees: a file the engine delivered there (or,
    # before a hashed manifest, could ship there from a shared cell's source
    # or the project's own source) that the stage does not ship now is
    # removed. The carried guard is never pruned; it is delivered whatever
    # the cells say.
    own = PROJECTS_SRC / name
    gone_hooks: set[str] = set()
    staged_hooks: set[str] = set()
    for d in ("agents", "commands", "hooks", "scripts"):
        prefix = f".claude/{d}/"
        known = _known_shared(d) | _files_under(own / d, exclude=("hooks.json",))
        staged = _files_under(src / d, exclude=("hooks.json",))
        if d == "hooks":
            known -= set(CARRIED_GUARD_ALL_FILES)
            staged -= set(CARRIED_GUARD_ALL_FILES)
        record(prefix, src / d, staged)
        # The carried guard is never pruned (_carry_guard delivers it
        # unconditionally), so a name in retired_hook_names() is excluded
        # here the same way known already excludes CARRIED_GUARD_FILES
        # above: nothing on that list is retired today, but a future one
        # that were would otherwise be deleted and immediately re-carried
        # on every sync.
        always_known = (
            retired_hooks - set(CARRIED_GUARD_ALL_FILES) if d == "hooks" else frozenset()
        )
        gone = prune(dst / ".claude" / d, prefix, known, staged, always_known=always_known)
        if d == "hooks":
            gone_hooks = gone
            staged_hooks = staged
    for target in SKILL_TARGETS:
        record(target, skills_src, staged_skills - skill_collisions[target])
        # The engine never shipped to .agents/skills/ before a hashed
        # manifest, so only .claude/skills/ has paths it could have shipped
        # without a record.
        could = known_skills if target == SKILL_TARGETS[0] else set()
        prune(dst / target, target, could, staged_skills)

    # Prune: anything under .claude/rules that the stage does not ship and that
    # the engine COULD ship (a name that exists in the source root's rules/,
    # in the project's rules/, or in rules/retired.json) is an opt-out or a
    # retired rule and gets removed, unless the project edited its delivered
    # copy since the engine shipped it, which is kept and reported the same
    # way _prune keeps an edited file everywhere else. Files with names the
    # engine has never known are left alone.
    rules_prefix = ".claude/rules/"
    rules_recorded = _under(previous, rules_prefix)
    known_rules = {p.name for p in (ROOT / "rules").rglob("*.md")} | retired_rule_names()
    if (PROJECTS_SRC / name / "rules").is_dir():
        known_rules |= {p.name for p in (PROJECTS_SRC / name / "rules").glob("*.md")}
    staged_rules = (
        {p.name for p in (src / "rules").rglob("*.md")}
        if (src / "rules").is_dir()
        else set()
    )
    # record() hashes base / rel; staged_rules is names only (rglob collapses
    # any nested source path to its basename), so only the names that are
    # actually flat under src/rules are hashed. A staged rule is always
    # delivered flat (below), so this only guards a source that is not.
    record(
        rules_prefix,
        src / "rules",
        {n for n in staged_rules if (src / "rules" / n).is_file()},
    )
    # A staged rule is delivered flat, at .claude/rules/<name>.md. A known name
    # found at any other path (an old nested layout such as rules/global/) is
    # always a stale duplicate of the flat delivered copy, removed regardless
    # of edits there: that path was never the delivered location of record.
    target_rules = dst / ".claude" / "rules"
    if target_rules.is_dir():
        for f in target_rules.rglob("*.md"):
            if f.name not in known_rules:
                continue
            canonical = f.relative_to(target_rules) == pathlib.Path(f.name)
            if f.name in staged_rules and canonical:
                continue
            if canonical and _locally_edited(f, rules_recorded.get(f.name)):
                print(
                    f"{PROG}: note: keep {short(f)}, locally edited since the engine delivered it",
                    file=sys.stderr,
                )
                retained[rules_prefix + f.name] = rules_recorded[f.name]
                continue
            acts.append(f"remove {short(f)}")
            if not dry:
                f.unlink()

    hooks_json = src / "hooks" / "hooks.json"
    hooks = json.loads(hooks_json.read_text()) if hooks_json.exists() else None
    claude_hook_prefix = "$CLAUDE_PROJECT_DIR/.claude/hooks/"
    if hooks is not None:
        gone_hooks |= _registered_hook_paths(hooks, claude_hook_prefix) - staged_hooks

    # Claude Code: .claude/settings.json. The project's own hooks.json, when it
    # has one, replaces the hooks key; without one, whatever hooks the file
    # already registers are kept. Either way the carried guard is registered
    # beside them, and every other settings key is preserved.
    settings_path = dst / ".claude" / "settings.json"
    settings = _read_settings(settings_path, name)
    base = _drop_registrations(
        hooks if hooks is not None else settings.get("hooks") or {},
        claude_hook_prefix,
        gone_hooks,
    )
    wanted = _with_carried_guards(base)
    if settings.get("hooks") != wanted:
        settings["hooks"] = wanted
        _write_if_changed(
            settings_path,
            (json.dumps(settings, indent=2) + "\n").encode(),
            dry,
            acts,
        )

    _activate_tracked_git_hooks(dst, dry, acts)
    _render_attribution_check(dst, dry, acts)
    if cp is not None:
        managed_components = _render_project_components(name, src, dst, managed_components, dry, acts)

    codex_prefix = ".codex/hooks/"
    codex_known = _known_shared("hooks") | _files_under(own / "hooks", exclude=("hooks.json",))
    codex_known |= set(_under(previous, codex_prefix))
    codex_known |= retired_hooks
    codex_staged = (
        _files_under(src / "hooks", exclude=("hooks.json", "*.test.sh"))
        if hooks is not None
        else set()
    )
    record(codex_prefix, src / "hooks", codex_staged)
    codex_gone = codex_known - codex_staged
    if hooks is not None:
        codex_gone |= _registered_hook_paths(hooks, claude_hook_prefix) - codex_staged

    if hooks is not None:
        # Codex: rewrite paths, wrap, write .codex/hooks.json. Built from the
        # project's own hooks only: the carried guard is a .claude/ delivery
        # and is not copied into .codex/hooks/.
        codex_hooks = json.loads(
            json.dumps(hooks).replace(
                "$CLAUDE_PROJECT_DIR/.claude/hooks/", "$CODEX_PROJECT_DIR/.codex/hooks/"
            )
        )
        codex_hooks = _drop_registrations(
            codex_hooks, "$CODEX_PROJECT_DIR/.codex/hooks/", codex_gone
        )
        _write_if_changed(
            dst / ".codex" / "hooks.json",
            (json.dumps({"hooks": codex_hooks}, indent=2) + "\n").encode(),
            dry,
            acts,
        )
        _copy_tree(
            src / "hooks",
            dst / ".codex" / "hooks",
            dry,
            acts,
            exclude=("hooks.json", "*.test.sh"),
        )

    # The Codex copy is pruned the same way. It carries no guard, so every
    # known hook path is prunable there, and it is only delivered while the
    # stage has a hooks.json.
    prune(
        dst / ".codex" / "hooks",
        codex_prefix,
        codex_known,
        codex_staged,
        always_known=retired_hooks,
    )
    codex_json = dst / ".codex" / "hooks.json"
    if hooks is None and codex_json.is_file():
        try:
            registered = json.loads(codex_json.read_text()).get("hooks") or {}
        except (OSError, ValueError, AttributeError):
            registered = None
        if isinstance(registered, dict):
            kept = _drop_registrations(registered, "$CODEX_PROJECT_DIR/.codex/hooks/", codex_gone)
            if kept != registered:
                _write_if_changed(
                    codex_json,
                    (json.dumps({"hooks": kept}, indent=2) + "\n").encode(),
                    dry,
                    acts,
                )

    _warn_on_gitignored_delivered_paths(name, dst, delivered)

    # "paths" is what this run delivered; "hashes" covers those and every
    # retained locally edited file, so a kept file stays protected. Every
    # value written here carries the sha256- prefix (MANIFEST_HASH_PREFIX);
    # _read_delivered strips it back off, so this dict itself stays raw hex
    # throughout, the same as retained/delivered above.
    hashes = dict(sorted({**retained, **delivered}.items()))
    _write_if_changed(
        delivered_path,
        (json.dumps({
            "paths": sorted(delivered),
            "hashes": {p: _manifest_hash(h) if isinstance(h, str) else h for p, h in hashes.items()},
            "project_plugins": sorted(managed_components[0]),
            "project_mcp_servers": sorted(managed_components[1]),
            RENDERERS_FIELD: dict(sorted(new_renderer_state.items())),
        }, indent=2) + "\n").encode(),
        dry,
        acts,
    )
    return acts


def adopt(name: str, cp=None) -> list[str]:
    """Capture an existing checkout's project config into projects-root."""
    dst = PROJECTS_SRC / name
    src, why = resolve(name, cp)
    acts: list[str] = []
    if src is None:
        return [f"adopt: {why}"]
    dst.mkdir(parents=True, exist_ok=True)
    retired_hooks = retired_hook_names()

    for s in SURFACES:
        f = src / s
        if f.exists():
            shutil.copy2(f, dst / "AGENTS.md")
            acts.append(f"master from {s}")
            break

    # Untouched delivered files and shared skills stay out of the capture.
    # A file changed since delivery belongs to the project and is captured.
    # A paths-only manifest records that the engine delivered a shared skill
    # but carries no hash, so nothing can show a local edit there: the checkout
    # copy may differ from the source only because the source moved on since
    # delivery, and such a skill is never adopted. It is noted when it differs.
    delivered, _hashed = _read_delivered(src / delivered_manifest())
    root_skills = ROOT / "skills"
    shared_skills = {p.name for p in root_skills.iterdir() if p.is_dir()} if root_skills.is_dir() else set()
    claude_skills = src / ".claude" / "skills"
    agents_skills = src / ".agents" / "skills"
    stale_shared: set[str] = set()

    def edited_or_unknown(path: pathlib.Path, recorded: str) -> bool:
        if recorded in delivered and delivered[recorded] is None:
            for prefix in SKILL_TARGETS:
                if recorded.startswith(prefix):
                    rel = recorded[len(prefix):]
                    if rel.split("/", 1)[0] in shared_skills:
                        shared = root_skills / rel
                        if shared.is_file() and path.read_bytes() != shared.read_bytes():
                            stale_shared.add(rel)
                        return False
            return True
        return _locally_edited(path, delivered.get(recorded))

    def capture_skill(path: pathlib.Path, prefix: str, rel: str) -> bool:
        recorded = f"{prefix}skills/{rel}"
        return edited_or_unknown(path, recorded) or (
            recorded not in delivered and rel.split("/", 1)[0] not in shared_skills
        )

    conflicts = {
        rel for rel in _files_under(claude_skills) & _files_under(agents_skills)
        if capture_skill(claude_skills / rel, ".claude/", rel)
        and capture_skill(agents_skills / rel, ".agents/", rel)
        and (claude_skills / rel).read_bytes() != (agents_skills / rel).read_bytes()
    }
    acts.extend(f"conflict skills/{rel}: .claude and .agents differ; neither captured" for rel in sorted(conflicts))
    for d in CLAUDE_DIRS:
        sd = src / ".claude" / d
        if not sd.is_dir():
            continue
        captured_any = False
        for f in sorted(sd.rglob("*")):
            rel = f.relative_to(sd).as_posix()
            if not f.is_file():
                continue
            if d == "skills" and rel in conflicts:
                continue
            if d == "hooks" and rel in retired_hooks:
                # A retired hook the engine once shipped is never the
                # project's own, even with no manifest record of it: the
                # next sync_project prune removes it, so capturing it here
                # would only re-deliver a stale orphan as project-owned.
                continue
            path = f".claude/{d}/{rel}"
            edited = edited_or_unknown(f, path)
            if not edited and (
                path in delivered
                or (d == "skills" and rel.split("/", 1)[0] in shared_skills)
            ):
                continue
            out = dst / d / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            captured_any = True
        if captured_any:
            acts.append(f"captured .claude/{d}")

    if agents_skills.is_dir():
        captured_any = False
        for f in sorted(agents_skills.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(agents_skills).as_posix()
            if rel in conflicts:
                continue
            path = f".agents/skills/{rel}"
            edited = edited_or_unknown(f, path)
            if not edited and (
                path in delivered or rel.split("/", 1)[0] in shared_skills
            ):
                continue
            out = dst / "skills" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            captured_any = True
        if captured_any:
            acts.append("captured .agents/skills")
    acts.extend(
        f"note skills/{rel}: differs from shared source; "
        "a paths-only manifest cannot show a local edit, not captured"
        for rel in sorted(stale_shared)
    )

    settings = src / ".claude" / "settings.json"
    if settings.exists():
        hooks = json.loads(settings.read_text()).get("hooks")
        if hooks:
            (dst / "hooks").mkdir(exist_ok=True)
            (dst / "hooks" / "hooks.json").write_text(
                json.dumps(hooks, indent=2) + "\n"
            )
            acts.append("captured hooks registration")

    for junk in dst.rglob(".DS_Store"):
        junk.unlink()

    # A column rendered before this source existed has its project:* cells
    # off, and the reconciler preserves existing cells, so without this the
    # next sync would stage the source root's own AGENTS.md as this project's
    # master and write it over the checkout's instruction surfaces.
    captured = []
    if (dst / "AGENTS.md").is_file():
        captured.append("project:AGENTS.md")
    captured += [f"project:{d}" for d in CLAUDE_DIRS if (dst / d).is_dir()]
    path = getattr(cp, "path", None)
    if captured and path is not None and path.is_file():
        for option in opt_in(path, name, captured):
            acts.append(f"opted in {option}")
    return acts


def opt_in(path: pathlib.Path, column: str, options: list[str]) -> list[str]:
    """Set *column*'s cell to x for each of *options* in control-plane.md.

    Rewrites only the matching cells, in place, so every other cell, label,
    and line keeps its bytes. A row or column that does not exist yet is left
    to the reconciler, whose default for a project:* row is on when the
    project's source exists. Returns the options whose cell changed.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed: list[str] = []
    index = None
    for n, raw in enumerate(lines):
        if not raw.lstrip().startswith("|"):
            index = None
            continue
        cells = _cells(raw)
        if cells[0].lower() == "option":
            index = cells.index(column) if column in cells else None
            continue
        if index is None or _option_id(cells[0]) not in options:
            continue
        if index < len(cells) and cells[index].lower() == "x":
            continue
        while len(cells) <= index:
            cells.append("")
        cells[index] = "x"
        ending = "\n" if raw.endswith("\n") else ""
        lines[n] = "| " + " | ".join(cells) + " |" + ending
        changed.append(_option_id(cells[0]))
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stratarc projects")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--adopt", metavar="NAME")
    ap.add_argument(
        "--verify",
        action="store_true",
        help="report manifest and filesystem disagreement, exit 1 if any",
    )
    ap.add_argument("--skip-reconcile", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument(
        "--root",
        metavar="PATH",
        help=f"the source root to deliver from (default: ${SOURCE_VARIABLE}, then the nearest {paths.CONFIG_NAME}, then the current directory)",
    )
    args = ap.parse_args(argv)
    # --root wins; otherwise a root a caller already pinned with
    # configure_root stays, and a first run resolves it now rather than from
    # the import-time default.
    if args.root or not _ROOT_CONFIGURED:
        configure_root(source_root(args.root))
    load_renderer_extensions()

    # The engine's reconciler, in process, handed the same root this run
    # delivers from.
    if not args.skip_reconcile:
        # --verify is read-only by contract: it never runs a writing
        # reconcile, so it always passes --check here too, the same as
        # --check does for a sync run.
        code = reconcile_main(["--root", str(ROOT)] + (["--check"] if (args.check or args.verify) else []))
        if code:
            return code
    cp = ControlPlane.load(ROOT / "control-plane.md")
    try:
        public_targets()
    except RuntimeError as error:
        print(f"{PROG}: refusing; {error}; nothing was delivered", file=sys.stderr)
        return 2

    if args.verify:
        drift = verify(cp)
        for d in drift:
            print(f"verify: {d}")
        if not drift:
            print(f"verify: manifest and tree agree, {len(cp.manifest)} projects")
        try:
            divergent, shims = surface_drift(cp)
        finally:
            cleanup_all()
        for s in shims:
            print(f"verify: shim {s}")
        for d in divergent:
            print(f"verify: drift {d}")
        if not divergent:
            print(f"verify: surfaces agree with the master, {len(shims)} shims")
        guard_drift = carried_guard_drift(cp)
        for g in guard_drift:
            print(f"verify: guard {g}")
        if not guard_drift:
            print("verify: carried guards agree with source")
        try:
            skills, skill_notes = skill_drift(cp)
        finally:
            cleanup_all()
        for s in skill_notes:
            print(f"verify: skill note {s}")
        for s in skills:
            print(f"verify: skill {s}")
        if not skills:
            print(f"verify: project skills agree with source, {len(skill_notes)} notes")
        return 1 if (drift or divergent or guard_drift or skills) else 0

    if args.adopt:
        for a in adopt(args.adopt, cp):
            print(f"adopt {args.adopt}: {a}")
        return 0

    if not PROJECTS_SRC.is_dir():
        print(f"{PROG}: no {short(PROJECTS_SRC)}", file=sys.stderr)
        return 0

    if cp.rows:
        names = [args.only] if args.only else cp.projects()
    else:
        cp = None
        names = (
            [args.only]
            if args.only
            else sorted(p.name for p in PROJECTS_SRC.iterdir() if p.is_dir())
        )
    stale = False
    refused = False
    try:
        for n in names:
            try:
                acts = sync_project(n, args.check, cp)
            except ProjectRefused as error:
                print(f"{PROG}: {error}", file=sys.stderr)
                refused = True
                continue
            if not acts:
                print(f"{PROG}: {n}: current")
                continue
            if not (len(acts) == 1 and acts[0].startswith("skip")):
                stale = True
            print(f"{PROG}: {n} {'would' if args.check else 'did'}:")
            for a in acts:
                print(f"  {a}")
    finally:
        cleanup_all()
    if refused:
        return 2
    return 1 if (args.check and stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
