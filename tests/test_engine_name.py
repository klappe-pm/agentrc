"""The engine name: precedence, validation, and the generated names derived from it."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agentrc import deploy_guard, paths
from agentrc.adapters import _components as components
from agentrc.adapters import codex, gemini, opencode
from agentrc.config import ConfigError, load_config

LEGACY = "llm-root"
POLICY = {
    "schemaVersion": 1,
    "allow": ["Bash(git *)"],
    "deny": ["Bash(git push --force *)", "Read(~/.ssh/**)"],
    "ask": [],
    "defaultMode": "bypassPermissions",
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTRC_SOURCE", raising=False)
    monkeypatch.delenv("AGENTRC_NAME", raising=False)
    monkeypatch.chdir(tmp_path)


def make_source(root: Path, name: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if name is not None:
        (root / "agentrc.toml").write_text(f'name = "{name}"\n')
    (root / "permissions.json").write_text(json.dumps(POLICY) + "\n")
    (root / "hooks").mkdir(exist_ok=True)
    (root / "hooks" / "opencode-runtime-hooks.ts").write_text("export const Hook = async () => ({})\n")
    (root / "components.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcp_servers": [
                    {
                        "name": "demo",
                        "wanted": True,
                        "owner": name or "agentrc",
                        "runtimes": ["codex", "gemini", "opencode"],
                        "command": "demo",
                    }
                ],
            }
        )
    )
    return root


def generated(source: Path, tmp_path: Path) -> dict[str, str]:
    """The names and contents the Codex, OpenCode, Gemini and ledger renderers produce."""
    out: dict[str, str] = {}
    for label, adapter in (("codex", codex), ("opencode", opencode), ("gemini", gemini)):
        target = tmp_path / f"target-{label}"
        target.mkdir()
        adapter.sync(source, target)
        for path in sorted(target.rglob("*")):
            if path.is_file() and path.name != "hooks":
                out[f"{label}/{path.relative_to(target).as_posix()}"] = path.read_text(errors="replace")
    return out


def test_default_name_is_agentrc():
    assert paths.engine_name() == "agentrc"


def test_default_name_shapes_every_generated_name(tmp_path):
    source = make_source(tmp_path / "src")
    files = generated(source, tmp_path)
    assert "codex/rules/agentrc.rules" in files
    assert "opencode/plugins/agentrc-hooks.ts" in files
    assert "gemini/policies/agentrc-permissions.toml" in files
    assert "codex/agentrc-mcp-servers.json" in files
    config = files["codex/config.toml"]
    assert config.count("# BEGIN agentrc permissions, generated from permissions.json\n[permissions.agentrc]\n") == 1
    assert config.rstrip().endswith("# END agentrc permissions")
    assert tomllib.loads(config)["default_permissions"] == "agentrc"
    assert not any(LEGACY in name for name in files)
    assert LEGACY not in config


def test_environment_name_reproduces_the_previous_output(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTRC_NAME", LEGACY)
    source = make_source(tmp_path / "src", LEGACY)
    files = generated(source, tmp_path)
    assert "codex/rules/llm-root.rules" in files
    assert "opencode/plugins/llm-root-hooks.ts" in files
    assert "gemini/policies/llm-root-permissions.toml" in files
    assert "codex/llm-root-mcp-servers.json" in files
    config = files["codex/config.toml"]
    assert "# BEGIN llm-root permissions, generated from permissions.json\n[permissions.llm-root]\nextends = \":workspace\"\n" in config
    assert '[permissions.llm-root.filesystem]\n"~/.ssh" = "deny"\n' in config
    assert config.rstrip().endswith("# END llm-root permissions")
    assert tomllib.loads(config)["default_permissions"] == "llm-root"


def test_config_name_reproduces_the_same_output_as_the_environment(monkeypatch, tmp_path):
    from_config = generated(make_source(tmp_path / "a", LEGACY), tmp_path / "a")
    monkeypatch.setenv("AGENTRC_NAME", LEGACY)
    (tmp_path / "b").mkdir()
    from_env = generated(make_source(tmp_path / "b", LEGACY), tmp_path / "b")
    assert from_config == from_env
    assert "codex/rules/llm-root.rules" in from_config


def test_environment_beats_config(monkeypatch, tmp_path):
    make_source(tmp_path / "src", "from-config")
    monkeypatch.setenv("AGENTRC_NAME", "from-env")
    assert paths.engine_name(tmp_path / "src") == "from-env"
    monkeypatch.delenv("AGENTRC_NAME")
    assert paths.engine_name(tmp_path / "src") == "from-config"


def test_name_is_read_lazily(monkeypatch):
    assert paths.engine_name() == "agentrc"
    monkeypatch.setenv("AGENTRC_NAME", "later")
    assert paths.engine_name() == "later"


def test_deploy_guard_names_follow_the_engine_name(monkeypatch):
    assert deploy_guard.stamp_name() == ".agentrc-deploy.json"
    assert deploy_guard.environment_config() == "agentrc.environment"
    assert deploy_guard.deploy_record() == Path(".agent-hooks/state/agentrc-deploy.json")
    monkeypatch.setenv("AGENTRC_NAME", LEGACY)
    assert deploy_guard.stamp_name() == ".llm-root-deploy.json"
    assert deploy_guard.environment_config() == "llm-root.environment"
    assert deploy_guard.deploy_record() == Path(".agent-hooks/state/llm-root-deploy.json")


def test_ledger_and_owner_follow_the_engine_name(monkeypatch):
    assert components.ledger_name() == "agentrc-mcp-servers.json"
    assert components.owner() == "agentrc"
    monkeypatch.setenv("AGENTRC_NAME", LEGACY)
    assert components.ledger_name() == "llm-root-mcp-servers.json"
    assert components.owner() == "llm-root"


def test_strip_removes_a_block_only_when_its_name_is_current():
    written = codex._codex_permissions_block(POLICY, "old-name")
    text = 'model = "x"\n\n' + written
    assert codex._strip_managed_permissions(text, "old-name") == 'model = "x"\n\n'
    assert codex._strip_managed_permissions(text, "agentrc") == text


def test_strip_removes_markerless_tables_of_the_current_name_only():
    text = '[permissions.old-name]\nextends = ":workspace"\n\n[permissions."other"]\nextends = ":workspace"\n'
    stripped = codex._strip_managed_permissions(text, "old-name")
    assert "old-name" not in stripped
    assert '[permissions."other"]' in stripped


def test_strip_escapes_the_name():
    text = "[permissions.axb]\nk = 1\n"
    assert codex._strip_managed_permissions(text, "a.b") == text


@pytest.mark.parametrize("bad", ["", "Agentrc", "1abc", "-abc", "a_b", "a b", "a.b"])
def test_config_rejects_bad_names(tmp_path, bad):
    (tmp_path / "agentrc.toml").write_text(f'name = "{bad}"\n')
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_config_rejects_a_non_string_name(tmp_path):
    (tmp_path / "agentrc.toml").write_text("name = 3\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


@pytest.mark.parametrize("good", ["agentrc", "llm-root", "a", "a1-b2"])
def test_config_accepts_valid_names(tmp_path, good):
    (tmp_path / "agentrc.toml").write_text(f'name = "{good}"\n')
    assert load_config(tmp_path).name == good


def test_config_default_name(tmp_path):
    assert load_config(tmp_path).name == "agentrc"


def test_environment_rejects_a_bad_name(monkeypatch):
    monkeypatch.setenv("AGENTRC_NAME", "Bad Name")
    with pytest.raises(ConfigError):
        paths.engine_name()
