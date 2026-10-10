from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# An external test exits with this status to be skipped, not failed.
SKIP_STATUS = 77

# Make the checkout importable without installing it.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Map every file under ``root`` (relative POSIX path) to its bytes."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


@pytest.fixture(autouse=True)
def no_engine_name_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an ambient STRATARC_NAME from changing the default name a test expects."""
    monkeypatch.delenv("STRATARC_NAME", raising=False)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def stratarc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary home that the engine reads through ``STRATARC_HOME``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("STRATARC_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary source root that the engine reads through ``STRATARC_SOURCE``."""
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setenv("STRATARC_SOURCE", str(root))
    return root


class ExternalTestFailure(Exception):
    """A shell or bun test exited non-zero."""


class ExternalTestItem(pytest.Item):
    """One test file run as a subprocess; it fails on a non-zero exit."""

    command: str
    marker: str

    def __init__(self, *, command: list[str], marker: str, **kwargs):
        super().__init__(**kwargs)
        self.command = command
        self.add_marker(marker)

    def runtest(self) -> None:
        result = subprocess.run(
            self.command, cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
        if result.returncode == SKIP_STATUS:
            # A test that cannot run on this machine says why on stderr and exits 77, the status make and automake use.
            pytest.skip((result.stderr or result.stdout).strip() or "skipped by the test")
        if result.returncode != 0:
            raise ExternalTestFailure(
                f"{' '.join(self.command)} exited {result.returncode}\n"
                f"{result.stdout}{result.stderr}"
            )

    def repr_failure(self, excinfo, style=None):
        if excinfo.errisinstance(ExternalTestFailure):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style)

    def reportinfo(self):
        # pytest builds the report for a skipped or failed item from this line number and asserts it is not None.
        return self.path, 0, self.name


class ExternalTestFile(pytest.File):
    def collect(self):
        if self.path.name.endswith(".test.sh"):
            yield ExternalTestItem.from_parent(
                self, name=self.path.name, command=["bash", str(self.path)], marker="shell"
            )
        elif self.path.name.endswith(".test.ts"):
            item = ExternalTestItem.from_parent(
                self, name=self.path.name, command=["bun", "test", str(self.path)], marker="bun"
            )
            if shutil.which("bun") is None:
                item.add_marker(pytest.mark.skip(reason="bun is not on PATH"))
            yield item


def pytest_collect_file(file_path: Path, parent):
    if file_path.name.endswith((".test.sh", ".test.ts")):
        return ExternalTestFile.from_parent(parent, path=file_path)
    return None
