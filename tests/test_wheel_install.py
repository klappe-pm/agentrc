from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

import pytest

from conftest import REPO_ROOT, tree_snapshot

TEMPLATE = REPO_ROOT / "stratarc" / "data" / "templates" / "source-root"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, f"{cmd} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


@pytest.mark.slow
def test_wheel_install_scaffolds_template(tmp_path: Path):
    assert TEMPLATE.is_dir(), f"template directory missing: {TEMPLATE}"

    wheel_dir = tmp_path / "wheels"
    _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(REPO_ROOT)])
    wheels = list(wheel_dir.glob("stratarc-*.whl"))
    assert len(wheels) == 1, wheels

    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(env_dir)
    bin_dir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    _run([str(bin_dir / "python"), "-m", "pip", "install", "--no-deps", str(wheels[0])])

    target = tmp_path / "scaffold"
    # Run outside the checkout so the installed package, not the source tree, is imported.
    _run([str(bin_dir / "stratarc"), "init", str(target)], cwd=tmp_path)

    expected = tree_snapshot(TEMPLATE)
    assert expected, "template tree is empty"
    actual = tree_snapshot(target)
    assert sorted(actual) == sorted(expected)
    for rel, data in expected.items():
        assert actual[rel] == data, f"content differs: {rel}"
