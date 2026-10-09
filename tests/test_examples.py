"""The worked example under examples/notes-cli stays valid and publishable."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from tests.test_schemas import describe, validator

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE = EXAMPLES / "notes-cli"
SOURCE = EXAMPLE / "source"
EXPECTED = EXAMPLE / "expected"

# Which schema validates which JSON file, by path below the source root.
# rules/tiers.json has no schema; test_tiers_list_every_rule covers it.
JSON_SCHEMAS = {
    "hooks/hooks.json": "hooks",
    "permissions.json": "permissions",
    "projects-root/notes-cli/permissions.json": "permissions",
    "components.json": "components",
}
NO_SCHEMA = {"rules/tiers.json"}

# Files the engine reads as configuration rather than inventories. The
# reconciler writes a control-plane row for an instruction file, a rule, a hook
# script, a skill, a command, an agent, a project item and a runtime, and for
# nothing else, so these carry no row.
NO_CONTROL_PLANE_ROW = {
    "control-plane.md",
    "permissions.json",
    "components.json",
    "hooks/hooks.json",
    "rules/tiers.json",
    "projects-root/notes-cli/permissions.json",
}

PROVENANCE_KEYS = re.compile(r"^(models|providers|session-link)[ \t]*:", re.MULTILINE)
# Assembled so that this file does not itself contain a session link.
SESSION_LINK = re.compile("claude" + r"\.ai/" + "code")


def source_files() -> list[Path]:
    return sorted(p for p in SOURCE.rglob("*") if p.is_file())


def relative(path: Path) -> str:
    return path.relative_to(SOURCE).as_posix()


def rule_files() -> list[Path]:
    return sorted(SOURCE.glob("rules/*.md")) + sorted(SOURCE.glob("projects-root/*/rules/*.md"))


def frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def frontmatter_keys(text: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in frontmatter(text).splitlines():
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match:
            keys[match.group(1)] = match.group(2).strip().strip('"')
    return keys


def test_json_files_have_a_schema_or_an_exemption() -> None:
    found = {relative(p) for p in source_files() if p.suffix == ".json"}
    assert found == set(JSON_SCHEMAS) | NO_SCHEMA


@pytest.mark.parametrize(("name", "schema"), sorted(JSON_SCHEMAS.items()))
def test_json_file_validates_against_its_schema(name: str, schema: str) -> None:
    document = json.loads((SOURCE / name).read_text(encoding="utf-8"))
    errors = list(validator(schema).iter_errors(document))
    assert not errors, describe(errors)


def test_tiers_list_every_rule() -> None:
    tiers = json.loads((SOURCE / "rules" / "tiers.json").read_text(encoding="utf-8"))
    assert set(tiers) == {"global", "common", "project"}
    listed = [name for names in tiers.values() for name in names]
    assert len(listed) == len(set(listed)), "a rule is listed under two tiers"
    assert sorted(listed) == sorted(p.name for p in SOURCE.glob("rules/*.md"))


@pytest.mark.parametrize("path", rule_files(), ids=relative)
def test_rule_shape(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"# {path.stem}"
    assert "## binding" in lines


@pytest.mark.parametrize("path", sorted(SOURCE.glob("**/SKILL.md")), ids=relative)
def test_skill_frontmatter(path: Path) -> None:
    keys = frontmatter_keys(path.read_text(encoding="utf-8"))
    assert keys.get("name") == path.parent.name
    assert keys.get("description")


def control_plane_links() -> set[str]:
    text = (SOURCE / "control-plane.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| \[[^\]]+\]\(([^)]+)\)", text, re.MULTILINE))


def test_control_plane_links_resolve() -> None:
    for link in control_plane_links():
        assert (SOURCE / link).is_file(), f"control-plane.md links to a missing file: {link}"


def test_control_plane_has_a_row_for_every_source_file() -> None:
    linked = control_plane_links()
    missing = [
        relative(p)
        for p in source_files()
        if relative(p) not in linked and relative(p) not in NO_CONTROL_PLANE_ROW
    ]
    assert not missing, f"no control-plane row for: {missing}"


def test_exempt_files_exist() -> None:
    assert NO_CONTROL_PLANE_ROW <= {relative(p) for p in source_files()}


def enabled_runtimes() -> list[str]:
    config = tomllib.loads((SOURCE / "stratarc.toml").read_text(encoding="utf-8"))
    return sorted(name for name, table in config["runtimes"].items() if table.get("enabled"))


def test_every_runtime_is_enabled() -> None:
    assert enabled_runtimes() == ["claude", "codex", "cursor", "gemini", "opencode"]


@pytest.mark.parametrize("runtime", enabled_runtimes())
def test_expected_has_a_directory_per_runtime(runtime: str) -> None:
    directory = EXPECTED / runtime
    assert directory.is_dir()
    assert any(p.is_file() for p in directory.rglob("*"))


def test_expected_has_the_managed_project() -> None:
    assert (EXPECTED / "project" / "notes-cli" / "AGENTS.md").is_file()


def example_files() -> list[Path]:
    return sorted(p for p in EXAMPLES.rglob("*") if p.is_file())


def test_examples_exist() -> None:
    assert example_files()


@pytest.mark.parametrize("path", [p for p in example_files() if p.suffix == ".md"], ids=lambda p: p.relative_to(EXAMPLES).as_posix())
def test_no_provenance_frontmatter(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not PROVENANCE_KEYS.search(frontmatter(text))


def test_no_session_link_anywhere_in_examples() -> None:
    hits = [
        p.relative_to(EXAMPLES).as_posix()
        for p in example_files()
        if SESSION_LINK.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not hits, f"session link in: {hits}"
