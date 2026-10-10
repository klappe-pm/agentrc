"""The shell and bun collector in conftest must report a skipped or failed external test, not crash pytest.

A runner without bun marks every .test.ts item as skipped. Reporting a skip needs a line number for the item, and a missing one made pytest abort with an internal error that hid every result after it. The machine this was written on has bun installed, so the case is run in a subprocess whose PATH has none.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def run_pytest(tmp_path: Path, filename: str, body: str) -> subprocess.CompletedProcess[str]:
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copy(CONFTEST, tests / "conftest.py")
    (tests / filename).write_text(body, encoding="utf-8")
    bash = shutil.which("bash")
    assert bash is not None
    # The directory holding bash (/bin or /usr/bin) has no bun, so shell tests run and bun tests skip.
    bash_dir = str(Path(bash).parent)
    assert shutil.which("bun", path=bash_dir) is None
    env = {"PATH": bash_dir, "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", str(tests / filename)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_bun_test_is_skipped_when_bun_is_missing(tmp_path: Path) -> None:
    result = run_pytest(tmp_path, "x.test.ts", "export {};\n")
    assert "INTERNALERROR" not in result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout


def test_a_shell_test_that_exits_77_is_skipped_with_its_reason(tmp_path: Path) -> None:
    result = run_pytest(
        tmp_path,
        "other-os.test.sh",
        "#!/usr/bin/env bash\necho 'needs a tool this machine lacks' >&2\nexit 77\n",
    )
    assert "INTERNALERROR" not in result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert "needs a tool this machine lacks" in result.stdout + result.stderr


def test_a_failing_shell_test_is_reported_as_a_failure(tmp_path: Path) -> None:
    result = run_pytest(tmp_path, "boom.test.sh", "#!/usr/bin/env bash\necho only-the-test-prints-this\nexit 1\n")
    assert "INTERNALERROR" not in result.stdout + result.stderr
    assert result.returncode == 1
    assert "1 failed" in result.stdout
    assert "exited 1" in result.stdout
    assert "FileNotFoundError" not in result.stdout
