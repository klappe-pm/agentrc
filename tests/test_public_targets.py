"""Parity of the two public-target readers.

hooks/lib/public-targets.py (imported through stratarc.public_targets) is the one definition of which checkouts are public. hooks/lib/attribution-detect.py carries a local copy of the same logic because the carried guard ships alone in a project's tracked .claude/hooks/lib. These tests hold the two to the same answers on the same fixtures: a plain checkout named after the target, a linked worktree of a target repository, a checkout whose origin slug matches owner/repo, a non-target, and a present but unreadable list.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stratarc import public_targets
from stratarc.resources import data_dir


def _load_detector():
    path = Path(str(data_dir("hooks/lib/attribution-detect.py")))
    spec = importlib.util.spec_from_file_location("attribution_detect_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


detect = _load_detector()


def _git(*args: str) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        env=environment,
    )


def _checkout(path: Path, origin: str | None = None) -> Path:
    _git("init", "-q", str(path))
    if origin:
        _git("-C", str(path), "remote", "add", "origin", origin)
    return path


class Fixture:
    def __init__(self, tmp: Path, source: Path):
        self.tmp = tmp
        self.source = source
        (source / "projects-root").mkdir(parents=True, exist_ok=True)
        self.projects = tmp / "projects"
        self.projects.mkdir()

    def write_list(self, text: str) -> None:
        (self.source / "projects-root" / "public-targets.json").write_text(text, encoding="utf-8")

    def engine(self, file: Path) -> bool:
        """The answer of hooks/lib/public-targets.py for a file inside a checkout."""
        return public_targets.is_public_checkout(file.parent, public_targets.load_public_targets(self.source))

    def carried(self, file: Path) -> bool:
        """The answer of the copy inside attribution-detect.py for the same file."""
        return detect._public_checkout({"file_path": str(file)}, str(self.tmp))

    def checkouts(self) -> dict[str, tuple[Path, bool]]:
        """Each fixture: a file path inside a checkout, and whether {"stratarc", "owner/repo"} makes it public."""
        plain = _checkout(self.projects / "stratarc", "https://github.com/someone/other.git")
        _git("-C", str(plain), "commit", "-q", "--allow-empty", "-m", "base")
        worktree = self.projects / "stratarc-feature-wt"
        _git("-C", str(plain), "worktree", "add", "-q", "-b", "feature", str(worktree))
        slug = _checkout(self.projects / "renamed-clone", "git@github.com:Owner/Repo.git")
        slug_worktree_base = _checkout(self.projects / "other-name", "https://github.com/owner/repo")
        _git("-C", str(slug_worktree_base), "commit", "-q", "--allow-empty", "-m", "base")
        slug_worktree = self.projects / "slug-wt"
        _git("-C", str(slug_worktree_base), "worktree", "add", "-q", "-b", "feature", str(slug_worktree))
        private = _checkout(self.projects / "demo", "https://github.com/fixture/demo.git")
        for directory in (plain, worktree, slug, slug_worktree, private):
            (directory / "docs").mkdir(exist_ok=True)
        return {
            "plain checkout named after the target": (plain / "docs" / "note.md", True),
            "linked worktree of the target repository": (worktree / "docs" / "note.md", True),
            "checkout whose origin slug matches": (slug / "docs" / "note.md", True),
            "linked worktree of a slug-matching repository": (slug_worktree / "docs" / "note.md", True),
            "non-target": (private / "docs" / "note.md", False),
        }


@pytest.fixture
def fx(tmp_path, source_root, monkeypatch) -> Fixture:
    monkeypatch.delenv("STRATARC_PUBLIC", raising=False)
    monkeypatch.delenv("LLM_ROOT", raising=False)
    return Fixture(tmp_path.resolve(), source_root)


def test_both_readers_resolve_the_same_identity_for_every_fixture(fx):
    fx.write_list('{"targets": ["stratarc", "OWNER/repo"]}\n')
    for label, (file, expected) in fx.checkouts().items():
        assert fx.engine(file) == expected, label
        assert fx.carried(file) == expected, label


def test_a_linked_worktree_is_public_only_through_its_main_worktree_name(fx):
    """The worktree directory is not named in the list; its main worktree is. Reading only the toplevel's own name would call it private in both readers."""
    fx.write_list('["stratarc"]\n')
    worktree_file = fx.checkouts()["linked worktree of the target repository"][0]
    assert worktree_file.parent.parent.name not in {"stratarc"}
    assert fx.engine(worktree_file)
    assert fx.carried(worktree_file)
    names, _slug = public_targets.checkout_identity(worktree_file.parent)
    assert names == frozenset({"stratarc-feature-wt", "stratarc"})


@pytest.mark.parametrize(
    "label",
    ["checkout whose origin slug matches", "linked worktree of a slug-matching repository"],
)
def test_the_slug_comparison_ignores_case_in_both_readers(fx, label):
    fx.write_list('["owner/REPO"]\n')
    file = fx.checkouts()[label][0]
    assert fx.engine(file)
    assert fx.carried(file)


def test_a_missing_list_declares_nothing_in_both_readers(fx):
    for label, (file, _expected) in fx.checkouts().items():
        assert not fx.engine(file), label
        assert not fx.carried(file), label


@pytest.mark.parametrize("broken", ["{not json", '"stratarc"', '{"targets": "stratarc"}', '{"targets": [1]}', "[1]", '[""]'])
def test_an_unreadable_list_raises_in_the_module_and_fails_closed_in_the_detector(fx, broken):
    checkouts = fx.checkouts()
    fx.write_list(broken)
    with pytest.raises(public_targets.PublicTargetsUnreadable, match="public-targets.json"):
        public_targets.load_public_targets(fx.source)
    assert detect._public_targets() is None
    for label, (file, _expected) in checkouts.items():
        # Fail closed: no checkout, the non-target included, keeps the exemption.
        assert fx.carried(file), label


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a mode 0 file")
def test_a_list_that_exists_but_cannot_be_opened_fails_closed_in_both(fx):
    fx.write_list('["stratarc"]\n')
    path = fx.source / "projects-root" / "public-targets.json"
    path.chmod(0)
    try:
        private = fx.checkouts()["non-target"][0]
        with pytest.raises(public_targets.PublicTargetsUnreadable):
            public_targets.load_public_targets(fx.source)
        assert fx.carried(private)
    finally:
        path.chmod(0o644)


def test_both_readers_agree_on_a_list_object_with_a_comment_key(fx):
    fx.write_list(json.dumps({"$comment": "fixture", "targets": ["stratarc"]}))
    assert public_targets.load_public_targets(fx.source) == frozenset({"stratarc"})
    assert detect._public_targets() == frozenset({"stratarc"})


def test_the_module_loads_the_reader_from_package_data():
    """The implementation is the packaged hooks/lib file, found through importlib.resources and not through the module's own location."""
    assert Path(public_targets._module.__file__).as_posix().endswith("stratarc/data/hooks/lib/public-targets.py")
    for name in public_targets.__all__:
        assert hasattr(public_targets, name)
