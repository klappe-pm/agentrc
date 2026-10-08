from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentrc import __version__
from agentrc.cli import STUBS, TEMPLATE, main
from agentrc.resources import data_dir
from conftest import REPO_ROOT, tree_snapshot


def _template_root() -> Path:
    # Inside a checkout the Traversable is a filesystem path.
    return Path(str(data_dir(TEMPLATE)))


@pytest.mark.parametrize("name", STUBS)
def test_stub_exits_2(name, capsys):
    assert main([name]) == 2
    assert capsys.readouterr().err.strip() == f"agentrc {name}: not yet extracted"


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_python_dash_m_entry():
    result = subprocess.run(
        [sys.executable, "-m", "agentrc", "sync"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "agentrc sync: not yet extracted" in result.stderr


def test_init_copies_template_tree(tmp_path: Path):
    template = _template_root()
    assert template.is_dir(), f"template directory missing: {template}"
    target = tmp_path / "source"

    assert main(["init", str(target)]) == 0

    expected = tree_snapshot(template)
    assert expected, "template tree is empty"
    assert tree_snapshot(target) == expected


def test_init_into_existing_empty_dir(tmp_path: Path):
    template = _template_root()
    assert template.is_dir(), f"template directory missing: {template}"
    assert main(["init", str(tmp_path)]) == 0
    assert tree_snapshot(tmp_path) == tree_snapshot(template)


def test_init_refuses_non_empty_target(tmp_path: Path, capsys):
    (tmp_path / "keep.txt").write_text("existing")

    assert main(["init", str(tmp_path)]) == 1
    assert "not empty" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]
