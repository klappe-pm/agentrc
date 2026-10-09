from __future__ import annotations

from pathlib import Path

import pytest

from stratarc import paths

VARIABLES = ("STRATARC_HOME", "STRATARC_SOURCE", "LLM_ROOT_PROJECTS_DIR", "STRATARC_GITHUB_OWNER", "STRATARC_NAME")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)


def test_home_prefers_stratarc_home(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "override"))
    assert paths.home() == tmp_path / "override"


def test_home_falls_back_to_home(tmp_path):
    assert paths.home() == tmp_path / "home"


def test_home_ignores_empty_stratarc_home(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATARC_HOME", "  ")
    assert paths.home() == tmp_path / "home"


def test_home_falls_back_to_platform_home(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: cls("/platform-home")))
    assert paths.home() == Path("/platform-home")


def test_source_root_explicit_beats_environment(monkeypatch, tmp_path):
    (tmp_path / "flag").mkdir()
    monkeypatch.setenv("STRATARC_SOURCE", str(tmp_path / "env"))
    assert paths.source_root(tmp_path / "flag") == (tmp_path / "flag").resolve()


def test_source_root_environment_beats_discovery(monkeypatch, tmp_path):
    (tmp_path / "stratarc.toml").write_text("")
    (tmp_path / "env").mkdir()
    monkeypatch.setenv("STRATARC_SOURCE", str(tmp_path / "env"))
    assert paths.source_root() == (tmp_path / "env").resolve()


def test_source_root_discovers_upward(monkeypatch, tmp_path):
    root = tmp_path / "src"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (root / "stratarc.toml").write_text("")
    monkeypatch.chdir(nested)
    assert paths.source_root() == root.resolve()


def test_source_root_nearest_config_wins(monkeypatch, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / "stratarc.toml").write_text("")
    (inner / "stratarc.toml").write_text("")
    monkeypatch.chdir(inner)
    assert paths.source_root() == inner.resolve()


def test_source_root_defaults_to_cwd(monkeypatch, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    assert paths.source_root() == bare.resolve()


def _register(tmp_path: Path) -> Path:
    """Write <home>/.stratarc/sources.toml the way `stratarc source use` does and return the registered directory."""
    registered = tmp_path / "registered"
    registered.mkdir(parents=True, exist_ok=True)
    layout = tmp_path / "home" / ".stratarc"
    layout.mkdir(parents=True, exist_ok=True)
    (layout / "sources.toml").write_text(f'schema_version = 1\nactive = "main"\n\n[sources.main]\npath = "{registered}"\n')
    return registered


def test_source_root_uses_the_active_registered_source_after_discovery_fails(tmp_path):
    registered = _register(tmp_path)
    assert paths.source_root() == registered.resolve()


def test_source_root_explicit_and_environment_beat_the_active_source(monkeypatch, tmp_path):
    _register(tmp_path)
    (tmp_path / "env").mkdir()
    assert paths.source_root(tmp_path / "flag") == (tmp_path / "flag").resolve()
    monkeypatch.setenv("STRATARC_SOURCE", str(tmp_path / "env"))
    assert paths.source_root() == (tmp_path / "env").resolve()


def test_source_root_nearest_config_beats_the_active_source(monkeypatch, tmp_path):
    _register(tmp_path)
    here = tmp_path / "here"
    here.mkdir()
    (here / "stratarc.toml").write_text("")
    monkeypatch.chdir(here)
    assert paths.source_root() == here.resolve()


def test_source_root_follows_the_home_override_for_the_active_source(monkeypatch, tmp_path):
    other = tmp_path / "other-home"
    (other / ".stratarc").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (other / ".stratarc" / "sources.toml").write_text(f'active = "main"\n[sources.main]\npath = "{target}"\n')
    monkeypatch.setenv("STRATARC_HOME", str(other))
    assert paths.source_root() == target.resolve()


@pytest.mark.parametrize(
    "content",
    [
        "this is = = not toml",
        'schema_version = 99\nactive = "main"\n[sources.main]\npath = "{target}"\n',
        '[sources.main]\npath = "{target}"\n',
        'active = "ghost"\n[sources.main]\npath = "{target}"\n',
        'active = "main"\n[sources.main]\npath = 7\n',
    ],
)
def test_source_root_ignores_an_unusable_sources_file(tmp_path, content):
    target = tmp_path / "registered"
    target.mkdir()
    layout = tmp_path / "home" / ".stratarc"
    layout.mkdir(parents=True)
    (layout / "sources.toml").write_text(content.format(target=target))
    assert paths.source_root() == tmp_path.resolve()


def test_source_root_ignores_an_active_source_whose_directory_is_gone(tmp_path):
    registered = _register(tmp_path)
    registered.rmdir()
    assert paths.source_root() == tmp_path.resolve()


def test_the_sources_schema_ceiling_matches_the_home_layout():
    from stratarc import home_layout

    assert paths._SOURCES_SCHEMA == home_layout.SCHEMA_VERSION


def test_projects_root_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "work"))
    assert paths.projects_root() == tmp_path / "work"


def test_projects_root_default_follows_home(monkeypatch, tmp_path):
    assert paths.projects_root() == tmp_path / "home" / "projects" / "active"
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "other"))
    assert paths.projects_root() == tmp_path / "other" / "projects" / "active"


def test_projects_root_explicit_beats_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "env"))
    assert paths.projects_root(tmp_path / "flag") == tmp_path / "flag"


def test_projects_root_environment_beats_config(monkeypatch, tmp_path):
    (tmp_path / "stratarc.toml").write_text('projects_root = "/from/config"\n')
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "env"))
    assert paths.projects_root() == tmp_path / "env"


def test_projects_root_reads_config(tmp_path):
    (tmp_path / "stratarc.toml").write_text('projects_root = "/from/config"\n')
    assert paths.projects_root() == Path("/from/config")


def test_projects_root_config_expands_tilde_against_stratarc_home(monkeypatch, tmp_path):
    (tmp_path / "stratarc.toml").write_text('projects_root = "~/work"\n')
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "other"))
    assert paths.projects_root() == tmp_path / "other" / "work"


def test_projects_root_reads_config_of_explicit_root(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "stratarc.toml").write_text('projects_root = "/elsewhere"\n')
    assert paths.projects_root(root=other) == Path("/elsewhere")


def test_projects_dir_default_lifts_the_active_folder_to_its_parent(tmp_path):
    assert paths.projects_dir() == tmp_path / "home" / "projects"


def test_projects_dir_uses_the_environment_value_as_given(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "work"))
    assert paths.projects_dir() == tmp_path / "work"


def test_projects_dir_reads_the_configuration_of_the_given_root(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "stratarc.toml").write_text('projects_root = "/elsewhere"\n')
    assert paths.projects_dir(other) == Path("/elsewhere")
    assert paths.projects_dir() == tmp_path / "home" / "projects"


def test_projects_dir_explicit_beats_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(tmp_path / "env"))
    assert paths.projects_dir(explicit=tmp_path / "flag") == tmp_path / "flag"


def test_github_owner_environment_beats_config(monkeypatch, tmp_path):
    (tmp_path / "stratarc.toml").write_text('owner = "from-config"\n')
    monkeypatch.setenv("STRATARC_GITHUB_OWNER", "from-env")
    assert paths.github_owner() == "from-env"


def test_github_owner_reads_config(tmp_path):
    (tmp_path / "stratarc.toml").write_text('owner = "from-config"\n')
    assert paths.github_owner() == "from-config"


def test_github_owner_uses_explicit_root(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "stratarc.toml").write_text('owner = "elsewhere"\n')
    assert paths.github_owner(other) == "elsewhere"


def test_github_owner_defaults_to_empty():
    assert paths.github_owner() == ""


def test_nothing_is_read_at_import(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "first"))
    assert paths.home() == tmp_path / "first"
    monkeypatch.setenv("STRATARC_HOME", str(tmp_path / "second"))
    assert paths.home() == tmp_path / "second"
