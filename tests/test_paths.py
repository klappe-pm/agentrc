from __future__ import annotations

from pathlib import Path

import pytest

from agentrc import paths

VARIABLES = ("AGENTRC_HOME", "AGENTRC_SOURCE", "LLM_ROOT_PROJECTS_DIR", "AGENTRC_GITHUB_OWNER", "AGENTRC_NAME")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)


def test_home_prefers_agentrc_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTRC_HOME", str(tmp_path / "override"))
    assert paths.home() == tmp_path / "override"


def test_home_falls_back_to_home(tmp_path):
    assert paths.home() == tmp_path / "home"


def test_home_ignores_empty_agentrc_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTRC_HOME", "  ")
    assert paths.home() == tmp_path / "home"


def test_home_falls_back_to_platform_home(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: cls("/platform-home")))
    assert paths.home() == Path("/platform-home")


def test_source_root_explicit_beats_environment(monkeypatch, tmp_path):
    (tmp_path / "flag").mkdir()
    monkeypatch.setenv("AGENTRC_SOURCE", str(tmp_path / "env"))
    assert paths.source_root(tmp_path / "flag") == (tmp_path / "flag").resolve()


def test_source_root_environment_beats_discovery(monkeypatch, tmp_path):
    (tmp_path / "agentrc.toml").write_text("")
    (tmp_path / "env").mkdir()
    monkeypatch.setenv("AGENTRC_SOURCE", str(tmp_path / "env"))
    assert paths.source_root() == (tmp_path / "env").resolve()


def test_source_root_discovers_upward(monkeypatch, tmp_path):
    root = tmp_path / "src"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (root / "agentrc.toml").write_text("")
    monkeypatch.chdir(nested)
    assert paths.source_root() == root.resolve()


def test_source_root_nearest_config_wins(monkeypatch, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / "agentrc.toml").write_text("")
    (inner / "agentrc.toml").write_text("")
    monkeypatch.chdir(inner)
    assert paths.source_root() == inner.resolve()


def test_source_root_defaults_to_cwd(monkeypatch, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    assert paths.source_root() == bare.resolve()


def test_projects_root_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "work"))
    assert paths.projects_root() == tmp_path / "work"


def test_projects_root_default_follows_home(monkeypatch, tmp_path):
    assert paths.projects_root() == tmp_path / "home" / "projects" / "active"
    monkeypatch.setenv("AGENTRC_HOME", str(tmp_path / "other"))
    assert paths.projects_root() == tmp_path / "other" / "projects" / "active"


def test_projects_root_explicit_beats_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "env"))
    assert paths.projects_root(tmp_path / "flag") == tmp_path / "flag"


def test_projects_root_environment_beats_config(monkeypatch, tmp_path):
    (tmp_path / "agentrc.toml").write_text('projects_root = "/from/config"\n')
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "env"))
    assert paths.projects_root() == tmp_path / "env"


def test_projects_root_reads_config(tmp_path):
    (tmp_path / "agentrc.toml").write_text('projects_root = "/from/config"\n')
    assert paths.projects_root() == Path("/from/config")


def test_projects_root_config_expands_tilde_against_agentrc_home(monkeypatch, tmp_path):
    (tmp_path / "agentrc.toml").write_text('projects_root = "~/work"\n')
    monkeypatch.setenv("AGENTRC_HOME", str(tmp_path / "other"))
    assert paths.projects_root() == tmp_path / "other" / "work"


def test_projects_root_reads_config_of_explicit_root(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "agentrc.toml").write_text('projects_root = "/elsewhere"\n')
    assert paths.projects_root(root=other) == Path("/elsewhere")


def test_projects_dir_default_lifts_the_active_folder_to_its_parent(tmp_path):
    assert paths.projects_dir() == tmp_path / "home" / "projects"


def test_projects_dir_uses_the_environment_value_as_given(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "work"))
    assert paths.projects_dir() == tmp_path / "work"


def test_projects_dir_reads_the_configuration_of_the_given_root(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "agentrc.toml").write_text('projects_root = "/elsewhere"\n')
    assert paths.projects_dir(other) == Path("/elsewhere")
    assert paths.projects_dir() == tmp_path / "home" / "projects"


def test_projects_dir_explicit_beats_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "env"))
    assert paths.projects_dir(explicit=tmp_path / "flag") == tmp_path / "flag"


def test_github_owner_environment_beats_config(monkeypatch, tmp_path):
    (tmp_path / "agentrc.toml").write_text('owner = "from-config"\n')
    monkeypatch.setenv("AGENTRC_GITHUB_OWNER", "from-env")
    assert paths.github_owner() == "from-env"


def test_github_owner_reads_config(tmp_path):
    (tmp_path / "agentrc.toml").write_text('owner = "from-config"\n')
    assert paths.github_owner() == "from-config"


def test_github_owner_uses_explicit_root(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "agentrc.toml").write_text('owner = "elsewhere"\n')
    assert paths.github_owner(other) == "elsewhere"


def test_github_owner_defaults_to_empty():
    assert paths.github_owner() == ""


def test_nothing_is_read_at_import(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTRC_HOME", str(tmp_path / "first"))
    assert paths.home() == tmp_path / "first"
    monkeypatch.setenv("AGENTRC_HOME", str(tmp_path / "second"))
    assert paths.home() == tmp_path / "second"
