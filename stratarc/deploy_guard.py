"""Guard a deploy of the shared configuration against a stale source.

Every runtime root and every project runtime directory that receives the
permission policy also receives a stamp file, `.<name>-deploy.json` (`<name>` is the engine name, `stratarc` by default),
naming the policy version, the source checkout, its branch and commit, and
the time of the write. Before a deploy writes anything, the stamp already
there is compared with the incoming policy: a deploy that would replace a
newer policy version with an older one is refused.

The stamp exists because a sync deploys whatever the checkout it runs in
holds. With several checkouts and sessions alive, the last one to write
won, silently, and a merged permission change was reverted three times in
one afternoon by commits that never mentioned permissions. The stamp turns
that silent loss into a refusal that names both versions.

A second guard keys on the branch, and applies only to a source root that
opts in by declaring environments. components.json declares each environment
under `environments` with the ref it deploys from (for example a container
that deploys `main` and a workstation that deploys `stable`). Which
environment a checkout is in is selected by STRATARC_ENVIRONMENT, or else by
the checkout's own `git config <name>.environment`. A checkout sitting on a
branch other than the environment's ref refuses to deploy unless the caller
says so explicitly. A checkout that is not a git repository has no branch and
is not refused.

With no environment selected and none declared there is no branch guard:
deploy_ref returns None and `ref` prints nothing and exits 0. With
environments declared but none selected, the checkout deploys `main`. A
selected environment that components.json does not declare is an error
(UnknownEnvironment), never a guess.

The deploy record: a successful full deploy writes the commit it deployed to
~/.agent-hooks/state/<name>-deploy.json, only after a deploy succeeded and
never by a failed one. Its first_deployed is when that commit was first
deployed here, kept across redeploys and restarts, which is where a promotion
soak starts.

    python3 -m stratarc.deploy_guard ref [--root PATH]   print the ref to deploy, if any
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

from stratarc.paths import engine_name, source_root

DEFAULT_BRANCHES = ("main",)
ENVIRONMENT_VARIABLE = "STRATARC_ENVIRONMENT"


def stamp_name(root: Path | None = None) -> str:
    """The stamp file name, `.<engine name>-deploy.json`."""
    return f".{engine_name(root)}-deploy.json"


def environment_config(root: Path | None = None) -> str:
    """The git config key that selects an environment, `<engine name>.environment`."""
    return f"{engine_name(root)}.environment"


def deploy_record(root: Path | None = None) -> Path:
    """The deploy record path relative to home, `.agent-hooks/state/<engine name>-deploy.json`."""
    return Path(".agent-hooks") / "state" / f"{engine_name(root)}-deploy.json"


class UnknownEnvironment(ValueError):
    """An environment is selected that components.json does not declare."""


def read_stamp(target: Path, root: Path | None = None) -> dict | None:
    """The parsed stamp under target, or None when absent or unreadable."""
    path = target / stamp_name(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def deployed_version(target: Path, root: Path | None = None) -> int:
    """The policy version the stamp under target records; 0 when there is none."""
    stamp = read_stamp(target, root)
    if not stamp:
        return 0
    version = stamp.get("policyVersion", 0)
    return version if isinstance(version, int) and version >= 0 else 0


def refusal(target: Path, version: int, root: Path | None = None) -> str | None:
    """A message when the deployed policy under target is newer than version, else None."""
    current = deployed_version(target, root)
    if current > version:
        stamp = read_stamp(target, root) or {}
        origin = stamp.get("source", "an unknown checkout")
        return (
            f"deployed policy version {current} (from {origin}) is newer than the "
            f"source's version {version}; refusing to write an older policy over it"
        )
    return None


def _git(root: Path, *args: str) -> str | None:
    """The stripped output of a git command at root, None when it fails or prints nothing."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def source_branch(root: Path) -> str | None:
    """The branch checked out at root, None when root is not a git checkout or is detached."""
    return _git(root, "symbolic-ref", "--short", "-q", "HEAD")


def source_commit(root: Path, *, full: bool = False) -> str | None:
    """The commit checked out at root, None when root is not a git checkout."""
    return _git(root, "rev-parse", "HEAD") if full else _git(root, "rev-parse", "--short", "HEAD")


def is_git_checkout(root: Path) -> bool:
    """Whether root is inside a git work tree at all."""
    return _git(root, "rev-parse", "--is-inside-work-tree") == "true"


def selected_environment(root: Path) -> str | None:
    """The environment root deploys as: STRATARC_ENVIRONMENT, else the checkout's git setting, else None."""
    named = os.environ.get(ENVIRONMENT_VARIABLE, "").strip()
    return named or _git(root, "config", "--local", "--get", environment_config(root))


def declared_environments(root: Path) -> dict:
    """The environments components.json at root declares; empty when it declares none."""
    try:
        document = json.loads((root / "components.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    environments = document.get("environments") if isinstance(document, dict) else None
    return environments if isinstance(environments, dict) else {}


def deploy_ref(root: Path) -> str | None:
    """The ref root deploys from, or None when there is no branch guard.

    Returns None when no environment is selected and components.json declares
    none: the source root has not opted into a branch guard, so callers apply
    none. Returns the selected environment's ref when one is selected. Returns
    main when environments are declared but none is selected.

    Raises UnknownEnvironment when the selected environment is not declared
    with a ref, since deploying under a name nothing defines would guess.
    """
    name = selected_environment(root)
    environments = declared_environments(root)
    if name is None:
        return DEFAULT_BRANCHES[0] if environments else None
    entry = environments.get(name)
    ref = entry.get("ref") if isinstance(entry, dict) else None
    if not isinstance(ref, str) or not ref:
        known = ", ".join(sorted(environments)) or "none"
        raise UnknownEnvironment(
            f"environment {name} (from {ENVIRONMENT_VARIABLE} or git config {environment_config(root)}) "
            f"is not declared with a ref in components.json environments (declared: {known})"
        )
    return ref


def deploy_record_path(home: Path, root: Path | None = None) -> Path:
    """Where the last successful deploy is recorded under home."""
    return home / deploy_record(root)


def read_deploy_record(home: Path, root: Path | None = None) -> dict | None:
    """The last successful deploy under home, None when absent, unreadable or without a commit."""
    try:
        data = json.loads(deploy_record_path(home, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("commit"), str) or not data["commit"]:
        return None
    return data


def source_is_clean(source: Path) -> bool:
    """Whether tracked, untracked and hidden source changes are absent."""
    try:
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        flags = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-v", "-z"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    hidden_changes = flags.returncode != 0 or any(
        entry[0].islower() or entry[0] == "S" for entry in flags.stdout.split("\0") if entry
    )
    return status.returncode == 0 and not status.stdout and not hidden_changes


def write_deploy_record(home: Path, source: Path, environment: str | None) -> Path | None:
    """Record a deployed commit only when the source tree matches it."""
    commit = source_commit(source, full=True)
    if commit is None:
        return None
    path = deploy_record_path(home, source)
    if not source_is_clean(source):
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    written = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    # first_deployed is when this commit was first deployed here; a redeploy
    # or a restart of the same commit keeps it, so the promotion soak (S-02)
    # is never reset. A record from before the key existed gives its written.
    previous = read_deploy_record(home, source)
    first = written
    if previous is not None and previous["commit"] == commit:
        kept = previous.get("first_deployed") or previous.get("written")
        if isinstance(kept, str) and kept:
            first = kept
    payload = {
        "commit": commit,
        "branch": source_branch(source),
        "environment": environment,
        "source": str(source),
        "written": written,
        "first_deployed": first,
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def branch_refusal(root: Path, allowed: tuple[str, ...] = DEFAULT_BRANCHES) -> str | None:
    """A message when root is a git checkout on a branch outside allowed, else None.

    A detached head counts as outside: nothing says which branch its tree
    came from, so nothing says the tree is the one the machine should run.
    """
    if not is_git_checkout(root):
        return None
    branch = source_branch(root)
    if branch in allowed:
        return None
    where = f"branch {branch}" if branch else "a detached head"
    return (
        f"source checkout {root} is on {where}; the shared configuration deploys "
        f"from {', '.join(allowed)} only (pass --allow-branch to override)"
    )


def unpromoted_refusal(root: Path, ref: str, remote: str = "origin") -> str | None:
    """Refuse a checkout whose commit is not the remote's promoted ref."""
    remote_ref = f"{remote}/{ref}"
    promoted = _git(root, "ls-remote", remote, f"refs/heads/{ref}")
    if promoted is None:
        return f"cannot verify {remote_ref}; refusing to deploy an unverified commit"
    target = promoted.split()[0]
    head = source_commit(root, full=True)
    if head != target:
        return f"checkout commit {head or 'unknown'} differs from {remote_ref} {target}; only a promoted commit deploys"
    if not source_is_clean(root):
        return f"source checkout {root} has local changes; only the promoted commit's clean tree deploys"
    return None


def write_stamp(target: Path, version: int, source: Path) -> Path:
    """Write the stamp under target and return its path."""
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "policyVersion": version,
        "source": str(source),
        "branch": source_branch(source),
        "commit": source_commit(source),
        "written": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    path = target / stamp_name(source)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """`ref`: print the ref the checkout deploys from, for the post-commit git hook; print nothing when there is no branch guard."""
    parser = argparse.ArgumentParser(prog="deploy_guard")
    sub = parser.add_subparsers(dest="command", required=True)
    ref = sub.add_parser("ref", help="print the ref this checkout's environment deploys from")
    ref.add_argument("--root", type=Path, default=None, help="source root (default: the resolved source root)")
    args = parser.parse_args(argv)
    root = source_root(args.root)
    try:
        ref_name = deploy_ref(root)
    except UnknownEnvironment as error:
        print(f"deploy_guard: {error}", file=sys.stderr)
        return 2
    if ref_name is not None:
        print(ref_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
