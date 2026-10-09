"""Project-tier permission sweep with an explicit per-project scope exception.

Other checkouts retain the shared permissions.json policy. A project whose `projects-root/<name>/permissions.json` carries a `projectScope` key uses that file for its exact checkout and explicitly enumerated worktrees, never a directory-prefix grant.

This module walks the projects root and rewrites any project-scope runtime config file that already carries its own permission settings, so the file states the shared policy. A project-scope file with no permission settings of its own already inherits the user-scope rendering, so the sweep never creates a file for an ordinary project: it only rewrites files that already opt into project-scope permissions. A scoped project is the one case that creates its native configs.

The four surfaces, and what is rewritten in each: Claude Code's <project>/.claude/settings.json has its "permissions" object replaced and its top-level "autoMode" key removed, since a project-scope mode and autoMode guidance are both user-scope concerns. Codex's <project>/.codex/config.toml has its top-level approval_policy line rewritten and its sandbox_mode and default_permissions lines removed. Gemini's <project>/.gemini/settings.json has general.defaultApprovalMode rewritten. OpenCode's <project>/opencode.json or opencode.jsonc has its "permission" object rewritten and a legacy "permissions" array removed, since OpenCode refuses a file that carries it.

settings.local.json is never considered: it is machine-local, and the policy itself deny-lists reads of it, so sweeping it would both violate the policy being rendered and leak a machine-local override into a shared render.

Usage:
  python3 -m agentrc.project_permissions                           sweep every project under the projects root
  python3 -m agentrc.project_permissions --check                   dry run, exit 1 if anything is stale
  python3 -m agentrc.project_permissions --only codex              sweep one runtime
  python3 -m agentrc.project_permissions --root PATH               read the policy from a different source root
  python3 -m agentrc.project_permissions --projects-root PATH      sweep a different projects root

The projects root is $LLM_ROOT_PROJECTS_DIR when set, then `projects_root` in the source root's `agentrc.toml`, then ~/projects.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import sys
import tomllib
from typing import Callable, Iterable

from agentrc.adapters._text import set_top_level, strip_jsonc_comments
from agentrc.deploy_guard import refusal
from agentrc.paths import home, projects_dir, source_root
from agentrc.permissions import (
    MAX_WALK_DEPTH,
    PRUNE_DIRS,
    claude_project_permissions,
    codex_approval_policy,
    filesystem_denies,
    gemini_approval_mode,
    load as load_permissions,
    opencode_permission,
    policy_version,
    same_in_order,
)
from agentrc.public_targets import PublicTargetsUnreadable, is_public_checkout, load_public_targets

PRUNE = PRUNE_DIRS
MAX_DEPTH = MAX_WALK_DEPTH  # directory depth below root that the walk descends
RUNTIMES = ("claude", "codex", "gemini", "opencode")


def short(p: pathlib.Path) -> str:
    """Render p with the home prefix replaced by a tilde, for readable output."""
    return str(p).replace(str(home()), "~")


def candidates(
    root: pathlib.Path, runtimes: Iterable[str] | None = None
) -> list[tuple[str, pathlib.Path]]:
    """Find every project-scope runtime file under root that may carry permission settings.

    Walks root with os.walk, pruning directory names in PRUNE and refusing to descend past MAX_DEPTH. runtimes restricts which surfaces are looked for; the default looks for all of RUNTIMES. The result is sorted by path, so output order is deterministic across runs.
    """
    wanted = set(runtimes) if runtimes is not None else set(RUNTIMES)
    found: list[tuple[str, pathlib.Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in PRUNE]
        current = pathlib.Path(dirpath)
        name = current.name
        if "claude" in wanted and name == ".claude" and "settings.json" in filenames:
            found.append(("claude", current / "settings.json"))
        if "codex" in wanted and name == ".codex" and "config.toml" in filenames:
            found.append(("codex", current / "config.toml"))
        if "gemini" in wanted and name == ".gemini" and "settings.json" in filenames:
            found.append(("gemini", current / "settings.json"))
        if "opencode" in wanted:
            for filename in ("opencode.json", "opencode.jsonc"):
                if filename in filenames:
                    found.append(("opencode", current / filename))
        if len(current.relative_to(root).parts) >= MAX_DEPTH:
            dirnames[:] = []
    return sorted(found, key=lambda item: item[1])


def _load_json_object(
    path: pathlib.Path, preprocess: Callable[[str], str] | None = None
) -> tuple[dict | None, str]:
    """Read path and parse it as a JSON object, returning (data, error).

    error is empty on success, and data is a dict only when error is empty. An unreadable file, invalid JSON, and a JSON document that is not an object are all reported through error rather than raised, since every caller here is a handler that must never raise.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, str(exc)
    if preprocess is not None:
        text = preprocess(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, ""


def _write_if_different(
    path: pathlib.Path,
    existing: dict,
    desired: dict,
    dry_run: bool,
    actions: list[str],
    ordered_keys: tuple[str, ...] = (),
) -> None:
    """Record a write action, and unless dry_run perform it, when desired differs from existing.

    The comparison is by parsed content, not by serialized bytes, so a file that already renders the desired content produces no action, whatever its exact whitespace or key order happens to be. A key named in ordered_keys is compared with its order too, for a value whose order is part of its meaning (OpenCode's last-match permission object).
    """
    if existing == desired and all(
        same_in_order(existing.get(key), desired.get(key)) for key in ordered_keys
    ):
        return
    actions.append(f"write {short(path)}")
    if not dry_run:
        path.write_text(
            json.dumps(desired, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


_CODEX_HEAD_KEYS = re.compile(
    r"(?m)^(approval_policy|sandbox_mode|default_permissions)\s*="
)
_APPROVAL_POLICY_LINE = re.compile(r"(?m)^approval_policy\s*=.*$")
_SANDBOX_MODE_LINE = re.compile(r"(?m)^sandbox_mode\s*=.*\n?")
_DEFAULT_PERMISSIONS_LINE = re.compile(r"(?m)^default_permissions\s*=.*\n?")


def _split_codex_head(text: str) -> tuple[str, str]:
    """Split a Codex config.toml into its top-level head and its table-owned tail.

    The head is every line before the first line that opens a table (a line starting with a literal "["); the tail is that line onward. A key name that reappears inside a table in the tail is never touched, since only the head is top-level.
    """
    match = re.search(r"(?m)^\[", text)
    return (
        (text, "") if match is None else (text[: match.start()], text[match.start() :])
    )


def _rewrite_codex_head(head: str, policy: dict) -> str:
    """Rewrite the shared approval keys in a Codex config.toml head block."""
    line = f"approval_policy = {json.dumps(codex_approval_policy(policy))}"
    if _APPROVAL_POLICY_LINE.search(head):
        head = _APPROVAL_POLICY_LINE.sub(line, head, count=1)
    else:
        if head and not head.endswith("\n"):
            head += "\n"
        head += line + "\n"
    head = _SANDBOX_MODE_LINE.sub("", head)
    head = _DEFAULT_PERMISSIONS_LINE.sub("", head)
    return head


def _claude_handler(
    path: pathlib.Path, policy: dict, dry_run: bool, actions: list[str]
) -> None:
    """Rewrite a project-scope .claude/settings.json so it states the shared policy.

    Claude Code ignores bypassPermissions and auto when a project-scope file sets permissions.defaultMode, and starts the session in Manual mode instead; any other project-scope value there would override the user-scope mode. Either way the project-scope render never states a mode, so defaultMode is simply absent from claude_project_permissions' output. autoMode guidance is a user-scope concern only, so a project-scope autoMode key is removed rather than rewritten; carrying it forward here would hand the classifier the same rules twice. https://code.claude.com/docs/en/permission-modes
    """
    data, error = _load_json_object(path)
    if error:
        actions.append(f"skip {short(path)}: {error}")
        return
    if "permissions" not in data and "autoMode" not in data:
        return
    desired = dict(data)
    desired["permissions"] = claude_project_permissions(policy)
    desired.pop("autoMode", None)
    _write_if_different(path, data, desired, dry_run, actions)


def _codex_handler(
    path: pathlib.Path, policy: dict, dry_run: bool, actions: list[str]
) -> None:
    """Rewrite a project-scope .codex/config.toml so it states the shared policy.

    Text-based: only the head block (the top-level lines before the first table) is ever inspected or rewritten.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        actions.append(f"skip {short(path)}: {exc}")
        return
    head, tail = _split_codex_head(text)
    if not _CODEX_HEAD_KEYS.search(head):
        return
    rendered = _rewrite_codex_head(head, policy) + tail
    if rendered == text:
        return
    actions.append(f"write {short(path)}")
    if not dry_run:
        path.write_text(rendered, encoding="utf-8")


def _gemini_handler(
    path: pathlib.Path, policy: dict, dry_run: bool, actions: list[str]
) -> None:
    """Rewrite a project-scope .gemini/settings.json so it states the shared policy."""
    data, error = _load_json_object(path)
    if error:
        actions.append(f"skip {short(path)}: {error}")
        return
    general = data.get("general")
    if not isinstance(general, dict) or "defaultApprovalMode" not in general:
        return
    desired = dict(data)
    desired["general"] = dict(general)
    desired["general"]["defaultApprovalMode"] = gemini_approval_mode(policy)
    _write_if_different(path, data, desired, dry_run, actions)


def _opencode_handler(
    path: pathlib.Path, policy: dict, dry_run: bool, actions: list[str]
) -> None:
    """Rewrite a project-scope opencode.json or opencode.jsonc so it states the shared policy.

    Comments in a .jsonc file are lost on a rewrite, the same tradeoff the user-scope OpenCode adapter makes: nothing in this render path has anywhere to preserve them.

    OpenCode's key is "permission"; a legacy "permissions" array makes OpenCode 1.4.2 refuse the file (verified 2026-09-23, `opencode debug config`), so it is removed. This sweep used to do the reverse.
    """
    data, error = _load_json_object(path, strip_jsonc_comments)
    if error:
        actions.append(f"skip {short(path)}: {error}")
        return
    if "permissions" not in data and "permission" not in data:
        return
    desired = {key: value for key, value in data.items() if key != "permissions"}
    desired["permission"] = opencode_permission(policy)
    _write_if_different(path, data, desired, dry_run, actions, ordered_keys=("permission",))


_HANDLERS: dict[str, Callable[[pathlib.Path, dict, bool, list[str]], None]] = {
    "claude": _claude_handler,
    "codex": _codex_handler,
    "gemini": _gemini_handler,
    "opencode": _opencode_handler,
}

# One scoped project: its own policy and the explicit checkouts (root first, then worktrees) it applies to.
Scope = tuple[dict, list[pathlib.Path]]


def _declares_scope(directory: pathlib.Path) -> bool:
    """True when directory/permissions.json is a JSON object with a `projectScope` key.

    Read raw, before load_permissions validates it, so a project that carries a permissions file for another purpose is never touched by this sweep.
    """
    try:
        data = json.loads((directory / "permissions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "projectScope" in data


def _project_scope(name: str, directory: pathlib.Path, shared: dict) -> Scope:
    """Load one project's explicit scope without changing the shared policy."""
    policy = load_permissions(directory)
    scope = policy.get("projectScope", {})
    root = scope.get("root", "")
    worktrees = scope.get("worktrees", [])
    if not isinstance(root, str) or not pathlib.Path(root).is_absolute():
        raise ValueError(f"{name} scope requires an absolute root")
    if pathlib.Path(root).name != name:
        raise ValueError(f"{name} scope requires its exact named checkout")
    if not isinstance(worktrees, list) or not all(
        isinstance(p, str) for p in worktrees
    ):
        raise ValueError(f"{name} worktrees must be an explicit path list")
    paths = [pathlib.Path(root), *(pathlib.Path(p) for p in worktrees)]
    for path in paths:
        if not path.is_absolute() or path != path.resolve() or len(path.parts) < 4:
            raise ValueError(
                f"{name} scope rejects broad, relative or symlink paths"
            )
    if policy.get("additionalDirectories") or policy.get("runtimeDirectories"):
        raise ValueError(f"{name} scope cannot grant additional shared roots")
    if policy.get("defaultMode") != "default":
        raise ValueError(f"{name} scope cannot bypass approvals")
    # Shared denials are cumulative, even though shared grants are not inherited.
    policy["deny"] = list(dict.fromkeys(shared.get("deny", []) + policy["deny"]))
    return policy, paths


def _scoped_projects(source: pathlib.Path, shared: dict) -> dict[str, Scope]:
    """Every project under source/projects-root whose permissions.json declares a projectScope, by name."""
    base = source / "projects-root"
    if not base.is_dir():
        return {}
    return {
        directory.name: _project_scope(directory.name, directory, shared)
        for directory in sorted(base.iterdir(), key=lambda path: path.name)
        if directory.is_dir() and _declares_scope(directory)
    }


def _scoped_profile(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?ms)^\[permissions\.{re.escape(name)}(?:\.[^\]]*)?\]\n.*?(?=^\[|\Z)")


def _scoped_codex(name: str, text: str, policy: dict, project: pathlib.Path) -> str:
    """Use a separate read-only base with a single explicit writable checkout."""
    rendered = _scoped_profile(name).sub("", text)
    for key, value in (
        ("approval_policy", '"never"'),
        ("default_permissions", json.dumps(name)),
        ("sandbox_mode", None),
    ):
        rendered = set_top_level(rendered, key, value)
    lines = [
        f"[permissions.{name}]",
        'extends = ":read-only"',
        f"[permissions.{name}.filesystem]",
        f'{json.dumps(str(project))} = "write"',
    ]
    for path in filesystem_denies(policy, tools=("Read",)):
        if not path.startswith("**/"):
            lines.append(f'{json.dumps(path.removesuffix("/**"))} = "deny"')
    lines.append(f'[permissions.{name}.filesystem.":workspace_roots"]')
    for path in filesystem_denies(policy):
        if path.startswith("**/"):
            lines.append(f'{json.dumps(path)} = "deny"')
    lines.extend([f"[permissions.{name}.network]", "enabled = false"])
    return rendered.rstrip() + "\n\n" + "\n".join(lines) + "\n"


def _scoped_handler(
    name: str,
    runtime: str,
    path: pathlib.Path,
    policy: dict,
    project: pathlib.Path,
    dry_run: bool,
    actions: list[str],
) -> None:
    """Render the exception; malformed input never overwrites that target."""
    if path != path.resolve():
        raise ValueError(f"refusing symlink permission target: {path}")
    if runtime == "codex":
        existing = path.read_text() if path.exists() else ""
        tomllib.loads(existing)
        rendered = _scoped_codex(name, existing, policy, project)
        tomllib.loads(rendered)
        if rendered != existing:
            actions.append(f"write {short(path)}")
            if not dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered)
        return
    data, error = _load_json_object(path) if path.exists() else ({}, "")
    if error:
        raise ValueError(f"invalid project permission config {path}: {error}")
    desired = copy.deepcopy(data)
    if runtime == "claude":
        desired["permissions"] = claude_project_permissions(policy)
        desired["permissions"]["allow"] = [f"Read({project}/**)", f"Edit({project}/**)"]
        desired["permissions"]["defaultMode"] = "default"
        desired.pop("autoMode", None)
        sandbox = desired.setdefault("sandbox", {})
        sandbox.update(
            enabled=True,
            autoAllowBashIfSandboxed=False,
            allowUnsandboxedCommands=False,
            excludedCommands=[],
        )
        sandbox["filesystem"] = {
            "allowWrite": [],
            "denyRead": filesystem_denies(policy, tools=("Read",)),
            "denyWrite": filesystem_denies(policy, tools=("Edit",)),
        }
    elif runtime == "gemini":
        desired.setdefault("general", {})["defaultApprovalMode"] = "default"
        desired.setdefault("tools", {}).update(sandbox="docker", sandboxAllowedPaths=[])
        desired.setdefault("security", {}).update(disableYoloMode=True)
    else:
        # Do not imply unverified native sandbox support on other runtimes.
        return
    if desired != data:
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
        _write_if_different(path, data, desired, dry_run, actions)


def refuse_if_stale(
    source: pathlib.Path, target: pathlib.Path | None = None
) -> str | None:
    """A refusal message when source's policy is older than what is deployed, else None.

    Compares against the stamp the sync writes to ~/.claude by default, the fixed proxy for "what was last deployed on this machine": whichever checkout wrote that stamp last is the one whose policy this sweep must not undo. A stale checkout rewriting every project's permission files was the original failure this guards against, not only the global runtime dirs. `target` is overridable for tests.
    """
    policy = load_permissions(source)
    return refusal(target if target is not None else home() / ".claude", policy_version(policy), source)


def sweep(
    source: pathlib.Path,
    root: pathlib.Path | None = None,
    dry_run: bool = False,
    runtimes: Iterable[str] | None = None,
) -> list[str]:
    """Rewrite every project-scope runtime file under root that already carries permission settings.

    Loads the canonical policy from source/permissions.json. root defaults to the projects directory (see projects_dir). Returns an empty list, touching nothing, when the policy is empty or root is not a directory. Each candidate file is passed to its runtime's handler, and a handler never raises: an unreadable or invalid file becomes a skip action and the sweep moves on to the next candidate.

    A public target (source/projects-root/public-targets.json, read through agentrc.public_targets) is never rewritten on any surface: its permission files are its own, and the private policy must not land in a published tree. A list that exists and cannot be read raises RuntimeError before any file is touched, the refusal the project sync makes for the same file.
    """
    if root is None:
        root = projects_dir(source)
    policy = load_permissions(source)
    if not policy or not root.is_dir():
        return []
    try:
        public = load_public_targets(source)
    except PublicTargetsUnreadable as error:
        raise RuntimeError(f"refusing; {error}; no permission file was rewritten") from None
    root = root.resolve()
    actions: list[str] = []
    scoped = _scoped_projects(source, policy)
    scoped_by_path = {
        path: (name, scope_policy)
        for name, (scope_policy, paths) in scoped.items()
        for path in paths
    }
    wanted = set(runtimes) if runtimes is not None else set(RUNTIMES)
    found = candidates(root, runtimes)
    for project in scoped_by_path:
        if project.is_relative_to(root) and (project / ".git").exists():
            for runtime, filename in (
                ("claude", ".claude/settings.json"),
                ("codex", ".codex/config.toml"),
                ("gemini", ".gemini/settings.json"),
            ):
                candidate = (runtime, project / filename)
                if runtime in wanted and candidate not in found:
                    found.append(candidate)
    skipped_public: set[pathlib.Path] = set()
    for runtime, path in sorted(found, key=lambda item: item[1]):
        project = path.parent if runtime == "opencode" else path.parent.parent
        if project.name in public or is_public_checkout(project, public):
            if project not in skipped_public:
                skipped_public.add(project)
                actions.append(f"skip {short(project)}: public repository, its permission files are its own")
            continue
        if project in scoped_by_path:
            name, scope_policy = scoped_by_path[project]
            _scoped_handler(name, runtime, path, scope_policy, project, dry_run, actions)
        else:
            _HANDLERS[runtime](path, policy, dry_run, actions)
    return actions


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run the project-tier permission sweep."""
    parser = argparse.ArgumentParser(
        prog="agentrc project-permissions",
        description="Rewrite project-scope runtime permission files to match permissions.json.",
    )
    parser.add_argument(
        "--check", action="store_true", help="dry run, exit 1 if anything is stale"
    )
    parser.add_argument(
        "--only", choices=RUNTIMES, metavar="RUNTIME", help="sweep one runtime only"
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=None,
        help="the source root that holds permissions.json (default: $AGENTRC_SOURCE, then the nearest agentrc.toml, then the current directory)",
    )
    parser.add_argument(
        "--projects-root",
        type=pathlib.Path,
        default=None,
        help="the directory to walk (default: $LLM_ROOT_PROJECTS_DIR, then agentrc.toml, then ~/projects)",
    )
    args = parser.parse_args(argv)

    source = source_root(args.root)
    walk = args.projects_root if args.projects_root is not None else projects_dir(source)

    stale = refuse_if_stale(source)
    if stale is not None:
        print(f"sync-permissions: {stale}", file=sys.stderr)
        return 2

    runtimes = (args.only,) if args.only else None
    try:
        actions = sweep(source, root=walk, dry_run=args.check, runtimes=runtimes)
    except RuntimeError as error:
        print(f"sync-permissions: {error}", file=sys.stderr)
        return 2

    if not actions:
        print("sync-permissions: current")
        return 0

    verb = "would" if args.check else "did"
    print(f"sync-permissions ({short(walk)}) {verb}:")
    for action in actions:
        print(f"  {action}")
    return 1 if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
