"""Tests for the project-level sync: projects-root/<name>/ delivered into each active checkout."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stratarc import paths, projects, staging
from stratarc.control_plane import ControlPlane
from stratarc.rules_digest import CANONICAL_RULES_PREFIX, DIGEST_BEGIN, DIGEST_END, rules_prefix

GIT_IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def write(path: Path, text: str = "", *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


class FakeControlPlane:
    def __init__(self, records: dict[str, tuple[str, str]], rows=()):
        self.manifest = records
        self.rows = list(rows)

    def status(self, name: str) -> str:
        return self.manifest[name][0]

    def find_checkout(self, name: str, base: Path) -> list[Path]:
        return [path for path in base.rglob(name) if (path / ".git").exists()]


class World:
    """A temporary source root, projects directory and home that the module reads through its public seams."""

    def __init__(self, root: Path, projects_dir: Path):
        self.root = root
        self.projects = projects_dir

    @property
    def src(self) -> Path:
        return self.root / "projects-root"

    def source(self, name: str = "proj", agents: str | None = "# proj\n") -> Path:
        path = self.src / name
        path.mkdir(parents=True, exist_ok=True)
        if agents is not None:
            write(path / "AGENTS.md", agents)
        return path

    def checkout(self, name: str = "proj", tier: str = "active") -> Path:
        path = self.projects / tier / name
        (path / ".git").mkdir(parents=True, exist_ok=True)
        return path

    def sync(self, name: str = "proj", dry: bool = False, cp=None) -> list[str]:
        return projects.sync_project(name, dry=dry, cp=cp)

    def plane(self, text: str) -> ControlPlane:
        return ControlPlane.load(write(self.root / "control-plane.md", text))


@pytest.fixture
def world(tmp_path, monkeypatch, stratarc_home):
    root = tmp_path / "source"
    root.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setenv("STRATARC_SOURCE", str(root))
    for name in ("ROOT", "PROJECTS_SRC", "RETIRED_RULES", "RETIRED_HOOKS", "_ROOT_CONFIGURED"):
        monkeypatch.setattr(projects, name, getattr(projects, name))
    monkeypatch.setattr(projects, "RENDERERS", {})
    projects.configure_root(root)
    yield World(root, projects_dir)
    staging.cleanup_all()


def manifest_of(checkout: Path) -> dict:
    return json.loads((checkout / projects.delivered_manifest()).read_text())


def table(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def project_plane(*, rule_cells: str = "x | x", with_agents: bool = True) -> str:
    rows = [
        "# control-plane",
        "",
        "## projects",
        "",
        "| project | status | tier | template | origin |",
        "|---|---|---|---|---|",
        "| proj | active | normal | base | - |",
        "",
        "## rules",
        "",
        "| option | tier | global | proj |",
        "|---|---|---|---|",
        f"| [rule:a-rule](rules/a-rule.md) | global | {rule_cells} |",
        "",
    ]
    if with_agents:
        rows += [
            "## project-local-configuration",
            "",
            "| option | global | proj |",
            "|---|---|---|",
            "| [project:AGENTS.md](projects-root/proj/AGENTS.md) |  | x |",
            "",
        ]
    return "\n".join(rows)


# manifest hashes


DIGEST = hashlib.sha256(b"fixture content").hexdigest()


def test_manifest_hash_adds_the_prefix():
    assert projects._manifest_hash(DIGEST) == f"sha256-{DIGEST}"


def test_manifest_digest_strips_the_prefix():
    assert projects._manifest_digest(f"sha256-{DIGEST}") == DIGEST


def test_manifest_digest_passes_a_bare_hex_value_through():
    assert projects._manifest_digest(DIGEST) == DIGEST


@pytest.mark.parametrize("value", [f"sha256-{DIGEST}", DIGEST])
def test_read_delivered_normalizes_either_manifest_form(tmp_path, value):
    path = write(tmp_path / "delivered.json", json.dumps({"hashes": {".claude/rules/secret-resolution.md": value}}))
    entries, hashed = projects._read_delivered(path)
    assert hashed
    assert entries[".claude/rules/secret-resolution.md"] == DIGEST


def test_a_written_manifest_does_not_trip_gitleaks(tmp_path):
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        pytest.skip("gitleaks is not installed")
    manifest = {
        "hashes": {
            ".claude/rules/no-secret-exposure.md": projects._manifest_hash(DIGEST),
            ".claude/rules/token-shaped-values.md": projects._manifest_hash(DIGEST),
        }
    }
    write(tmp_path / "delivered.json", json.dumps(manifest, indent=2))
    result = subprocess.run(
        [gitleaks, "detect", "--no-git", "--source", str(tmp_path), "--exit-code", "1"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout


def test_the_delivered_manifest_is_named_for_the_engine(world):
    assert projects.delivered_manifest() == Path(".claude/stratarc-delivered.json")
    write(world.root / "stratarc.toml", 'name = "mytool"\n')
    assert projects.delivered_manifest() == Path(".claude/mytool-delivered.json")


# verify


def test_source_only_project_is_valid(world):
    world.source("vault-maker")
    cp = FakeControlPlane({"vault-maker": ("inactive", "normal")})
    assert projects.verify(cp) == []


def test_canonical_repository_is_not_reported_as_unregistered(world, tmp_path):
    primary = world.checkout("source-repo")
    projects.configure_root(primary)
    assert projects.verify(FakeControlPlane({})) == []


def test_nested_repository_is_not_an_independent_project(world):
    parent = world.checkout("session-data", "archived")
    (parent / "security-audits" / ".git").mkdir(parents=True)
    cp = FakeControlPlane({"session-data": ("archived", "low")})
    assert projects.verify(cp) == []


def test_worktree_rooted_verify_excludes_the_primary_checkout(world, tmp_path):
    primary = world.projects / "active" / "source-repo"
    git("init", "-q", str(primary))
    git(*GIT_IDENTITY, "-C", str(primary), "commit", "--allow-empty", "-m", "fixture")
    linked = tmp_path / "_worktrees" / "source-repo-lane-b"
    git("-C", str(primary), "worktree", "add", "-q", str(linked), "-b", "fixture-lane")
    projects.configure_root(linked)
    assert projects.verify(FakeControlPlane({})) == []


def test_verify_does_not_report_an_unmanaged_row_as_a_missing_checkout(world):
    (world.projects / "active" / "notes-vault" / ".obsidian").mkdir(parents=True)
    cp = FakeControlPlane({"notes-vault": ("unmanaged", "-")})
    assert projects.verify(cp) == []


def test_verify_reports_a_checkout_in_the_wrong_status_folder(world):
    world.checkout("proj", "archived")
    out = projects.verify(FakeControlPlane({"proj": ("active", "normal")}))
    assert out == ["proj: listed as active but sits under archived/"]


def test_verify_reports_an_unlisted_checkout(world):
    world.checkout("stray")
    out = projects.verify(FakeControlPlane({}))
    assert len(out) == 1
    assert out[0].startswith("stray: checkout at ")


def test_verify_does_not_report_a_public_target_checkout(world):
    write(world.src / "public-targets.json", json.dumps(["public-lib"]))
    world.checkout("public-lib")
    assert projects.verify(FakeControlPlane({})) == []


# projects directory


def test_projects_dir_lifts_the_default_active_folder_to_its_parent(world, monkeypatch, stratarc_home):
    monkeypatch.delenv("LLM_ROOT_PROJECTS_DIR")
    assert projects.projects_dir() == stratarc_home / "projects"


def test_projects_dir_uses_the_environment_value_as_given(world):
    assert projects.projects_dir() == world.projects


def test_projects_dir_reads_the_environment_when_called(world, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("LLM_ROOT_PROJECTS_DIR", str(other))
    assert projects.projects_dir() == other


# resolve


def test_resolve_refuses_to_target_the_canonical_checkout(world):
    """Even a stale manifest row for the canonical checkout must never route a write into it."""
    primary = world.checkout("source-repo")
    projects.configure_root(primary)
    dst, why = projects.resolve("source-repo", FakeControlPlane({"source-repo": ("active", "normal")}))
    assert dst is None
    assert "canonical" in why


def test_resolve_refuses_a_public_target_by_name(world):
    write(world.src / "public-targets.json", json.dumps(["public-lib"]))
    world.checkout("public-lib")
    dst, why = projects.resolve("public-lib", FakeControlPlane({"public-lib": ("active", "normal")}))
    assert dst is None
    assert "public repository" in why


def test_resolve_skips_a_project_that_is_not_active(world):
    world.checkout("proj", "archived")
    dst, why = projects.resolve("proj", FakeControlPlane({"proj": ("archived", "low")}))
    assert dst is None
    assert "only active projects" in why


def test_resolve_skips_a_project_missing_from_the_manifest(world):
    dst, why = projects.resolve("ghost", FakeControlPlane({}))
    assert dst is None
    assert "not in the control-plane manifest" in why


def test_resolve_without_a_control_plane_finds_a_single_checkout(world):
    checkout = world.checkout("proj")
    dst, why = projects.resolve("proj", None)
    assert dst == checkout
    assert why == ""


def test_resolve_without_a_control_plane_skips_an_ambiguous_name(world):
    world.checkout("proj", "active")
    world.checkout("proj", "archived")
    dst, why = projects.resolve("proj", None)
    assert dst is None
    assert "not resolvable" in why


# source trees are never targets


def test_a_checkout_with_a_config_file_is_a_source_tree(world):
    checkout = world.checkout("other-source")
    write(checkout / "stratarc.toml", "")
    dst, why = projects.resolve("other-source", FakeControlPlane({"other-source": ("active", "normal")}))
    assert dst is None
    assert "source tree" in why


def test_a_checkout_with_a_control_plane_and_projects_root_is_a_source_tree(world):
    checkout = world.checkout("second-clone")
    write(checkout / "control-plane.md", "# control-plane\n")
    (checkout / "projects-root").mkdir()
    dst, why = projects.resolve("second-clone", FakeControlPlane({"second-clone": ("active", "normal")}))
    assert dst is None
    assert "source tree" in why


def test_a_control_plane_alone_does_not_make_a_source_tree(tmp_path):
    write(tmp_path / "control-plane.md", "# control-plane\n")
    assert projects._is_source_tree(tmp_path) is False
    (tmp_path / "projects-root").mkdir()
    assert projects._is_source_tree(tmp_path) is True


def test_an_engine_scripts_file_no_longer_marks_a_source_tree(tmp_path):
    """The old test looked for the engine's own script; a user's source root never has one."""
    (tmp_path / "projects-root").mkdir()
    write(tmp_path / "scripts" / "sync-projects.py", "")
    assert projects._is_source_tree(tmp_path) is False


def test_a_checkout_of_a_public_repository_is_skipped_by_its_origin(world):
    checkout = world.checkout("renamed-clone")
    git("-C", str(checkout), "init", "-q")
    git("-C", str(checkout), "remote", "add", "origin", "https://github.com/someone/public-lib.git")
    write(world.src / "public-targets.json", json.dumps(["someone/public-lib"]))
    dst, why = projects.resolve("renamed-clone", FakeControlPlane({"renamed-clone": ("active", "normal")}))
    assert dst is None
    assert "public repository" in why


# retired rules


def test_retired_rule_is_pruned_and_unknown_file_is_kept(world):
    write(world.root / "rules" / "current.md", "current\n")
    write(world.root / "rules" / "retired.json", '{"retired": ["old-name.md"]}\n')
    world.source()
    checkout = world.checkout()
    rules = checkout / ".claude" / "rules" / "global"
    write(rules / "old-name.md", "stale\n")
    write(rules / "never-known.md", "local\n")
    acts = world.sync()
    assert f"remove {rules / 'old-name.md'}" in acts
    assert not (rules / "old-name.md").exists()
    assert (rules / "never-known.md").exists()


def test_known_rule_at_nested_path_is_pruned_when_delivered_flat(world):
    write(world.root / "rules" / "current.md", "current\n")
    source = world.source()
    write(source / "rules" / "current.md", "current\n")
    checkout = world.checkout()
    nested = checkout / ".claude" / "rules" / "global"
    write(nested / "current.md", "old copy\n")
    world.sync()
    assert not (nested / "current.md").exists()
    assert (checkout / ".claude" / "rules" / "current.md").read_text() == "current\n"


def test_a_locally_edited_rule_is_kept_when_the_project_opts_out(world):
    write(world.root / "rules" / "current.md", "v1\n")
    source = world.source()
    write(source / "rules" / "current.md", "v1\n")
    checkout = world.checkout()
    target = checkout / ".claude" / "rules"
    world.sync()
    assert (target / "current.md").read_text() == "v1\n"
    (target / "current.md").write_text("v1, with a local addition\n")
    (source / "rules" / "current.md").unlink()
    acts = world.sync()
    assert (target / "current.md").read_text() == "v1, with a local addition\n"
    assert not [a for a in acts if "current.md" in a], acts


def test_adopt_skips_an_unedited_delivered_rule_and_captures_an_edited_one(world):
    write(world.root / "rules" / "current.md", "v1\n")
    source = world.source()
    write(source / "rules" / "current.md", "v1\n")
    checkout = world.checkout()
    target = checkout / ".claude" / "rules"
    world.sync()
    unedited = projects.adopt("proj", cp=None)
    assert not [a for a in unedited if "rules" in a], unedited
    assert (source / "rules" / "current.md").read_text() == "v1\n"
    (target / "current.md").write_text("v1, edited in the checkout\n")
    edited = projects.adopt("proj", cp=None)
    assert "captured .claude/rules" in edited
    assert (source / "rules" / "current.md").read_text() == "v1, edited in the checkout\n"


def test_missing_retired_list_is_empty(world):
    assert projects.retired_rule_names() == set()


def test_project_copy_ignores_generated_python_artifacts(world):
    source = world.source("proj", agents=None)
    write(source / "hooks" / "hook.py", "VALUE = 1\n")
    cache = source / "hooks" / "__pycache__"
    cache.mkdir()
    (cache / "hook.cpython-314.pyc").write_bytes(b"compiled")
    checkout = world.checkout()
    world.sync()
    target = checkout / ".claude" / "hooks"
    assert (target / "hook.py").is_file()
    assert not (target / "__pycache__").exists()


# prune


def test_denied_empty_directory_cleanup_does_not_stop_file_removal(tmp_path, monkeypatch, capsys):
    target = tmp_path / "hooks"
    nested = target / "lib"
    first = write(nested / "retired.sh", "old\n")
    second = write(target / "also-retired.sh", "old\n")

    def deny(self, *args):
        raise PermissionError(1, "Operation not permitted", str(self))

    monkeypatch.setattr(Path, "rmdir", deny)
    acts: list[str] = []
    gone, kept = projects._prune(target, {"lib/retired.sh", "also-retired.sh"}, set(), False, acts, {})
    assert gone == {"lib/retired.sh", "also-retired.sh"}
    assert kept == {}
    assert not first.exists()
    assert not second.exists()
    assert nested.is_dir()
    assert len(acts) == 2
    assert "Operation not permitted" in capsys.readouterr().err


def test_a_locally_edited_file_is_kept_by_the_prune(tmp_path, capsys):
    target = tmp_path / "hooks"
    edited = write(target / "mine.sh", "edited\n")
    recorded = hashlib.sha256(b"original\n").hexdigest()
    acts: list[str] = []
    gone, kept = projects._prune(target, {"mine.sh"}, set(), False, acts, {"mine.sh": recorded})
    assert gone == {"mine.sh"}
    assert kept == {"mine.sh": recorded}
    assert edited.exists()
    assert acts == []
    assert "locally edited" in capsys.readouterr().err


# retired hooks


def test_missing_retired_hooks_list_is_empty(world):
    assert projects.retired_hook_names() == set()


def test_retired_hook_survives_when_the_manifest_never_recorded_the_hooks_prefix(world):
    write(world.root / "hooks" / "retired.json", json.dumps({"retired": ["lib/old-guard.sh", "top-guard.sh"]}) + "\n")
    world.source()
    checkout = world.checkout()
    claude_hooks = checkout / ".claude" / "hooks"
    write(claude_hooks / "lib" / "old-guard.sh", "stale\n")
    write(claude_hooks / "top-guard.sh", "#!/usr/bin/env bash\nexit 0\n")
    never_known = write(claude_hooks / "mine.sh", "mine\n")
    codex_hooks = checkout / ".codex" / "hooks"
    write(codex_hooks / "lib" / "old-guard.sh", "stale\n")
    write(codex_hooks / "top-guard.sh", "#!/usr/bin/env bash\nexit 0\n")
    codex_never_known = write(codex_hooks / "mine.sh", "mine\n")
    settings_path = write(
        checkout / ".claude" / "settings.json",
        json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Write|Edit",
                    "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/top-guard.sh", "timeout": 3}],
                }]
            }
        }) + "\n",
    )
    codex_json = write(
        checkout / ".codex" / "hooks.json",
        json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Write|Edit",
                    "hooks": [{"type": "command", "command": "$CODEX_PROJECT_DIR/.codex/hooks/top-guard.sh"}],
                }]
            }
        }) + "\n",
    )
    write(
        checkout / projects.delivered_manifest(),
        json.dumps({
            "paths": ["AGENTS.md"],
            "hashes": {"AGENTS.md": hashlib.sha256(b"# proj\n").hexdigest()},
            "project_plugins": [],
            "project_mcp_servers": [],
        }) + "\n",
    )
    world.sync()
    assert not (claude_hooks / "lib" / "old-guard.sh").exists()
    assert not (claude_hooks / "top-guard.sh").exists()
    assert never_known.exists()
    assert not (codex_hooks / "lib" / "old-guard.sh").exists()
    assert not (codex_hooks / "top-guard.sh").exists()
    assert codex_never_known.exists()
    assert "top-guard.sh" not in json.dumps(json.loads(settings_path.read_text()))
    assert "top-guard.sh" not in codex_json.read_text()


# surface drift


def surface_world(world, surfaces: dict[str, str], copy: list[str] | None = None, body: str = "# proj\nmaster body\n"):
    source = world.source("proj", agents=body)
    if copy is not None:
        write(source / "surfaces.json", json.dumps({"copy": copy}) + "\n")
    checkout = world.checkout()
    for name, text in surfaces.items():
        write(checkout / name, text)
    return FakeControlPlane({"proj": ("active", "normal")})


def test_hand_edited_surface_without_surfaces_json_is_drift(world):
    cp = surface_world(world, {"AGENTS.md": "# proj\nmaster body\n", "CLAUDE.md": "hand edited\n"})
    divergent, shims = projects.surface_drift(cp)
    assert any("proj: CLAUDE.md" in d for d in divergent)
    assert shims == []


def test_surface_left_out_of_surfaces_json_is_a_shim_not_drift(world):
    same = "# proj\nmaster body\n"
    cp = surface_world(
        world,
        {"AGENTS.md": same, "CODEX.md": same, "GEMINI.md": same, "CLAUDE.md": "hand authored shim\n"},
        copy=["AGENTS.md", "CODEX.md", "GEMINI.md"],
    )
    divergent, shims = projects.surface_drift(cp)
    assert divergent == []
    assert any("proj: CLAUDE.md" in s for s in shims)


def test_unknown_surface_name_in_copy_list_is_not_drift(world):
    cp = surface_world(world, {"AGENTS.md": "# proj\nmaster body\n"}, copy=["AGENTS.md", "WARP.md"])
    divergent, _shims = projects.surface_drift(cp)
    assert divergent == []


def test_a_missing_surface_is_drift(world):
    cp = surface_world(world, {"AGENTS.md": "# proj\nmaster body\n"})
    divergent, _shims = projects.surface_drift(cp)
    assert "proj: CLAUDE.md is missing" in divergent


# digest embedding


def digest_body() -> bytes:
    return f"# proj\n\n{DIGEST_BEGIN}\nold\n{DIGEST_END}\n".encode()


def digest_rules(world, rules: dict[str, str], tiers: dict[str, list[str]]) -> None:
    for stem, text in rules.items():
        write(world.root / "rules" / f"{stem}.md", text)
    write(world.root / "rules" / "tiers.json", json.dumps(tiers) + "\n")


def test_master_with_markers_is_replaced_with_the_columns_digest(world):
    digest_rules(world, {"a-rule": "# a-rule\n\nBody.\n"}, {"global": ["a-rule.md"], "common": [], "project": []})
    world.plane(project_plane())
    updated = projects._embed_project_digest(digest_body(), "proj")
    assert b"### a-rule" in updated
    assert b"old\n" + DIGEST_END.encode() not in updated


def test_master_without_markers_is_returned_unchanged(world):
    body = b"# proj\n\nno markers here\n"
    assert projects._embed_project_digest(body, "proj") == body


def test_sync_project_embeds_the_column_digest_end_to_end(world):
    digest_rules(world, {"a-rule": "# a-rule\n\nBody.\n"}, {"global": ["a-rule.md"], "common": [], "project": []})
    world.source("proj", agents=digest_body().decode())
    checkout = world.checkout()
    cp = world.plane(project_plane())
    world.sync(cp=cp)
    written = (checkout / "AGENTS.md").read_text(encoding="utf-8")
    assert "### a-rule" in written
    assert f"old\n{DIGEST_END}" not in written


def test_every_project_surface_carries_the_binding_digest(world):
    digest_rules(
        world,
        {
            stem: f"# {stem}\n\n## binding\n\nBIND-{n}.\n\n## rationale\n\nWHY-{n}.\n"
            for stem, n in (("a-rule", "A"), ("b-rule", "B"))
        },
        {"global": ["a-rule.md"], "common": ["b-rule.md"], "project": []},
    )
    world.source("proj", agents=f"# proj\n\n{DIGEST_BEGIN}\n{DIGEST_END}\n")
    checkout = world.checkout()
    cp = world.plane(
        table(
            "# control-plane",
            "",
            "## projects",
            "",
            "| project | status | tier | template | origin |",
            "|---|---|---|---|---|",
            "| proj | active | normal | base | - |",
            "",
            "## rules",
            "",
            "| option | tier | global | proj |",
            "|---|---|---|---|",
            "| [rule:a-rule](rules/a-rule.md) | global | x |  |",
            "| [rule:b-rule](rules/b-rule.md) | common | x | x |",
            "",
            "## project-local-configuration",
            "",
            "| option | global | proj |",
            "|---|---|---|",
            "| [project:AGENTS.md](projects-root/proj/AGENTS.md) |  | x |",
        )
    )
    world.sync(cp=cp)
    divergent, _shims = projects.surface_drift(cp)
    claude = (checkout / "CLAUDE.md").read_text(encoding="utf-8")
    assert CANONICAL_RULES_PREFIX not in claude
    for stem, n in (("a-rule", "A"), ("b-rule", "B")):
        assert f"BIND-{n}." in claude
        assert f"WHY-{n}." not in claude
        assert f"Full rule: `{rules_prefix(world.root)}{stem}.md`" in claude
    assert (checkout / ".claude" / "rules" / "b-rule.md").is_file()
    for name in ("AGENTS.md", "CODEX.md", "GEMINI.md"):
        assert (checkout / name).read_text(encoding="utf-8") == claude
    assert divergent == []


# adopt

ADOPT_PLANE = (
    "| project | status | tier | template | origin |\n"
    "|---|---|---|---|---|\n"
    "| demo | active | normal | base | - |\n"
    "\n"
    "| option | global | demo |\n"
    "|---|---|---|\n"
    "| AGENTS.md | x | x |\n"
    "| [project:AGENTS.md](projects-root/other/AGENTS.md) |  |  |\n"
    "| project:rules |  |  |\n"
)


def test_adopt_opts_the_project_into_what_it_captured(world):
    write(world.root / "AGENTS.md", "SOURCE ROOT OWN INSTRUCTIONS\n")
    path = write(world.root / "control-plane.md", ADOPT_PLANE)
    checkout = world.checkout("demo")
    write(checkout / "AGENTS.md", "DEMO OWN INSTRUCTIONS\n")
    write(checkout / ".claude" / "rules" / "local.md", "local\n")
    acts = projects.adopt("demo", ControlPlane.load(path))
    after = ControlPlane.load(path)
    assert after.enabled("demo", "project:AGENTS.md"), acts
    assert after.enabled("demo", "project:rules"), acts
    assert not after.enabled("global", "project:AGENTS.md")
    assert after.enabled("demo", "AGENTS.md")
    assert "[project:AGENTS.md](projects-root/other/AGENTS.md)" in path.read_text()
    stage, _ = staging.build_stage(world.root, after, "demo")
    assert (stage / "AGENTS.md").read_text() == "DEMO OWN INSTRUCTIONS\n"


def test_adopt_without_a_control_plane_file_changes_nothing(world):
    checkout = world.checkout("demo")
    write(checkout / "AGENTS.md", "DEMO\n")
    acts = projects.adopt("demo", FakeControlPlane({"demo": ("active", "normal")}))
    assert "master from AGENTS.md" in acts


def test_adopt_never_captures_a_retired_hook_as_project_owned(world):
    write(world.root / "hooks" / "retired.json", json.dumps({"retired": ["lib/old-guard.sh"]}) + "\n")
    checkout = world.checkout("demo")
    write(checkout / "AGENTS.md", "DEMO\n")
    write(checkout / ".claude" / "hooks" / "lib" / "old-guard.sh", "stale\n")
    acts = projects.adopt("demo", FakeControlPlane({"demo": ("active", "normal")}))
    assert not (world.src / "demo" / "hooks" / "lib" / "old-guard.sh").exists()
    assert not [a for a in acts if "hooks" in a], acts


# carried guards

PACKAGED_HOOKS = projects._packaged("hooks")

# Attribution text is assembled from fragments so this file carries no literal line for the live guard to block on.
_AGENT = "Cla" + "ude"
_FOOTER = "Generated with " + _AGENT + " Code"
_CARRIED_COMMAND = "$CLAUDE_PROJECT_DIR/.claude/hooks/no-attribution-guard.sh"
PATTERNS = "lib/private/env-dump-patterns.json"


def carried_commands(hooks: dict, guard: str = "no-attribution-guard.sh") -> list[str]:
    return [
        h.get("command", "")
        for group in hooks.get("PreToolUse", [])
        for h in group.get("hooks", [])
        if h.get("command", "").endswith("/" + guard)
    ]


def git_config(checkout: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(checkout), "config", *args], capture_output=True, text=True)
    return result.stdout.strip()


def run_carried(checkout: Path, home: Path, command: str, guard: str = "no-attribution-guard.sh", extra_env=None):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(checkout),
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", "-c", f"$CLAUDE_PROJECT_DIR/.claude/hooks/{guard}"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def user_level_guard(home: Path, register: bool = True, event: str = "PreToolUse", matcher=None) -> None:
    user_hooks = home / ".claude" / "hooks"
    (user_hooks / "lib").mkdir(parents=True)
    for rel in projects.CARRIED_GUARD_FILES:
        shutil.copy2(PACKAGED_HOOKS / rel, user_hooks / rel)
    if register:
        group = {"hooks": [{"type": "command", "command": "$HOME/.claude/hooks/no-attribution-guard.sh"}]}
        if matcher is not None:
            group["matcher"] = matcher
        write(home / ".claude" / "settings.json", json.dumps({"hooks": {event: [group]}}))


@pytest.fixture
def carried(world):
    """A source root with one project, proj, and its checkout."""
    world.source()
    return world.checkout()


def test_guard_and_library_land_executable_in_the_tracked_tree(world, carried):
    world.sync()
    for rel in projects.CARRIED_GUARD_FILES:
        out = carried / ".claude" / "hooks" / rel
        assert out.is_file(), rel
        assert out.read_bytes() == (PACKAGED_HOOKS / rel).read_bytes(), rel
    assert os.access(carried / ".claude" / "hooks" / "no-attribution-guard.sh", os.X_OK)


def test_the_source_roots_hooks_overlay_the_packaged_guard_library(world, carried):
    write(world.root / "hooks" / "lib" / "guard-log.sh", "# source root variant\n")
    world.sync()
    assert (carried / ".claude" / "hooks" / "lib" / "guard-log.sh").read_text() == "# source root variant\n"
    assert (carried / ".claude" / "hooks" / "lib" / "log.sh").read_bytes() == (PACKAGED_HOOKS / "lib" / "log.sh").read_bytes()
    assert projects.carried_source("lib/guard-log.sh") == world.root / "hooks" / "lib" / "guard-log.sh"


def test_the_carried_set_covers_every_file_the_guard_sources():
    """A library the guard reads at run time and the carry leaves behind would make the carried copy crash in a clone."""
    import re

    for guard, files in projects.CARRIED_GUARDS.items():
        referenced = set()
        for rel in files:
            text = (PACKAGED_HOOKS / rel).read_text(encoding="utf-8")
            for var, name in re.findall(r"\$(HOOK_DIR|_LOG_LIB_DIR|_LOG_UTILS_DIR)/([\w./-]+)", text):
                referenced.add(name if var == "HOOK_DIR" else "lib/" + name)
        assert referenced, guard
        assert referenced <= set(files), guard


def test_each_guard_compares_the_same_files_the_sync_carries():
    import re

    assert set(projects.CARRIED_GUARDS) == {"no-attribution-guard.sh", "env-dump-guard.sh"}
    for guard, files in projects.CARRIED_GUARDS.items():
        text = (PACKAGED_HOOKS / guard).read_text(encoding="utf-8")
        match = re.search(r"for rel in ([^;]+); do", text)
        assert match, guard
        assert tuple(match.group(1).split()) == files, guard
    guard_text = (PACKAGED_HOOKS / "env-dump-guard.sh").read_text(encoding="utf-8")
    for rel in projects.CARRIED_GUARD_OPTIONAL_FILES:
        assert rel in guard_text


def test_the_env_dump_guard_is_registered_once_without_a_matcher(world, carried):
    world.sync()
    settings = json.loads((carried / ".claude" / "settings.json").read_text())
    guard = "env-dump-guard.sh"
    assert carried_commands(settings["hooks"], guard) == [f"$CLAUDE_PROJECT_DIR/.claude/hooks/{guard}"]
    group = next(g for g in settings["hooks"]["PreToolUse"] if carried_commands({"PreToolUse": [g]}, guard))
    assert "matcher" not in group
    assert os.access(carried / ".claude" / "hooks" / guard, os.X_OK)


def test_the_carried_env_dump_guard_denies_in_a_clone(world, carried, tmp_path):
    world.sync()
    home = tmp_path / "clone-home"
    home.mkdir()
    result = run_carried(
        carried, home, "launchctl print system", guard="env-dump-guard.sh", extra_env={"GUARD_LOG_DIR": str(tmp_path / "log")}
    )
    assert '"permissionDecision":"deny"' in result.stdout


def test_registration_is_merged_beside_the_projects_own_hooks(world, carried):
    own = {
        "PostToolUse": [
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/own.sh"}]}
        ]
    }
    source = world.src / "proj"
    write(source / "hooks" / "hooks.json", json.dumps(own))
    write(source / "hooks" / "own.sh", "exit 0\n")
    settings_path = write(carried / ".claude" / "settings.json", json.dumps({"permissions": {"allow": ["Read"]}}))
    world.sync()
    settings = json.loads(settings_path.read_text())
    assert settings["permissions"] == {"allow": ["Read"]}
    assert settings["hooks"]["PostToolUse"] == own["PostToolUse"]
    assert carried_commands(settings["hooks"]) == [_CARRIED_COMMAND]
    guard_group = next(g for g in settings["hooks"]["PreToolUse"] if carried_commands({"PreToolUse": [g]}))
    assert "matcher" not in guard_group
    codex = json.loads((carried / ".codex" / "hooks.json").read_text())
    assert carried_commands(codex["hooks"]) == []


def test_a_project_without_its_own_hooks_keeps_hand_registered_hooks(world, carried):
    hand = {"Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}
    settings_path = write(carried / ".claude" / "settings.json", json.dumps({"hooks": hand}))
    world.sync()
    settings = json.loads(settings_path.read_text())
    assert settings["hooks"]["Stop"] == hand["Stop"]
    assert carried_commands(settings["hooks"]) == [_CARRIED_COMMAND]


def test_a_second_sync_is_current(world, carried):
    write(world.src / "proj" / "hooks" / "hooks.json", "{}")
    world.sync()
    assert world.sync() == []


def test_a_deleted_hook_loses_its_registrations_with_hooks_json_still_present(world, carried):
    command = "$CLAUDE_PROJECT_DIR/.claude/hooks/own.sh"
    own = {"PostToolUse": [{"hooks": [{"type": "command", "command": command}]}]}
    source = world.src / "proj"
    write(source / "hooks" / "hooks.json", json.dumps(own))
    source_hook = write(source / "hooks" / "own.sh", "exit 0\n")
    world.sync()
    claude_hook = carried / ".claude" / "hooks" / "own.sh"
    codex_hook = carried / ".codex" / "hooks" / "own.sh"
    assert claude_hook.is_file()
    assert codex_hook.is_file()
    source_hook.unlink()
    world.sync()
    assert not claude_hook.exists()
    assert not codex_hook.exists()
    settings = json.loads((carried / ".claude" / "settings.json").read_text())
    assert command not in json.dumps(settings)
    assert carried_commands(settings["hooks"]) == [_CARRIED_COMMAND]
    assert "own.sh" not in (carried / ".codex" / "hooks.json").read_text()
    assert world.sync() == []


def test_check_mode_writes_nothing(world, carried):
    acts = world.sync(dry=True)
    assert any("no-attribution-guard.sh" in a for a in acts), acts
    assert not (carried / ".claude" / "hooks").exists()


def test_verify_reports_a_carried_guard_that_differs_from_source(world, carried):
    world.sync()
    cp = FakeControlPlane({"proj": ("active", "normal")})
    assert projects.carried_guard_drift(cp) == []
    (carried / ".claude" / "hooks" / "lib" / "attribution-detect.py").write_text("# edited\n")
    drift = projects.carried_guard_drift(cp)
    assert len(drift) == 1, drift
    assert "proj: .claude/hooks/lib/attribution-detect.py differs from source" in drift[0]


def test_carry_copies_the_pattern_file_when_the_source_root_has_it(world, carried):
    source = write(world.root / "hooks" / PATTERNS, '{"version": 1}\n')
    world.sync()
    out = carried / ".claude" / "hooks" / PATTERNS
    assert out.read_bytes() == source.read_bytes()
    assert world.sync() == []


def test_carry_still_carries_the_guard_without_the_pattern_file(world, carried):
    assert not (world.root / "hooks" / PATTERNS).exists()
    acts = world.sync()
    assert not any("skip carry" in a for a in acts), acts
    for rel in projects.CARRIED_GUARD_FILES:
        assert (carried / ".claude" / "hooks" / rel).is_file(), rel
    assert not (carried / ".claude" / "hooks" / PATTERNS).exists()


def test_the_pattern_file_is_never_read_from_the_packaged_data():
    assert not (PACKAGED_HOOKS / PATTERNS).exists()
    assert projects.carried_source(PATTERNS) is None


def test_verify_reports_a_changed_or_missing_pattern_file(world, carried):
    write(world.root / "hooks" / PATTERNS, '{"version": 1}\n')
    world.sync()
    cp = FakeControlPlane({"proj": ("active", "normal")})
    assert projects.carried_guard_drift(cp) == []
    out = carried / ".claude" / "hooks" / PATTERNS
    out.write_text("{}\n")
    assert projects.carried_guard_drift(cp) == [f"proj: .claude/hooks/{PATTERNS} differs from source"]
    out.unlink()
    assert projects.carried_guard_drift(cp) == [f"proj: .claude/hooks/{PATTERNS} is missing"]


def test_verify_is_clean_when_neither_side_has_the_pattern_file(world, carried):
    world.sync()
    assert projects.carried_guard_drift(FakeControlPlane({"proj": ("active", "normal")})) == []


def test_verify_reports_a_carried_pattern_file_the_source_dropped(world, carried):
    source = write(world.root / "hooks" / PATTERNS, '{"version": 1}\n')
    world.sync()
    source.unlink()
    drift = projects.carried_guard_drift(FakeControlPlane({"proj": ("active", "normal")}))
    assert drift == [f"proj: .claude/hooks/{PATTERNS} is stale: source no longer carries {PATTERNS}"]


def test_delivery_removes_a_carried_pattern_file_the_source_dropped(world, carried):
    source = write(world.root / "hooks" / PATTERNS, '{"version": 1}\n')
    world.sync()
    out = carried / ".claude" / "hooks" / PATTERNS
    assert out.is_file()
    source.unlink()
    acts = world.sync(dry=True)
    assert any("remove" in a and "env-dump-patterns.json" in a for a in acts), acts
    assert out.is_file()
    world.sync()
    assert not out.exists()
    assert world.sync() == []
    assert projects.carried_guard_drift(FakeControlPlane({"proj": ("active", "normal")})) == []


def test_verify_reports_a_carried_guard_that_lost_its_executable_bit(world, carried):
    world.sync()
    (carried / ".claude" / "hooks" / "no-attribution-guard.sh").chmod(0o644)
    drift = projects.carried_guard_drift(FakeControlPlane({"proj": ("active", "normal")}))
    assert "proj: .claude/hooks/no-attribution-guard.sh is not executable" in drift


def test_a_project_copy_of_a_carried_library_file_is_not_a_pending_action(world, carried):
    lib = world.src / "proj" / "hooks" / "lib"
    lib.mkdir(parents=True)
    shutil.copy2(PACKAGED_HOOKS / "lib" / "log.sh", lib / "log.sh")
    write(lib / "guard-log.sh", "# project variant\n")
    world.sync()
    assert world.sync() == []
    out = carried / ".claude" / "hooks" / "lib" / "guard-log.sh"
    assert out.read_bytes() == (PACKAGED_HOOKS / "lib" / "guard-log.sh").read_bytes()


def test_verify_reports_a_missing_guard_and_registration(world, carried):
    drift = projects.carried_guard_drift(FakeControlPlane({"proj": ("active", "normal")}))
    assert "proj: .claude/hooks/no-attribution-guard.sh is missing" in drift
    assert "proj: .claude/settings.json does not register the carried no-attribution-guard.sh" in drift
    assert "proj: .claude/hooks/env-dump-guard.sh is missing" in drift
    assert "proj: .claude/settings.json does not register the carried env-dump-guard.sh" in drift


def tracked_githooks(checkout: Path) -> None:
    shutil.rmtree(checkout / ".git")
    git("init", "-q", str(checkout))
    write(checkout / ".githooks" / "commit-msg", "exit 0\n")


def test_hooks_path_is_set_only_where_githooks_is_tracked(world, carried):
    tracked_githooks(carried)
    world.sync()
    assert git_config(carried, "--get", "core.hooksPath") == ""
    git("-C", str(carried), "add", ".githooks")
    acts = world.sync()
    assert f"git config core.hooksPath .githooks in {carried}" in acts
    assert git_config(carried, "--get", "core.hooksPath") == ".githooks"
    assert world.sync() == []


def test_hooks_path_already_set_elsewhere_is_left_alone(world, carried):
    tracked_githooks(carried)
    git("-C", str(carried), "add", ".githooks")
    git("-C", str(carried), "config", "core.hooksPath", "custom-hooks")
    acts = world.sync()
    assert git_config(carried, "--get", "core.hooksPath") == "custom-hooks"
    assert not any("core.hooksPath" in a for a in acts), acts
    assert world.sync() == []


def test_hooks_path_set_globally_is_respected(world, carried, tmp_path, monkeypatch):
    home = tmp_path / "git-home"
    home.mkdir()
    write(home / ".gitconfig", "[core]\n\thooksPath = /custom/hooks\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    tracked_githooks(carried)
    git("-C", str(carried), "add", ".githooks")
    acts = world.sync()
    assert git_config(carried, "--local", "--get", "core.hooksPath") == ""
    assert not any("core.hooksPath" in a for a in acts), acts


def test_replacing_the_registration_keeps_sibling_hooks_in_its_group(world, carried):
    sibling = {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/own.sh"}
    hand = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": _CARRIED_COMMAND}, sibling]}]}
    settings_path = write(carried / ".claude" / "settings.json", json.dumps({"hooks": hand}))
    world.sync()
    pre = json.loads(settings_path.read_text())["hooks"]["PreToolUse"]
    assert {"matcher": "Bash", "hooks": [sibling]} in pre
    assert carried_commands({"PreToolUse": pre}) == [_CARRIED_COMMAND]
    assert world.sync() == []


def test_the_carried_guard_denies_with_home_pointed_at_an_empty_directory(world, carried, tmp_path):
    world.sync()
    home = tmp_path / "empty-home"
    home.mkdir()
    body = write(tmp_path / "body.md", "Adds a check.\n\n" + _FOOTER + "\n")
    result = run_carried(carried, home, f"gh pr create --title x --body-file {body}")
    assert result.returncode == 0, result.stderr
    assert '"permissionDecision":"deny"' in result.stdout
    clean = write(tmp_path / "clean.md", "Adds a check.\n")
    result = run_carried(carried, home, f"gh pr create --title x --body-file {clean}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_the_carried_guard_still_denies_when_the_log_write_fails(world, carried, tmp_path):
    world.sync()
    home = tmp_path / "locked-home"
    home.mkdir()
    home.chmod(0o500)
    try:
        body = write(tmp_path / "body.md", _FOOTER + "\n")
        result = run_carried(carried, home, f"gh pr create --title x --body-file {body}")
    finally:
        home.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert '"permissionDecision":"deny"' in result.stdout
    assert not (home / ".agent-hooks").exists()


def test_the_carried_copy_runs_unless_the_user_level_guard_covers_every_pre_tool_call(world, carried, tmp_path):
    world.sync()
    body = write(tmp_path / "body.md", _FOOTER + "\n")
    command = f"gh pr create --title x --body-file {body}"
    for label, kwargs in (("post-tool-only", {"event": "PostToolUse"}), ("narrow-matcher", {"matcher": "Edit"})):
        home = tmp_path / label
        user_level_guard(home, **kwargs)
        assert '"permissionDecision":"deny"' in run_carried(carried, home, command).stdout, label
    for label, matcher in (("empty-matcher", ""), ("star-matcher", "*")):
        home = tmp_path / label
        user_level_guard(home, matcher=matcher)
        assert run_carried(carried, home, command).stdout == "", label


def test_the_carried_copy_runs_when_the_user_level_guard_is_not_executable(world, carried, tmp_path):
    world.sync()
    body = write(tmp_path / "body.md", _FOOTER + "\n")
    home = tmp_path / "home-a"
    user_level_guard(home)
    (home / ".claude" / "hooks" / "no-attribution-guard.sh").chmod(0o644)
    result = run_carried(carried, home, f"gh pr create --title x --body-file {body}")
    assert '"permissionDecision":"deny"' in result.stdout


def test_the_carried_copy_defers_to_an_identical_registered_user_level_guard(world, carried, tmp_path):
    world.sync()
    home = tmp_path / "home-b"
    user_level_guard(home)
    body = write(tmp_path / "body.md", _FOOTER + "\n")
    result = run_carried(carried, home, f"gh pr create --title x --body-file {body}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_the_carried_copy_runs_when_the_user_level_guard_differs_or_is_unregistered(world, carried, tmp_path):
    world.sync()
    body = write(tmp_path / "body.md", _FOOTER + "\n")
    command = f"gh pr create --title x --body-file {body}"
    stale = tmp_path / "stale-home"
    user_level_guard(stale)
    (stale / ".claude" / "hooks" / "lib" / "attribution-detect.py").write_text("# older\n")
    assert '"permissionDecision":"deny"' in run_carried(carried, stale, command).stdout
    unregistered = tmp_path / "unregistered-home"
    user_level_guard(unregistered, register=False)
    assert '"permissionDecision":"deny"' in run_carried(carried, unregistered, command).stdout


# cleared control-plane cells


def shared_plane(on: bool) -> str:
    """One active project, proj, with every shared and project option either opted in (on) or cleared."""
    x = "x" if on else ""
    return table(
        "# control-plane",
        "",
        "## projects",
        "",
        "| project | status | tier | template | origin |",
        "|---|---|---|---|---|",
        "| proj | active | normal | base | - |",
        "",
        "## shared",
        "",
        "| option | global | proj |",
        "|---|---|---|",
        f"| [agent:helper](agents/helper.md) |  | {x} |",
        f"| [command:ship](commands/ship.md) |  | {x} |",
        f"| [hook:gate](hooks/gate.sh) |  | {x} |",
        f"| [skill:drafting](skills/drafting/SKILL.md) |  | {x} |",
        "",
        "## project-local-configuration",
        "",
        "| option | global | proj |",
        "|---|---|---|",
        "| [project:AGENTS.md](projects-root/proj/AGENTS.md) |  | x |",
        f"| [project:scripts](projects-root/proj/scripts) |  | {x} |",
    )


DELIVERED = (
    ".claude/agents/helper.md",
    ".claude/commands/ship.md",
    ".claude/hooks/gate.sh",
    ".claude/hooks/lib/shared-helper.sh",
    ".claude/scripts/build.sh",
)


@pytest.fixture
def shared(world):
    """A source root with a shared agent, command, hook and skill, and one project with a script."""
    root = world.root
    write(root / "hooks" / "lib" / "shared-helper.sh", "# shared helper\n")
    write(root / "hooks" / "gate.sh", "#!/usr/bin/env bash\nexit 0\n")
    registry = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/gate.sh"}]}]}
    write(root / "hooks" / "hooks.json", json.dumps(registry))
    write(root / "agents" / "helper.md", "# helper\n")
    write(root / "commands" / "ship.md", "# ship\n")
    write(root / "skills" / "drafting" / "SKILL.md", "# drafting\n")
    write(root / "skills" / "drafting" / "references" / "notes.md", "notes\n")
    source = world.source()
    write(source / "scripts" / "build.sh", "echo build\n")
    return world.checkout()


def sync_shared(world, on: bool, dry: bool = False) -> list[str]:
    cp = world.plane(shared_plane(on))
    try:
        return projects.sync_project("proj", dry=dry, cp=cp)
    finally:
        staging.cleanup_all()


def test_cleared_cells_are_removed_and_check_is_stale_until_then(world, shared):
    sync_shared(world, on=True)
    for rel in DELIVERED:
        assert (shared / rel).is_file(), rel
    settings_path = shared / ".claude" / "settings.json"
    gate = "$CLAUDE_PROJECT_DIR/.claude/hooks/gate.sh"
    assert gate in settings_path.read_text()
    # Files nobody shipped are the project's own.
    write(shared / ".claude" / "agents" / "local.md", "mine\n")
    write(shared / ".claude" / "scripts" / "local.sh", "mine\n")
    dry = sync_shared(world, on=False, dry=True)
    for rel in DELIVERED:
        assert f"remove {shared / rel}" in dry
        assert (shared / rel).is_file(), rel
    sync_shared(world, on=False)
    for rel in DELIVERED:
        assert not (shared / rel).exists(), rel
    assert (shared / ".claude" / "agents" / "local.md").is_file()
    assert (shared / ".claude" / "scripts" / "local.sh").is_file()
    for rel in projects.CARRIED_GUARD_FILES:
        assert (shared / ".claude" / "hooks" / rel).is_file(), rel
    settings = json.loads(settings_path.read_text())
    assert gate not in json.dumps(settings)
    assert carried_commands(settings["hooks"]) == [_CARRIED_COMMAND]
    assert sync_shared(world, on=False) == []


def test_the_codex_hook_copy_is_removed_with_the_claude_copy(world, shared):
    sync_shared(world, on=True)
    codex = shared / ".codex" / "hooks" / "gate.sh"
    assert codex.is_file()
    sync_shared(world, on=False)
    assert not codex.exists()
    codex_json = shared / ".codex" / "hooks.json"
    if codex_json.exists():
        assert "gate.sh" not in codex_json.read_text()


def test_a_project_column_skill_is_delivered_and_removed_when_cleared(world, shared):
    write(world.root / "skills" / "sync-exclude.json", '{"exclude": {}}\n')
    sync_shared(world, on=True)
    skill = shared / ".claude" / "skills" / "drafting"
    assert (skill / "SKILL.md").read_text() == "# drafting\n"
    assert (skill / "references" / "notes.md").read_text() == "notes\n"
    assert not (shared / ".claude" / "skills" / "sync-exclude.json").exists()
    assert sync_shared(world, on=True) == []
    sync_shared(world, on=False)
    assert not (skill / "SKILL.md").exists()
    assert not (skill / "references" / "notes.md").exists()


def test_deleted_source_files_are_removed_from_both_hook_trees(world, shared):
    root = world.root
    write(root / "skills" / "sync-exclude.json", '{"exclude": {}}\n')
    sources = (
        root / "agents" / "helper.md",
        root / "commands" / "ship.md",
        root / "hooks" / "lib" / "shared-helper.sh",
        root / "projects-root" / "proj" / "scripts" / "build.sh",
        root / "skills" / "drafting" / "SKILL.md",
        root / "skills" / "drafting" / "references" / "notes.md",
    )
    delivered = (
        shared / ".claude" / "agents" / "helper.md",
        shared / ".claude" / "commands" / "ship.md",
        shared / ".claude" / "hooks" / "lib" / "shared-helper.sh",
        shared / ".codex" / "hooks" / "lib" / "shared-helper.sh",
        shared / ".claude" / "scripts" / "build.sh",
        shared / ".claude" / "skills" / "drafting" / "SKILL.md",
        shared / ".claude" / "skills" / "drafting" / "references" / "notes.md",
    )
    sync_shared(world, on=True)
    for path in delivered:
        assert path.is_file(), path
    manifest = shared / projects.delivered_manifest()
    before = manifest.read_bytes()
    local = write(shared / ".claude" / "agents" / "local.md", "mine\n")
    for path in sources:
        path.unlink()
    dry = sync_shared(world, on=True, dry=True)
    for path in delivered:
        assert f"remove {path}" in dry
        assert path.is_file(), path
    assert manifest.read_bytes() == before
    sync_shared(world, on=True)
    for path in delivered:
        assert not path.exists(), path
    assert local.read_text() == "mine\n"
    after = manifest.read_bytes()
    assert not any(a.startswith(("write ", "remove ")) for a in sync_shared(world, on=True))
    assert manifest.read_bytes() == after


def test_a_locally_edited_retired_hook_is_kept(world, shared):
    """Wiring the retired list into the prune must not bypass the locally-edited check."""
    sync_shared(world, on=True)
    gate = shared / ".claude" / "hooks" / "gate.sh"
    gate.write_text("#!/usr/bin/env bash\nexit 0\n# edited locally\n")
    (shared / ".codex" / "hooks" / "gate.sh").write_text("#!/usr/bin/env bash\nexit 0\n# edited locally\n")
    (world.root / "hooks" / "gate.sh").unlink()
    write(world.root / "hooks" / "retired.json", json.dumps({"retired": ["gate.sh"]}) + "\n")
    acts = sync_shared(world, on=True)
    assert gate.read_text() == "#!/usr/bin/env bash\nexit 0\n# edited locally\n"
    assert not [a for a in acts if "gate.sh" in a], acts


def test_dropping_registrations_keeps_everything_that_does_not_run_a_removed_file():
    prefix = "$CLAUDE_PROJECT_DIR/.claude/hooks/"
    hooks = {
        "Stop": [],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"command": prefix + "gate.sh"}, {"command": prefix + "gate.sh.bak"}]},
            {"hooks": [{"command": prefix + "gate.sh --strict"}]},
            "not a group",
        ],
        "PostToolUse": [{"hooks": [{"command": prefix + "gate.sh"}]}],
    }
    assert projects._drop_registrations(hooks, prefix, {"gate.sh"}) == {
        "Stop": [],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"command": prefix + "gate.sh.bak"}]},
            "not a group",
        ],
    }
    assert projects._drop_registrations(hooks, prefix, set()) == hooks


def test_a_skill_listed_in_sync_exclude_is_not_delivered(world, shared):
    write(world.root / "skills" / "sync-exclude.json", '{"exclude": {"drafting": "host adapted"}}\n')
    sync_shared(world, on=True)
    assert not (shared / ".claude" / "skills" / "drafting").exists()


def test_a_newly_excluded_skill_is_pruned_without_a_prior_manifest(world, shared):
    write(world.root / "skills" / "sync-exclude.json", '{"exclude": {}}\n')
    sync_shared(world, on=True)
    skill = shared / ".claude" / "skills" / "drafting"
    delivered = (skill / "SKILL.md", skill / "references" / "notes.md")
    for path in delivered:
        assert path.is_file(), path
    (shared / projects.delivered_manifest()).unlink()
    write(world.root / "skills" / "sync-exclude.json", '{"exclude": {"drafting": "host adapted"}}\n')
    dry = sync_shared(world, on=True, dry=True)
    for path in delivered:
        assert f"remove {path}" in dry
        assert path.is_file(), path
    sync_shared(world, on=True)
    for path in delivered:
        assert not path.exists(), path
    assert sync_shared(world, on=True) == []


def test_a_locally_edited_delivered_agent_is_kept_when_its_cell_is_cleared(world, shared, capsys):
    """The manifest records each delivered file's content hash: an edited file is kept and reported, an untouched one removed."""
    sync_shared(world, on=True)
    edited = shared / ".claude" / "agents" / "helper.md"
    edited.write_text("# helper, edited in the project\n")
    capsys.readouterr()
    acts = sync_shared(world, on=False)
    assert f"remove {edited}" not in acts
    assert edited.read_text() == "# helper, edited in the project\n"
    assert "locally edited" in capsys.readouterr().err
    assert not (shared / ".claude" / "commands" / "ship.md").exists()
    assert sync_shared(world, on=False) == []
    assert edited.is_file()


# project skills

SKILL = Path(".claude") / "skills" / "local-skill" / "SKILL.md"
REFERENCE = Path(".claude") / "skills" / "local-skill" / "references" / "notes.md"
AGENTS_SKILL = Path(".agents") / "skills" / "local-skill" / "SKILL.md"
SHARED_ROW = (
    "## shared\n\n| option | global | proj |\n|---|---|---|\n"
    "| [skill:{name}](skills/{name}/SKILL.md) |  | x |\n\n"
    "## project-local-configuration"
)


def skill_plane(skills: bool = True, shared_skill: str | None = None) -> str:
    x = "x" if skills else ""
    text = table(
        "# control-plane",
        "",
        "## projects",
        "",
        "| project | status | tier | template | origin |",
        "|---|---|---|---|---|",
        "| proj | active | normal | base | - |",
        "",
        "## project-local-configuration",
        "",
        "| option | global | proj |",
        "|---|---|---|",
        "| [project:AGENTS.md](projects-root/proj/AGENTS.md) |  | x |",
        f"| [project:skills](projects-root/proj/skills/local-skill/SKILL.md) |  | {x} |",
    )
    if shared_skill:
        text = text.replace("## project-local-configuration", SHARED_ROW.format(name=shared_skill))
    return text


@pytest.fixture
def skills(world):
    source = world.source()
    skill = source / "skills" / "local-skill"
    write(skill / "SKILL.md", "---\nname: local-skill\ndescription: fixture\n---\n\n# local-skill\n")
    write(skill / "references" / "notes.md", "notes\n")
    write(world.root / "control-plane.md", skill_plane())
    return world.checkout()


def reload_plane(world) -> ControlPlane:
    return ControlPlane.load(world.root / "control-plane.md")


def sync_skills(world, dry: bool = False) -> list[str]:
    try:
        return projects.sync_project("proj", dry=dry, cp=reload_plane(world))
    finally:
        staging.cleanup_all()


def verify_skills(world) -> tuple[list[str], list[str]]:
    try:
        return projects.skill_drift(reload_plane(world))
    finally:
        staging.cleanup_all()


def set_plane(world, text: str) -> None:
    write(world.root / "control-plane.md", text)


def source_skill(world) -> Path:
    return world.src / "proj" / "skills" / "local-skill"


def test_a_project_skill_reaches_its_checkout_and_verify_is_clean(world, skills):
    source = source_skill(world)
    sync_skills(world)
    assert (skills / SKILL).read_bytes() == (source / "SKILL.md").read_bytes()
    assert (skills / REFERENCE).read_text() == "notes\n"
    assert (skills / AGENTS_SKILL).read_bytes() == (source / "SKILL.md").read_bytes()
    manifest = manifest_of(skills)
    assert SKILL.as_posix() in manifest["paths"]
    assert AGENTS_SKILL.as_posix() in manifest["paths"]
    digest = hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest()
    assert manifest["hashes"][SKILL.as_posix()] == projects._manifest_hash(digest)
    assert verify_skills(world) == ([], [])
    assert sync_skills(world) == []


def test_an_unrecorded_agents_skill_collision_is_kept_and_not_manifested(world, skills, capsys):
    own = skills / AGENTS_SKILL
    write(own, "# project authored\n")
    acts = sync_skills(world)
    assert own.read_text() == "# project authored\n"
    assert (skills / SKILL).is_file()
    assert not any("collision" in act for act in acts)
    assert f"collision {own}: project file kept" in capsys.readouterr().err
    manifest = manifest_of(skills)
    assert AGENTS_SKILL.as_posix() not in manifest["paths"]
    assert AGENTS_SKILL.as_posix() not in manifest["hashes"]
    assert sync_skills(world, dry=True) == []
    drift, notes = verify_skills(world)
    assert drift == []
    assert f"proj: {AGENTS_SKILL.as_posix()} is a protected project collision, kept" in notes
    sync_skills(world)
    assert own.read_text() == "# project authored\n"


def test_an_identical_unrecorded_skill_is_not_claimed_or_pruned(world, skills):
    own = skills / AGENTS_SKILL
    own.parent.mkdir(parents=True)
    own.write_bytes((source_skill(world) / "SKILL.md").read_bytes())
    sync_skills(world)
    assert AGENTS_SKILL.as_posix() not in manifest_of(skills)["paths"]
    set_plane(world, skill_plane(skills=False))
    sync_skills(world)
    assert own.is_file()


def test_verify_exit_code_counts_skill_drift(world, skills, capsys):
    sync_skills(world)
    capsys.readouterr()
    assert projects.main(["--verify", "--skip-reconcile"]) == 0, capsys.readouterr().out
    capsys.readouterr()
    (skills / AGENTS_SKILL).write_text("stale\n")
    assert projects.main(["--verify", "--skip-reconcile"]) == 1
    assert f"verify: skill proj: {AGENTS_SKILL.as_posix()} is changed from source" in capsys.readouterr().out


def test_verify_reports_missing_changed_and_extra_skill_files(world, skills):
    assert sorted(verify_skills(world)[0]) == sorted(
        f"proj: {tree}/skills/local-skill/{rel} is missing"
        for tree in (".claude", ".agents")
        for rel in ("SKILL.md", "references/notes.md")
    )
    sync_skills(world)
    (skills / SKILL).write_text("stale\n")
    (skills / REFERENCE).unlink()
    # A delivered file whose source is gone is extra until a sync prunes it.
    (source_skill(world) / "references" / "notes.md").unlink()
    write(skills / REFERENCE, "notes\n")
    # A file the project added inside a delivered skill is a note: no sync removes it.
    added = write(skills / ".claude" / "skills" / "local-skill" / "scratch.md", "the project's own\n")
    drift, notes = verify_skills(world)
    assert sorted(drift) == sorted(
        [
            f"proj: {SKILL.as_posix()} is changed from source",
            f"proj: {REFERENCE.as_posix()} is extra, not in source",
            "proj: .agents/skills/local-skill/references/notes.md is extra, not in source",
        ]
    )
    assert notes == ["proj: .claude/skills/local-skill/scratch.md is the project's own, inside a delivered skill"]
    (skills / AGENTS_SKILL).unlink()
    assert f"proj: {AGENTS_SKILL.as_posix()} is missing" in verify_skills(world)[0]
    own = write(skills / ".claude" / "skills" / "handmade" / "SKILL.md", "# handmade\n")
    assert not any("handmade" in d for d in sum(verify_skills(world), []))
    sync_skills(world)
    assert verify_skills(world)[0] == []
    assert added.read_text() == "the project's own\n"
    assert own.is_file()


def test_a_project_skill_removed_from_source_is_pruned(world, skills):
    sync_skills(world)
    shutil.rmtree(world.src / "proj" / "skills")
    dry = sync_skills(world, dry=True)
    assert f"remove {skills / SKILL}" in dry
    assert (skills / SKILL).is_file()
    sync_skills(world)
    assert not (skills / SKILL).exists()
    assert not (skills / REFERENCE).exists()
    assert not (skills / ".claude" / "skills" / "local-skill").exists()
    assert not (skills / ".agents" / "skills" / "local-skill").exists()
    assert verify_skills(world)[0] == []
    assert sync_skills(world) == []


def test_a_project_skill_whose_cell_is_cleared_is_pruned(world, skills):
    sync_skills(world)
    assert (skills / SKILL).is_file()
    set_plane(world, skill_plane(skills=False))
    sync_skills(world)
    assert not (skills / SKILL).exists()
    assert not (skills / AGENTS_SKILL).exists()
    assert verify_skills(world)[0] == []


def test_a_locally_edited_skill_file_is_kept_and_reported_never_deleted(world, skills, capsys):
    sync_skills(world)
    (skills / SKILL).write_text("edited in the project\n")
    shutil.rmtree(world.src / "proj" / "skills")
    capsys.readouterr()
    acts = sync_skills(world)
    assert f"remove {skills / SKILL}" not in acts
    assert f"remove {skills / REFERENCE}" in acts
    assert (skills / SKILL).read_text() == "edited in the project\n"
    assert "locally edited" in capsys.readouterr().err
    assert not (skills / AGENTS_SKILL).exists()
    assert verify_skills(world) == ([], [f"proj: {SKILL.as_posix()} is locally edited since delivery, kept"])
    assert sync_skills(world) == []
    assert (skills / SKILL).is_file()


def test_a_project_skill_named_like_an_excluded_shared_skill_is_delivered(world, skills):
    write(world.root / "skills" / "sync-exclude.json", '{"exclude": {"local-skill": "host adapted"}}\n')
    sync_skills(world)
    assert (skills / SKILL).is_file()
    assert verify_skills(world)[0] == []


def shared_local_skill(world) -> None:
    write(world.root / "skills" / "local-skill" / "SKILL.md", "# shared\n")


def test_an_excluded_shared_skill_stays_excluded_when_project_skills_are_off(world, skills):
    shared_local_skill(world)
    write(world.root / "skills" / "sync-exclude.json", '{"exclude": {"local-skill": "host adapted"}}\n')
    set_plane(world, skill_plane(skills=False, shared_skill="local-skill"))
    sync_skills(world)
    assert not (skills / SKILL).exists()
    assert verify_skills(world)[0] == []


def test_a_project_exclusion_does_not_hide_a_shared_skill_when_project_skills_are_off(world, skills):
    shared_local_skill(world)
    write(world.src / "proj" / "skills" / "sync-exclude.json", '{"exclude": {"local-skill": "project adapted"}}\n')
    set_plane(world, skill_plane(skills=False, shared_skill="local-skill"))
    sync_skills(world)
    assert (skills / SKILL).read_text() == "# shared\n"
    assert verify_skills(world)[0] == []


def test_a_project_skill_replaces_a_shared_skill_of_the_same_name_whole(world, skills, capsys):
    shared_local_skill(world)
    write(world.root / "skills" / "local-skill" / "shared-only.md", "shared\n")
    set_plane(world, skill_plane(shared_skill="local-skill"))
    capsys.readouterr()
    sync_skills(world)
    text = (skills / SKILL).read_text()
    assert "local-skill" in text
    assert "shared" not in text
    assert not (skills / ".claude" / "skills" / "local-skill" / "shared-only.md").exists()
    assert "replaces the shared skill:local-skill" in capsys.readouterr().err


def test_a_hashed_manifest_prunes_only_what_it_delivered(world, skills):
    """A file the project wrote at a path the engine could ship but never delivered is kept once the manifest is the complete record."""
    write(world.root / "skills" / "drafting" / "SKILL.md", "# drafting\n")
    sync_skills(world)
    own = write(skills / ".claude" / "skills" / "drafting" / "SKILL.md", "# the project's own drafting\n")
    assert sync_skills(world) == []
    assert own.is_file()
    assert verify_skills(world)[0] == []


def test_a_list_only_manifest_from_before_hashes_still_prunes_its_paths(world, skills):
    old = write(skills / ".claude" / "agents" / "renamed-away.md", "# once delivered\n")
    manifest = write(skills / projects.delivered_manifest(), json.dumps({"paths": [".claude/agents/renamed-away.md"]}))
    acts = sync_skills(world)
    assert f"remove {old}" in acts
    assert not old.exists()
    assert "hashes" in json.loads(manifest.read_text())


def test_adopt_captures_project_skills_and_opts_them_in(world):
    path = write(
        world.root / "control-plane.md",
        "| project | status | tier | template | origin |\n"
        "|---|---|---|---|---|\n"
        "| proj | active | normal | base | - |\n"
        "\n"
        "| option | global | proj |\n"
        "|---|---|---|\n"
        "| project:skills |  |  |\n",
    )
    checkout = world.checkout()
    write(checkout / "AGENTS.md", "# proj\n")
    write(checkout / ".claude" / "skills" / "handmade" / "SKILL.md", "# handmade\n")
    acts = projects.adopt("proj", ControlPlane.load(path))
    assert "captured .claude/skills" in acts
    assert (world.src / "proj" / "skills" / "handmade" / "SKILL.md").read_text() == "# handmade\n"
    assert ControlPlane.load(path).enabled("proj", "project:skills"), acts


def test_adopt_captures_locally_edited_delivered_files(world, skills):
    write(world.root / "skills" / "shared" / "SKILL.md", "# shared original\n")
    set_plane(world, skill_plane(shared_skill="shared"))
    sync_skills(world)
    edited_project_skill = skills / SKILL
    edited_shared_skill = skills / ".claude" / "skills" / "shared" / "SKILL.md"
    edited_project_skill.write_text("project edit\n")
    edited_shared_skill.write_text("shared edit\n")
    projects.adopt("proj", reload_plane(world))
    source = world.src / "proj"
    assert (source / "skills" / "local-skill" / "SKILL.md").read_text() == "project edit\n"
    assert (source / "skills" / "shared" / "SKILL.md").read_text() == "shared edit\n"
    sync_skills(world)
    assert edited_project_skill.read_text() == "project edit\n"
    assert edited_shared_skill.read_text() == "shared edit\n"


def paths_only(checkout: Path) -> None:
    """Rewrite the delivered manifest into the list-only form that predates hashes."""
    path = checkout / projects.delivered_manifest()
    manifest = json.loads(path.read_text())
    path.write_text(json.dumps({"paths": manifest["paths"]}) + "\n")


def test_adopt_captures_the_unknown_paths_only_manifest_entry(world, skills):
    sync_skills(world)
    paths_only(skills)
    (skills / AGENTS_SKILL).unlink()
    edited = skills / SKILL
    edited.write_text("local edit from old checkout\n")
    acts = projects.adopt("proj", reload_plane(world))
    assert "captured .claude/skills" in acts
    assert (source_skill(world) / "SKILL.md").read_text() == "local edit from old checkout\n"
    sync_skills(world)
    assert edited.read_text() == "local edit from old checkout\n"


def test_adopt_skips_an_unchanged_shared_skill_in_a_paths_only_manifest(world, skills):
    write(world.root / "skills" / "shared" / "SKILL.md", "# shared original\n")
    set_plane(world, skill_plane(shared_skill="shared"))
    sync_skills(world)
    paths_only(skills)
    acts = projects.adopt("proj", reload_plane(world))
    assert "captured .claude/skills" in acts
    assert not (world.src / "proj" / "skills" / "shared").exists()


def test_adopt_skips_a_shared_skill_changed_at_source_in_a_paths_only_manifest(world, skills):
    shared = write(world.root / "skills" / "shared" / "SKILL.md", "# shared original\n")
    set_plane(world, skill_plane(shared_skill="shared"))
    sync_skills(world)
    paths_only(skills)
    shared.write_text("# shared updated at source\n")
    acts = projects.adopt("proj", reload_plane(world))
    assert not (world.src / "proj" / "skills" / "shared").exists()
    assert (
        "note skills/shared/SKILL.md: differs from shared source; a paths-only manifest cannot show a local edit, not captured"
        in acts
    )
    sync_skills(world)
    assert (skills / ".claude" / "skills" / "shared" / "SKILL.md").read_text() == "# shared updated at source\n"


def test_adopt_captures_a_skill_edited_only_in_the_agents_tree(world, skills):
    sync_skills(world)
    agents_skill = skills / AGENTS_SKILL
    agents_skill.write_text("agents tree edit\n")
    projects.adopt("proj", reload_plane(world))
    assert (source_skill(world) / "SKILL.md").read_text() == "agents tree edit\n"
    sync_skills(world)
    assert agents_skill.read_text() == "agents tree edit\n"


def test_adopt_reports_conflicting_edits_without_choosing_a_copy(world, skills):
    sync_skills(world)
    source = source_skill(world) / "SKILL.md"
    original = source.read_bytes()
    (skills / SKILL).write_text("claude edit\n")
    (skills / AGENTS_SKILL).write_text("agents edit\n")
    acts = projects.adopt("proj", reload_plane(world))
    assert any("conflict" in act and "local-skill/SKILL.md" in act for act in acts)
    assert source.read_bytes() == original
    assert (skills / SKILL).read_text() == "claude edit\n"
    assert (skills / AGENTS_SKILL).read_text() == "agents edit\n"


def test_adopt_captures_a_new_skill_authored_only_in_the_agents_tree(world, skills):
    sync_skills(world)
    agents_skill = write(skills / ".agents" / "skills" / "handmade" / "SKILL.md", "# handmade\n")
    acts = projects.adopt("proj", reload_plane(world))
    assert "captured .agents/skills" in acts
    assert (world.src / "proj" / "skills" / "handmade" / "SKILL.md").read_text() == "# handmade\n"
    sync_skills(world)
    assert agents_skill.read_text() == "# handmade\n"


def test_adopt_does_not_capture_an_unrecorded_shared_skill_from_the_agents_tree(world, skills):
    write(world.root / "skills" / "shared" / "SKILL.md", "# shared\n")
    write(skills / ".agents" / "skills" / "shared" / "SKILL.md", "# shared checkout\n")
    acts = projects.adopt("proj", reload_plane(world))
    assert "captured .agents/skills" not in acts
    assert not (world.src / "proj" / "skills" / "shared").exists()


# shared directory lists


def test_the_sync_and_the_reconciler_share_stagings_directories():
    from stratarc import reconcile

    assert "skills" in staging.PROJECT_DIRECTORIES
    assert projects.CLAUDE_DIRS is staging.PROJECT_DIRECTORIES
    assert reconcile.CONFIG_DIRECTORIES is staging.PROJECT_DIRECTORIES


def test_scripts_means_the_projects_own_helper_scripts(world):
    """The "scripts" entry delivers projects-root/<name>/scripts/ to .claude/scripts/; the engine's own scripts directory is never read."""
    assert "scripts" in projects.CLAUDE_DIRS
    source = world.source()
    write(source / "scripts" / "build.sh", "echo build\n")
    write(world.root / "scripts" / "engine-only.sh", "echo not for projects\n")
    checkout = world.checkout()
    world.sync()
    assert (checkout / ".claude" / "scripts" / "build.sh").read_text() == "echo build\n"
    assert not (checkout / ".claude" / "scripts" / "engine-only.sh").exists()


# component selection

COMPONENTS = {
    "version": 1,
    "plugins": [
        {"name": "plug", "runtimes": ["claude"], "owner": "anthropic", "wanted": True, "marketplace": "m"},
    ],
    "mcp_servers": [
        {"name": "srv", "runtimes": ["claude"], "owner": "stratarc", "wanted": True, "command": "srv", "args": ["--stdio"]},
        {
            "name": "vault",
            "runtimes": ["claude"],
            "owner": "stratarc",
            "wanted": True,
            "command": "v",
            "env": {"KEY": "secret://vault/key"},
        },
    ],
}


def components_plane(on: bool) -> str:
    x = "x" if on else ""
    return table(
        "# control-plane",
        "",
        "## projects",
        "",
        "| project | status | tier | template | origin |",
        "|---|---|---|---|---|",
        "| proj | active | normal | base | - |",
        "",
        "## shared",
        "",
        "| option | global | proj |",
        "|---|---|---|",
        f"| [mcp:srv](components.json) |  | {x} |",
        f"| [mcp:vault](components.json) |  | {x} |",
        f"| [plugin:plug](components.json) |  | {x} |",
        "",
        "## project-local-configuration",
        "",
        "| option | global | proj |",
        "|---|---|---|",
        "| [project:AGENTS.md](projects-root/proj/AGENTS.md) |  | x |",
    )


@pytest.fixture
def components(world):
    write(world.root / "components.json", json.dumps(COMPONENTS))
    world.source()
    checkout = world.projects / "active" / "proj"
    checkout.mkdir(parents=True)
    git("init", "-q", str(checkout))
    return checkout


def sync_components(world, on: bool, dry: bool = False) -> tuple[list[str], str]:
    import contextlib
    import io

    cp = world.plane(components_plane(on))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            acts = projects.sync_project("proj", dry=dry, cp=cp)
        finally:
            staging.cleanup_all()
    return acts, err.getvalue()


def local_settings(checkout: Path) -> dict:
    return json.loads((checkout / ".claude" / "settings.local.json").read_text())


def account_servers(home: Path, checkout: Path) -> dict:
    account = json.loads((home / ".claude.json").read_text())
    return account["projects"][str(checkout.resolve())].get("mcpServers", {})


def test_the_selection_lands_in_local_scope_and_no_tracked_file(world, components, stratarc_home):
    _acts, err = sync_components(world, on=True)
    assert local_settings(components)["enabledPlugins"] == {"plug@m": True}
    assert account_servers(stratarc_home, components) == {"srv": {"command": "srv", "args": ["--stdio"], "type": "stdio"}}
    # A secret:// value is never written unresolved; it is named instead.
    assert "vault" in err
    settings = json.loads((components / ".claude" / "settings.json").read_text())
    assert "enabledPlugins" not in settings
    assert "mcpServers" not in settings
    assert not (components / ".mcp.json").exists()
    _code, tracked = projects._git_out(components, "ls-files", "--", ".claude/settings.local.json")
    assert tracked == ""


def test_new_local_settings_are_ignored_before_creation(world, components):
    sync_components(world, on=True)
    code, _ = projects._git_out(components, "-c", "core.excludesFile=/dev/null", "check-ignore", "-q", ".claude/settings.local.json")
    assert code == 0


def test_a_local_ignore_override_prevents_settings_creation(world, components):
    write(components / ".claude" / ".gitignore", "!settings.local.json\n")
    actions, _err = sync_components(world, on=True)
    assert not (components / ".claude" / "settings.local.json").exists()
    assert any("component render blocked" in action for action in actions), actions
    sync_components(world, on=True)
    code, location = projects._git_out(components, "rev-parse", "--git-path", "info/exclude")
    assert code == 0
    exclude = Path(location)
    if not exclude.is_absolute():
        exclude = components / exclude
    assert exclude.read_text().count("/.claude/settings.local.json") == 1


def test_merge_by_key_keeps_every_other_key_and_hand_added_entry(world, components, stratarc_home):
    write(
        components / ".claude" / "settings.local.json",
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "enabledPlugins": {"mine@x": True, "plug@m": False}}),
    )
    key = str(components.resolve())
    write(
        stratarc_home / ".claude.json",
        json.dumps({"numStartups": 3, "projects": {key: {"allowedTools": ["Read"], "mcpServers": {"handmade": {"command": "mine"}}}}}),
    )
    sync_components(world, on=True)
    assert local_settings(components) == {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "enabledPlugins": {"mine@x": True, "plug@m": True},
    }
    account = json.loads((stratarc_home / ".claude.json").read_text())
    assert account["numStartups"] == 3
    assert account["projects"][key]["allowedTools"] == ["Read"]
    assert sorted(account["projects"][key]["mcpServers"]) == ["handmade", "srv"]


def test_clearing_the_cells_removes_only_what_the_manifest_declares(world, components, stratarc_home):
    sync_components(world, on=True)
    local = local_settings(components)
    local["enabledPlugins"]["mine@x"] = True
    write(components / ".claude" / "settings.local.json", json.dumps(local))
    dry, _err = sync_components(world, on=False, dry=True)
    assert any("settings.local.json" in a for a in dry), dry
    assert any(".claude.json" in a for a in dry), dry
    sync_components(world, on=False)
    assert local_settings(components)["enabledPlugins"] == {"mine@x": True}
    assert account_servers(stratarc_home, components) == {}
    assert sync_components(world, on=False)[0] == []


def test_removed_manifest_declarations_prune_previous_project_keys(world, components, stratarc_home):
    sync_components(world, on=True)
    local = local_settings(components)
    local["enabledPlugins"]["mine@x"] = True
    write(components / ".claude" / "settings.local.json", json.dumps(local))
    account_path = stratarc_home / ".claude.json"
    account = json.loads(account_path.read_text())
    account["projects"][str(components.resolve())]["mcpServers"]["handmade"] = {"command": "mine"}
    account_path.write_text(json.dumps(account))
    write(world.root / "components.json", json.dumps({"version": 1}))
    dry, _err = sync_components(world, on=True, dry=True)
    assert any("settings.local.json" in action for action in dry), dry
    assert any(".claude.json" in action for action in dry), dry
    sync_components(world, on=True)
    assert local_settings(components)["enabledPlugins"] == {"mine@x": True}
    assert account_servers(stratarc_home, components) == {"handmade": {"command": "mine"}}
    assert sync_components(world, on=True)[0] == []


def test_a_marketplace_change_prunes_the_old_plugin_key(world, components):
    sync_components(world, on=True)
    changed = dict(COMPONENTS, plugins=[dict(COMPONENTS["plugins"][0], marketplace="new")])
    write(world.root / "components.json", json.dumps(changed))
    sync_components(world, on=True)
    assert local_settings(components)["enabledPlugins"] == {"plug@new": True}


def test_skipped_cleanup_keeps_previous_keys_for_a_later_sync(world, components, stratarc_home):
    sync_components(world, on=True)
    local_path = components / ".claude" / "settings.local.json"
    account_path = stratarc_home / ".claude.json"
    local, account = local_path.read_text(), account_path.read_text()
    local_path.write_text("{invalid")
    account_path.write_text("{invalid")
    write(world.root / "components.json", json.dumps({"version": 1}))
    sync_components(world, on=True)
    local_path.write_text(local)
    account_path.write_text(account)
    sync_components(world, on=True)
    assert "enabledPlugins" not in local_settings(components)
    assert account_servers(stratarc_home, components) == {}


def test_the_account_file_keeps_its_permissions(world, components, stratarc_home):
    account = write(stratarc_home / ".claude.json", "{}\n")
    account.chmod(0o600)
    sync_components(world, on=True)
    assert "srv" in account_servers(stratarc_home, components)
    assert account.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in stratarc_home.iterdir()) == [".claude.json"]


def test_a_second_component_sync_is_current(world, components):
    sync_components(world, on=True)
    assert sync_components(world, on=True)[0] == []


def test_a_tracked_settings_local_is_never_written(world, components):
    local = write(components / ".claude" / "settings.local.json", "{}\n")
    git("-C", str(components), "add", "-f", ".claude/settings.local.json")
    _acts, err = sync_components(world, on=True)
    assert local.read_text() == "{}\n"
    assert "tracked" in err


@pytest.mark.parametrize("refusal", ["tracked settings", "malformed settings", "malformed account"])
def test_a_refused_component_render_is_reported_as_stale(world, components, stratarc_home, refusal):
    sync_components(world, on=True)
    if refusal == "tracked settings":
        git("-C", str(components), "add", "-f", ".claude/settings.local.json")
    elif refusal == "malformed settings":
        (components / ".claude" / "settings.local.json").write_text("{invalid")
    else:
        (stratarc_home / ".claude.json").write_text("{invalid")
    actions, _err = sync_components(world, on=True, dry=True)
    assert any("component render blocked" in action for action in actions), actions


def test_nothing_declared_touches_nothing(world, components, stratarc_home):
    write(world.root / "components.json", json.dumps({"version": 1}))
    sync_components(world, on=True)
    assert not (components / ".claude" / "settings.local.json").exists()
    assert not (stratarc_home / ".claude.json").exists()


def test_the_owner_of_a_declared_server_is_the_engine_name(world, components, stratarc_home):
    """A server another owner installs is not the engine's to render."""
    foreign = json.loads(json.dumps(COMPONENTS))
    for server in foreign["mcp_servers"]:
        server["owner"] = "someone-else"
    write(world.root / "components.json", json.dumps(foreign))
    sync_components(world, on=True)
    assert not (stratarc_home / ".claude.json").exists()


# source trees are never targets


def source_tree_checkout(projects_dir: Path, name: str) -> Path:
    checkout = projects_dir / "active" / name
    (checkout / ".git").mkdir(parents=True)
    (checkout / "projects-root").mkdir()
    write(checkout / "control-plane.md", "# control-plane\n")
    write(checkout / "AGENTS.md", "# agents\n")
    return checkout


def test_resolve_refuses_the_primary_from_a_linked_worktree(world, tmp_path):
    primary = world.projects / "active" / "source-repo"
    git("init", "-q", str(primary))
    git(*GIT_IDENTITY, "-C", str(primary), "commit", "--allow-empty", "-m", "fixture")
    linked = tmp_path / "_worktrees" / "source-repo-lane"
    git("-C", str(primary), "worktree", "add", "-q", str(linked), "-b", "fixture-lane")
    projects.configure_root(linked)
    dst, why = projects.resolve("source-repo", FakeControlPlane({"source-repo": ("active", "normal")}))
    assert dst is None
    assert "canonical" in why


def test_resolve_refuses_a_source_tree_when_git_cannot_name_it(world, monkeypatch, tmp_path):
    """canonical_checkout() falls back to ROOT when git fails, which from a linked worktree or a second clone is not the checkout being synced into."""
    checkout = source_tree_checkout(world.projects, "source-repo")
    elsewhere = tmp_path / "_worktrees" / "source-repo-lane"
    write(elsewhere / "projects-root" / "source-repo" / "AGENTS.md", "# stale\n")
    projects.configure_root(elsewhere)
    monkeypatch.setattr(projects, "canonical_checkout", lambda: elsewhere)
    cp = FakeControlPlane({"source-repo": ("active", "normal")})
    dst, why = projects.resolve("source-repo", cp)
    acts = projects.sync_project("source-repo", dry=False, cp=None)
    assert dst is None
    assert "source tree" in why
    assert len(acts) == 1, acts
    assert acts[0].startswith("skip"), acts
    for name in ("CLAUDE.md", "CODEX.md", "GEMINI.md"):
        assert not (checkout / name).exists(), name
    assert (checkout / "AGENTS.md").read_text() == "# agents\n"


# gitignored delivered paths


def ignored_checkout(tmp_path: Path, gitignore: str | None = None) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git("init", "-q", str(checkout))
    if gitignore is not None:
        write(checkout / ".gitignore", gitignore)
    return checkout


def test_a_gitignored_delivered_path_under_claude_hooks_is_noted(tmp_path, capsys):
    checkout = ignored_checkout(tmp_path, "lib/\n")
    write(checkout / ".claude" / "hooks" / "lib" / "guard.sh", "#!/bin/sh\n")
    projects._warn_on_gitignored_delivered_paths("proj", checkout, {".claude/hooks/lib/guard.sh": "irrelevant"})
    err = capsys.readouterr().err
    assert ".claude/hooks/lib/guard.sh" in err
    assert "gitignore" in err.lower()


def test_a_delivered_path_that_is_not_gitignored_is_silent(tmp_path, capsys):
    checkout = ignored_checkout(tmp_path)
    write(checkout / ".claude" / "hooks" / "guard.sh", "#!/bin/sh\n")
    projects._warn_on_gitignored_delivered_paths("proj", checkout, {".claude/hooks/guard.sh": "irrelevant"})
    assert capsys.readouterr().err == ""


def test_a_delivered_path_outside_the_checked_trees_is_not_checked(tmp_path, capsys):
    """.codex/hooks/ is neither a .claude/ project directory nor a skill target."""
    checkout = ignored_checkout(tmp_path, "lib/\n")
    write(checkout / ".codex" / "hooks" / "lib" / "guard.sh", "#!/bin/sh\n")
    projects._warn_on_gitignored_delivered_paths("proj", checkout, {".codex/hooks/lib/guard.sh": "irrelevant"})
    assert capsys.readouterr().err == ""


# rendered attribution check

WORKFLOW = Path(".github") / "workflows" / "attribution-check.yml"
COMMENT_WORKFLOW = Path(".github") / "workflows" / "attribution-check-comment.yml"
CHECK = Path(".github") / "attribution-check" / "attribution-check.py"
PACKAGED_CI = projects._packaged("ci")
OWNER = "fixture-owner"


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("STRATARC_GITHUB_OWNER", OWNER)
    return OWNER


def origin_checkout(world, origin: str | None) -> Path:
    world.source()
    checkout = world.projects / "active" / "proj"
    checkout.mkdir(parents=True)
    git("init", "-q", str(checkout))
    if origin is not None:
        git("-C", str(checkout), "remote", "add", "origin", origin)
    return checkout


@pytest.mark.parametrize(
    "origin",
    [
        f"git@github.com:{OWNER}/proj.git",
        f"https://github.com/{OWNER}/proj",
        f"https://github.com/{OWNER.title()}/proj.git",
        f"ssh://git@github.com/{OWNER}/proj.git",
    ],
)
def test_owned_github_origins_receive_the_workflow_and_the_check(world, owner, origin):
    checkout = origin_checkout(world, origin)
    acts = world.sync()
    assert any(str(WORKFLOW) in a for a in acts), acts
    for rendered in (WORKFLOW, COMMENT_WORKFLOW):
        workflow = (checkout / rendered).read_text()
        assert str(CHECK) in workflow, rendered
        assert "attribution-check.py" in workflow
        assert f"{projects.CI_DATA}/attribution-check.py" not in workflow
    assert (checkout / CHECK).read_bytes() == (PACKAGED_CI / "attribution-check.py").read_bytes()


def test_every_script_a_rendered_workflow_runs_is_delivered_by_the_same_sync(world, owner):
    """A workflow that runs a path the sync never wrote fails on every project it is rendered into."""
    import re

    checkout = origin_checkout(world, f"git@github.com:{OWNER}/proj.git")
    world.sync()
    for rendered in (WORKFLOW, COMMENT_WORKFLOW):
        text = (checkout / rendered).read_text()
        scripts = re.findall(r"python3\s+(\S+)", text)
        assert scripts, rendered
        for script in scripts:
            assert (checkout / script).is_file(), f"{rendered} runs {script}, which the sync did not deliver"


def test_this_repositorys_own_workflows_run_files_it_holds():
    import re

    repo = Path(__file__).resolve().parent.parent
    for workflow in (repo / ".github" / "workflows").glob("*.yml"):
        for script in re.findall(r"python3\s+(\S+\.py)", workflow.read_text()):
            assert (repo / script).is_file(), f"{workflow.name} runs {script}, which the repository does not hold"


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:someone-else/proj.git",
        f"https://gitlab.com/{OWNER}/proj.git",
        f"https://github.com/{OWNER}-fork/proj.git",
        None,
    ],
)
def test_other_origins_receive_nothing(world, owner, origin):
    checkout = origin_checkout(world, origin)
    acts = world.sync()
    assert not (checkout / ".github").exists()
    assert not any(".github" in a for a in acts), acts


def test_no_configured_owner_means_no_origin_is_owned(world, monkeypatch):
    monkeypatch.delenv("STRATARC_GITHUB_OWNER", raising=False)
    checkout = origin_checkout(world, f"git@github.com:{OWNER}/proj.git")
    world.sync()
    assert not (checkout / ".github").exists()


def test_the_rendered_check_reads_the_packaged_ci_data(world, owner):
    """A source root with its own scripts/ci/ changes nothing: the files come from the package."""
    write(world.root / "scripts" / "ci" / "attribution-check.py", "raise SystemExit(99)\n")
    checkout = origin_checkout(world, f"git@github.com:{OWNER}/proj.git")
    world.sync()
    assert (checkout / CHECK).read_bytes() == (PACKAGED_CI / "attribution-check.py").read_bytes()


def test_a_second_sync_is_current_and_check_mode_writes_nothing(world, owner):
    checkout = origin_checkout(world, f"git@github.com:{OWNER}/proj.git")
    acts = world.sync(dry=True)
    assert any(str(CHECK) in a for a in acts), acts
    assert not (checkout / ".github").exists()
    world.sync()
    assert world.sync() == []


def test_verify_reports_a_rendered_check_that_differs_from_source(world, owner):
    checkout = origin_checkout(world, f"git@github.com:{OWNER}/proj.git")
    cp = FakeControlPlane({"proj": ("active", "normal")})
    drift = projects.carried_guard_drift(cp)
    for target in (WORKFLOW, CHECK, COMMENT_WORKFLOW):
        assert f"proj: {target} is missing" in drift
    world.sync()
    assert projects.carried_guard_drift(cp) == []
    (checkout / WORKFLOW).write_text("name: edited\n")
    assert projects.carried_guard_drift(cp) == [f"proj: {WORKFLOW} differs from source"]


def test_verify_ignores_the_check_where_origin_is_not_owned(world, owner):
    origin_checkout(world, "git@github.com:someone-else/proj.git")
    world.sync()
    assert projects.carried_guard_drift(FakeControlPlane({"proj": ("active", "normal")})) == []


def test_the_rendered_check_runs_in_the_project_with_the_carried_detector(world, owner, tmp_path):
    checkout = origin_checkout(world, f"git@github.com:{OWNER}/proj.git")
    world.sync()
    event = tmp_path / "event.json"
    for message, code in (("Clean subject", 0), ("Subject\n\n" + _FOOTER, 1)):
        event.write_text(json.dumps({"commits": [{"id": "9" * 40, "distinct": True, "message": message}]}))
        result = subprocess.run(
            ["python3", str(CHECK), "--event-name", "push", "--event-path", str(event)],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == code, result.stdout + result.stderr


# github owner


def test_the_default_owner_is_empty(world, monkeypatch):
    monkeypatch.delenv("STRATARC_GITHUB_OWNER", raising=False)
    assert projects.github_owner() == ""
    assert not hasattr(projects, "DEFAULT_GITHUB_OWNER")


def test_remotes_json_with_one_owner_wins(world, monkeypatch):
    monkeypatch.setenv("STRATARC_GITHUB_OWNER", "other")
    write(world.src / "remotes.json", json.dumps({"a": "someone/a", "b": "someone/b"}))
    assert projects.github_owner() == "someone"


@pytest.mark.parametrize(
    "remotes",
    [None, "{not json", "[]", "{}", json.dumps({"a": "one/a", "b": "two/b"}), json.dumps({"a": "no-slash"})],
)
def test_the_configuration_is_used_when_remotes_json_carries_no_single_owner(world, monkeypatch, remotes):
    if remotes is not None:
        write(world.src / "remotes.json", remotes)
    monkeypatch.delenv("STRATARC_GITHUB_OWNER", raising=False)
    assert projects.github_owner() == ""
    monkeypatch.setenv("STRATARC_GITHUB_OWNER", "other")
    assert projects.github_owner() == "other"
    monkeypatch.setenv("STRATARC_GITHUB_OWNER", "  ")
    assert projects.github_owner() == ""


def test_the_owner_comes_from_stratarc_toml_when_the_variable_is_unset(world, monkeypatch):
    monkeypatch.delenv("STRATARC_GITHUB_OWNER", raising=False)
    write(world.root / "stratarc.toml", 'owner = "from-toml"\n')
    assert projects.github_owner() == "from-toml"
    monkeypatch.setenv("STRATARC_GITHUB_OWNER", "from-env")
    assert projects.github_owner() == "from-env"


def test_the_rendered_check_follows_the_configured_owner(world, monkeypatch):
    monkeypatch.delenv("STRATARC_GITHUB_OWNER", raising=False)
    checkout = origin_checkout(world, "git@github.com:someone/proj.git")
    write(world.src / "remotes.json", json.dumps({"proj": "someone/proj"}))
    acts = world.sync()
    assert any(str(WORKFLOW) in a for a in acts), acts
    assert (checkout / CHECK).is_file()
    (world.src / "remotes.json").unlink()
    shutil.rmtree(checkout / ".github")
    monkeypatch.setenv("STRATARC_GITHUB_OWNER", "someone")
    world.sync()
    assert (checkout / CHECK).is_file()
    shutil.rmtree(checkout / ".github")
    monkeypatch.delenv("STRATARC_GITHUB_OWNER")
    acts = world.sync()
    assert not (checkout / ".github").exists(), acts


# public targets


@pytest.fixture
def public(world):
    """A delivered project, proj, beside a public target, stratarc, that has a checkout and a source directory."""
    world.source()
    checkout = world.checkout()
    world.source("stratarc", agents="# stratarc\n")
    public_checkout = world.projects / "active" / "stratarc"
    git("init", "-q", str(public_checkout))
    write(world.src / "public-targets.json", '{"targets": ["stratarc"]}\n')
    return checkout, public_checkout


def only_git(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


def test_a_public_target_receives_no_file_and_its_neighbour_is_unchanged(world, public):
    checkout, public_checkout = public
    cp = FakeControlPlane({"proj": ("active", "normal"), "stratarc": ("active", "normal")})
    for control_plane in (None, cp):
        acts = world.sync("stratarc", cp=control_plane)
        assert len(acts) == 1, acts
        assert acts[0].startswith("skip: stratarc is a public repository"), acts
        assert only_git(public_checkout) == [".git"]
    acts = world.sync("proj")
    assert any("AGENTS.md" in a for a in acts), acts
    assert (checkout / "AGENTS.md").is_file()
    assert (checkout / ".claude" / "hooks" / "no-attribution-guard.sh").is_file()


def test_adopt_and_check_refuse_a_public_target_the_same_way(world, public):
    _checkout, public_checkout = public
    dry = world.sync("stratarc", dry=True)
    adopted = projects.adopt("stratarc", None)
    assert dry[0].startswith("skip: stratarc is a public repository"), dry
    assert adopted[0].startswith("adopt: skip: stratarc is a public repository"), adopted
    assert only_git(public_checkout) == [".git"]


def test_verify_does_not_report_the_public_checkout_as_unregistered_in_a_mixed_tree(world, public):
    assert projects.verify(FakeControlPlane({"proj": ("active", "normal")})) == []


def test_a_missing_list_declares_nothing(world, public):
    _checkout, public_checkout = public
    (world.src / "public-targets.json").unlink()
    world.sync("stratarc")
    assert (public_checkout / "AGENTS.md").is_file()


def test_a_bare_list_is_read_the_same_as_the_targets_object(world, public):
    _checkout, public_checkout = public
    write(world.src / "public-targets.json", '["stratarc"]\n')
    assert projects.public_targets() == frozenset({"stratarc"})
    acts = world.sync("stratarc")
    assert acts[0].startswith("skip: stratarc is a public repository"), acts
    assert only_git(public_checkout) == [".git"]


@pytest.mark.parametrize("broken", ["{not json", '"stratarc"', '{"targets": "stratarc"}', "[1]"])
def test_an_unreadable_list_refuses_before_any_delivery(world, public, broken):
    _checkout, public_checkout = public
    write(world.src / "public-targets.json", broken)
    with pytest.raises(RuntimeError, match="public-targets.json"):
        world.sync("stratarc")
    with pytest.raises(RuntimeError, match="public-targets.json"):
        world.sync("proj")
    assert only_git(public_checkout) == [".git"]


def test_main_refuses_to_run_with_an_unreadable_list(world, public, capsys):
    write(world.src / "public-targets.json", "{not json")
    assert projects.main(["--skip-reconcile", "--check"]) == 2
    assert "nothing was delivered" in capsys.readouterr().err


# source root resolution


def test_the_flag_beats_the_environment_beats_discovery(world, tmp_path, monkeypatch):
    flagged = tmp_path / "flagged"
    ambient = tmp_path / "ambient"
    flagged.mkdir()
    ambient.mkdir()
    monkeypatch.setenv("STRATARC_SOURCE", str(ambient))
    assert projects.source_root(str(flagged)) == flagged
    assert projects.source_root(None) == ambient
    monkeypatch.setenv("STRATARC_SOURCE", "")
    assert projects.source_root(None, flagged) == flagged


def test_discovery_finds_the_nearest_stratarc_toml(world, tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    write(tree / paths.CONFIG_NAME, "")
    (tree / "deep" / "er").mkdir(parents=True)
    monkeypatch.delenv("STRATARC_SOURCE")
    monkeypatch.chdir(tree / "deep" / "er")
    assert projects.source_root() == tree.resolve()


def test_the_module_never_defaults_to_its_own_location(world, tmp_path, monkeypatch):
    assert not hasattr(projects, "SCRIPTS")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("STRATARC_SOURCE")
    monkeypatch.chdir(elsewhere)
    assert projects.source_root() == elsewhere.resolve()


def test_main_with_the_root_flag_loads_the_control_plane_from_that_tree(world, tmp_path, monkeypatch, capsys):
    """--root rebinds ROOT before anything reads it: the control plane loaded is the flagged tree's, not the ambient one's."""
    flagged = tmp_path / "flagged"
    (flagged / "projects-root").mkdir(parents=True)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("STRATARC_SOURCE", str(ambient))
    seen = []

    class Reached(Exception):
        pass

    def load(path):
        seen.append(Path(path))
        raise Reached

    monkeypatch.setattr(ControlPlane, "load", staticmethod(load))
    with pytest.raises(Reached):
        projects.main(["--root", str(flagged), "--skip-reconcile", "--check"])
    assert seen == [flagged / "control-plane.md"]
    assert projects.ROOT == flagged
    assert projects.PROJECTS_SRC == flagged / "projects-root"


def test_main_resolves_the_root_from_the_environment_on_a_first_run(world, tmp_path, monkeypatch):
    other = tmp_path / "other"
    (other / "projects-root").mkdir(parents=True)
    monkeypatch.setattr(projects, "_ROOT_CONFIGURED", False)
    monkeypatch.setenv("STRATARC_SOURCE", str(other))
    seen = []

    class Reached(Exception):
        pass

    def load(path):
        seen.append(Path(path))
        raise Reached

    monkeypatch.setattr(ControlPlane, "load", staticmethod(load))
    with pytest.raises(Reached):
        projects.main(["--skip-reconcile"])
    assert seen == [other.resolve() / "control-plane.md"]


# main


def test_main_runs_the_reconciler_in_process_with_the_root(world, monkeypatch):
    calls = []
    monkeypatch.setattr(projects, "reconcile_main", lambda argv: calls.append(list(argv)) or 0)
    write(world.root / "control-plane.md", "# control-plane\n")
    assert projects.main(["--check"]) == 0
    assert calls == [["--root", str(world.root), "--check"]]
    calls.clear()
    assert projects.main([]) == 0
    assert calls == [["--root", str(world.root)]]
    calls.clear()
    projects.main(["--verify"])
    assert calls == [["--root", str(world.root), "--check"]]


def test_main_stops_when_the_reconciler_fails(world, monkeypatch, capsys):
    monkeypatch.setattr(projects, "reconcile_main", lambda argv: 2)
    assert projects.main([]) == 2
    assert capsys.readouterr().out == ""


def test_main_skips_the_reconciler_when_asked(world, monkeypatch):
    def refuse(argv):
        raise AssertionError("the reconciler must not run")

    monkeypatch.setattr(projects, "reconcile_main", refuse)
    write(world.root / "control-plane.md", "# control-plane\n")
    assert projects.main(["--skip-reconcile", "--check"]) == 0


def test_main_syncs_and_then_reports_current(world, monkeypatch, capsys):
    monkeypatch.setattr(projects, "reconcile_main", lambda argv: 0)
    world.source()
    world.checkout()
    write(world.root / "control-plane.md", "# control-plane\n")
    assert projects.main(["--check"]) == 1
    assert "projects: proj would:" in capsys.readouterr().out
    assert projects.main([]) == 0
    assert "projects: proj did:" in capsys.readouterr().out
    assert projects.main(["--check"]) == 0
    assert "projects: proj: current" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [[], ["--check"]])
def test_main_refuses_a_project_with_malformed_settings_and_continues(world, monkeypatch, capsys, flags):
    monkeypatch.setattr(projects, "reconcile_main", lambda argv: 0)
    for name in ("alpha", "beta"):
        world.source(name)
        world.checkout(name)
    broken = write(world.projects / "active" / "alpha" / ".claude" / "settings.json", '{"hooks": ')
    write(world.root / "control-plane.md", "# control-plane\n")

    code = projects.main(flags)

    captured = capsys.readouterr()
    assert code == 2, captured
    assert "Traceback" not in captured.err
    assert "settings.json" in captured.err and "alpha" in captured.err
    assert "line 1" in captured.err or "Expecting" in captured.err
    assert "projects: beta " in captured.out
    assert broken.read_text() == '{"hooks": '
    if not flags:
        assert (world.projects / "active" / "beta" / "AGENTS.md").is_file()
        assert not (world.projects / "active" / "alpha" / "AGENTS.md").exists()


def test_main_with_the_real_reconciler_adds_the_project_to_the_control_plane(world, capsys):
    world.source()
    world.checkout()
    write(world.root / "control-plane.md", "# control-plane\n")
    code = projects.main(["--only", "proj"])
    assert code == 0, capsys.readouterr()
    checkout = world.projects / "active" / "proj"
    assert "| proj | active |" in (world.root / "control-plane.md").read_text()
    for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md", "GEMINI.md"):
        assert (checkout / name).is_file(), name
    assert (checkout / projects.delivered_manifest()).is_file()
    assert projects.main(["--only", "proj", "--check"]) == 0


# the module names no private tool


def test_the_module_text_names_no_private_tool():
    text = Path(projects.__file__).read_text(encoding="utf-8").lower()
    for word in ("kata", "roborev", "freellmapi", "nodeterm", "klappe", "llm-root", "llm_root"):
        if word == "llm_root":
            assert "llm_root_projects_dir" in text
            text = text.replace("llm_root_projects_dir", "")
        assert word not in text, word


# per-project renderers

EXTENSION_FIXTURE = Path(__file__).parent / "fixtures" / "projects" / "renderer-extension"


@pytest.fixture
def renderer_world(world):
    shutil.copytree(EXTENSION_FIXTURE, world.root, dirs_exist_ok=True)
    world.source()
    return world, world.checkout()


def test_no_extension_registers_nothing_and_every_other_delivery_runs(world):
    assert projects.load_renderer_extensions() == []
    assert projects.RENDERERS == {}
    world.source()
    checkout = world.checkout()
    write(world.src / "proj" / "notice.txt", "hello\n")
    world.sync()
    assert (checkout / "AGENTS.md").is_file()
    assert not (checkout / "NOTICE.txt").exists()
    assert manifest_of(checkout)[projects.RENDERERS_FIELD] == {}


def test_an_extension_under_the_source_root_registers_a_renderer(renderer_world):
    world, _checkout = renderer_world
    assert projects.load_renderer_extensions() == ["notice"]
    assert set(projects.RENDERERS) == {"notice"}


def test_a_renderer_delivers_records_its_state_and_settles(renderer_world):
    world, checkout = renderer_world
    projects.load_renderer_extensions()
    write(world.src / "proj" / "notice.txt", "hello\n")
    acts = world.sync(dry=True)
    assert "render notice into NOTICE.txt" in acts
    assert not (checkout / "NOTICE.txt").exists()
    world.sync()
    assert (checkout / "NOTICE.txt").read_text() == "hello\n"
    assert manifest_of(checkout)[projects.RENDERERS_FIELD] == {"notice": projects.hash_provider_entry({"text": "hello\n"})}
    assert world.sync() == []


def test_a_renderer_removes_what_it_delivered_when_the_project_opts_out(renderer_world):
    world, checkout = renderer_world
    projects.load_renderer_extensions()
    opt_in = write(world.src / "proj" / "notice.txt", "hello\n")
    world.sync()
    opt_in.unlink()
    world.sync()
    assert not (checkout / "NOTICE.txt").exists()
    assert manifest_of(checkout)[projects.RENDERERS_FIELD] == {}
    assert world.sync() == []


def test_a_renderer_keeps_a_block_the_project_edited_and_keeps_its_state(renderer_world):
    world, checkout = renderer_world
    projects.load_renderer_extensions()
    opt_in = write(world.src / "proj" / "notice.txt", "hello\n")
    world.sync()
    state = manifest_of(checkout)[projects.RENDERERS_FIELD]
    (checkout / "NOTICE.txt").write_text("edited by the project\n")
    opt_in.unlink()
    world.sync()
    assert (checkout / "NOTICE.txt").read_text() == "edited by the project\n"
    assert manifest_of(checkout)[projects.RENDERERS_FIELD] == state


def test_a_renderer_leaves_a_file_it_never_delivered_alone(renderer_world):
    world, checkout = renderer_world
    projects.load_renderer_extensions()
    write(checkout / "NOTICE.txt", "the project's own\n")
    write(world.src / "proj" / "notice.txt", "hello\n")
    world.sync()
    assert (checkout / "NOTICE.txt").read_text() == "the project's own\n"
    assert manifest_of(checkout)[projects.RENDERERS_FIELD] == {}


def test_main_loads_the_extension_from_the_source_root(renderer_world, monkeypatch):
    world, checkout = renderer_world
    monkeypatch.setattr(projects, "reconcile_main", lambda argv: 0)
    write(world.root / "control-plane.md", "# control-plane\n")
    write(world.src / "proj" / "notice.txt", "hello\n")
    assert projects.main([]) == 0
    assert (checkout / "NOTICE.txt").read_text() == "hello\n"


def test_an_extension_that_cannot_be_loaded_is_reported_and_skipped(world, capsys):
    write(world.root / projects.RENDERER_EXTENSION, "def register(register_renderer:\n    pass\n")
    assert projects.load_renderer_extensions() == []
    assert "renderer extension" in capsys.readouterr().err
    world.source()
    checkout = world.checkout()
    world.sync()
    assert (checkout / "AGENTS.md").is_file()


def test_an_extension_without_a_register_function_is_reported_and_skipped(world, capsys):
    write(world.root / projects.RENDERER_EXTENSION, "VALUE = 1\n")
    assert projects.load_renderer_extensions() == []
    assert "not loaded" in capsys.readouterr().err


def test_a_later_registration_under_the_same_key_replaces_the_earlier_one(world):
    projects.register_renderer("k", lambda *args: "first")
    projects.register_renderer("k", lambda *args: "second")
    assert projects.RENDERERS["k"]() == "second"


def test_the_manifest_without_renderer_state_reads_as_empty(tmp_path):
    path = write(tmp_path / "delivered.json", json.dumps({"paths": []}))
    assert projects._read_renderer_state(path) == {}
    path.write_text(json.dumps({"renderers": {"a": "x", "b": 3}}))
    assert projects._read_renderer_state(path) == {"a": "x"}
    assert projects._read_renderer_state(tmp_path / "missing.json") == {}

