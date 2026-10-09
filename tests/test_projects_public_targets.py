"""Public-target identity in the project sync.

A public target is judged by its directory name, its main worktree's name or its origin's owner/repo slug, so a linked worktree of a public repository or a renamed clone of one receives nothing and is not reported as unregistered.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from stratarc import projects


def git(*args: str) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        env=environment,
    )


class NoRows:
    """The slice of a control plane that verify() reads, with no rows."""

    manifest: dict = {}

    def status(self, name: str) -> str:
        raise AssertionError(name)

    def find_checkout(self, name: str, base: Path) -> list[Path]:
        return []


class Tree:
    def __init__(self, source: Path, projects_dir: Path):
        self.source = source
        self.projects = projects_dir

    def declare(self, text: str) -> None:
        (self.source / "projects-root" / "public-targets.json").write_text(text, encoding="utf-8")

    def checkout(self, name: str, origin: str | None = None) -> Path:
        path = self.projects / "active" / name
        git("init", "-q", str(path))
        if origin:
            git("-C", str(path), "remote", "add", "origin", origin)
        return path

    def worktree_of(self, main: Path, name: str) -> Path:
        git("-C", str(main), "commit", "-q", "--allow-empty", "-m", "base")
        path = self.projects / "active" / name
        git("-C", str(main), "worktree", "add", "-q", "-b", name, str(path))
        assert (path / ".git").is_file()
        return path


@pytest.fixture
def tree(tmp_path, monkeypatch, stratarc_home):
    source = tmp_path.resolve() / "source"
    (source / "projects-root").mkdir(parents=True)
    projects_dir = tmp_path.resolve() / "projects"
    (projects_dir / "active").mkdir(parents=True)
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(projects_dir))
    monkeypatch.delenv("STRATARC_SOURCE", raising=False)
    for name in ("ROOT", "PROJECTS_SRC", "RETIRED_RULES", "RETIRED_HOOKS", "_ROOT_CONFIGURED"):
        monkeypatch.setattr(projects, name, getattr(projects, name))
    projects.configure_root(source)
    return Tree(source, projects_dir)


def test_a_linked_worktree_of_a_listed_repository_is_public_under_another_name(tree):
    tree.declare('{"targets": ["public-lib"]}\n')
    main = tree.checkout("public-lib")
    worktree = tree.worktree_of(main, "public-lib-feature")
    private = tree.checkout("demo", "https://github.com/fixture/demo.git")
    assert projects._public_checkout(main)
    assert projects._public_checkout(worktree)
    assert not projects._public_checkout(private)


def test_a_checkout_whose_origin_slug_is_listed_is_public_whatever_it_is_called(tree):
    tree.declare('["Fixture/Renamed"]\n')
    clone = tree.checkout("renamed-clone", "git@github.com:fixture/renamed.git")
    private = tree.checkout("demo", "https://github.com/fixture/demo.git")
    assert projects._public_checkout(clone)
    assert not projects._public_checkout(private)


def test_resolve_with_no_control_plane_refuses_a_worktree_of_a_public_repository(tree):
    tree.declare('["public-lib"]\n')
    main = tree.checkout("public-lib")
    tree.worktree_of(main, "public-lib-feature")
    path, reason = projects.resolve("public-lib-feature", None)
    assert path is None
    assert "checkout of a public repository" in reason
    tree.checkout("demo", "https://github.com/fixture/demo.git")
    path, reason = projects.resolve("demo", None)
    assert (path, reason) == (tree.projects / "active" / "demo", "")


def test_verify_does_not_report_a_public_worktree_or_slug_clone_as_unregistered(tree):
    tree.declare('["public-lib", "fixture/renamed"]\n')
    main = tree.checkout("public-lib")
    tree.worktree_of(main, "public-lib-feature")
    tree.checkout("renamed-clone", "https://github.com/fixture/renamed.git")
    tree.checkout("stray", "https://github.com/fixture/stray.git")
    drift = projects.verify(NoRows())
    assert [line.split(":")[0] for line in drift] == ["stray"], drift


@pytest.mark.parametrize("broken", ["{not json", '{"targets": [1]}', '"public-lib"'])
def test_an_unreadable_list_raises_runtime_error_naming_the_file(tree, broken):
    tree.checkout("public-lib")
    tree.declare(broken)
    with pytest.raises(RuntimeError, match="public-targets.json"):
        projects.public_targets()
    with pytest.raises(RuntimeError, match="public-targets.json"):
        projects._public_checkout(tree.projects / "active" / "public-lib")
