from __future__ import annotations

import pytest

from stratarc.config import Config, ConfigError, load_config
from conftest import REPO_ROOT

TEMPLATE = REPO_ROOT / "stratarc" / "data" / "templates" / "source-root"
EXAMPLE = REPO_ROOT / "examples" / "notes-cli" / "source"
RUNTIMES = {"claude", "codex", "gemini", "cursor", "opencode"}


@pytest.fixture(autouse=True)
def fake_home(monkeypatch, tmp_path):
    monkeypatch.delenv("STRATARC_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def write(root, text):
    (root / "stratarc.toml").write_text(text, encoding="utf-8")


def test_missing_file_yields_defaults(tmp_path):
    assert load_config(tmp_path) == Config()
    assert load_config(tmp_path).owner == ""


def test_template_shape(tmp_path):
    config = load_config(TEMPLATE)
    assert set(config.runtimes) == RUNTIMES
    assert config.runtimes["claude"].enabled is True
    assert config.runtimes["codex"].enabled is False
    assert config.runtimes["claude"].target == tmp_path / "home" / ".claude"
    assert config.runtimes["opencode"].target == tmp_path / "home" / ".config" / "opencode"
    assert config.projects_root == tmp_path / "home" / "projects"
    assert config.owner == ""


def test_example_shape():
    config = load_config(EXAMPLE)
    assert set(config.runtimes) == RUNTIMES
    assert all(runtime.enabled for runtime in config.runtimes.values())
    assert config.owner == "example-owner"


def test_tilde_follows_stratarc_home(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "redirected"))
    write(tmp_path, 'projects_root = "~/work"\n[runtimes.claude]\nenabled = true\ntarget = "~/.claude"\n')
    config = load_config(tmp_path)
    assert config.projects_root == tmp_path / "redirected" / "work"
    assert config.runtimes["claude"].target == tmp_path / "redirected" / ".claude"


def test_absolute_paths_are_kept(tmp_path):
    write(tmp_path, 'projects_root = "/srv/checkouts"\n')
    assert str(load_config(tmp_path).projects_root) == "/srv/checkouts"


def test_runtime_defaults(tmp_path):
    write(tmp_path, "[runtimes.codex]\n")
    runtime = load_config(tmp_path).runtimes["codex"]
    assert runtime.enabled is False
    assert runtime.target is None


@pytest.mark.parametrize(
    "text",
    [
        "owner = [",
        "owner = 3\n",
        'projects_root = ""\n',
        "projects_root = 4\n",
        "runtimes = 1\n",
        "[runtimes]\nclaude = 1\n",
        '[runtimes.claude]\nenabled = "yes"\n',
        "[runtimes.claude]\ntarget = 5\n",
    ],
)
def test_malformed_file_names_the_path(tmp_path, text):
    write(tmp_path, text)
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path)
    assert str(tmp_path / "stratarc.toml") in str(caught.value)
