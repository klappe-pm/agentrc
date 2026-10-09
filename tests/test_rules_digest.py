"""Tests for tier-manifest-driven rule selection and per-column rendering."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from stratarc.rules_digest import (
    CANONICAL_RULES_PREFIX,
    DIGEST_BEGIN,
    DIGEST_END,
    DIGEST_SOURCE_ENV_VAR,
    SOURCE_TOKEN,
    _extract_binding_section,
    build_rules_digest,
    digest_or_note,
    embed_digest,
    load_tiers,
    render_root,
    render_source,
    resolve_rules_root,
    rules_prefix,
)

FIXTURE_RULES = Path(__file__).resolve().parent / "fixtures" / "rules"


def write_rule(root: Path, name: str, body: str = "") -> None:
    stem = name[:-3] if name.endswith(".md") else name
    (root / name).write_text(f"# {stem}\n\n{body or 'Body.'}\n", encoding="utf-8")


def write_tiers(root: Path, tiers: dict[str, list[str]]) -> None:
    (root / "tiers.json").write_text(json.dumps(tiers), encoding="utf-8")


def headings(digest: str) -> list[str]:
    return [line[4:] for line in digest.splitlines() if line.startswith("### ")]


@pytest.fixture
def rules(tmp_path) -> Path:
    root = tmp_path / "rules"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def no_digest_override(monkeypatch):
    monkeypatch.delenv(DIGEST_SOURCE_ENV_VAR, raising=False)


class TestFixtureManifest:
    """The fixture rule tree is a coherent example: one tier per rule, every tiered name a file."""

    def test_every_rule_file_has_exactly_one_tier(self):
        tiers = json.loads((FIXTURE_RULES / "tiers.json").read_text(encoding="utf-8"))
        rule_files = {p.name for p in FIXTURE_RULES.glob("*.md") if p.name != "README.md"}
        seen: dict[str, str] = {}
        for tier_name, members in tiers.items():
            for name in members:
                assert name not in seen, f"{name!r} appears in more than one tier"
                seen[name] = tier_name
        assert set(seen) == rule_files

    def test_the_fixture_digest_carries_global_then_common(self):
        digest = build_rules_digest(FIXTURE_RULES)
        assert digest is not None
        assert headings(digest) == ["never-commit-local-databases", "conventional-commit-messages"]

    def test_every_fixture_rule_has_a_binding_so_no_rationale_reaches_a_surface(self):
        digest = build_rules_digest(FIXTURE_RULES)
        assert digest is not None
        assert "#### rationale" not in digest
        assert digest.count("Full rule: `") == len(re.findall(r"^### ", digest, re.MULTILINE))


class TestTierSelection:
    def test_default_render_carries_global_and_common_in_tier_then_alphabetical_order(self, rules):
        for name in ("z-global.md", "a-global.md", "b-common.md", "a-project.md"):
            write_rule(rules, name)
        write_tiers(
            rules,
            {
                "global": ["z-global.md", "a-global.md"],
                "common": ["b-common.md"],
                "project": ["a-project.md"],
            },
        )
        digest = build_rules_digest(rules)
        assert digest is not None
        assert headings(digest) == ["a-global", "z-global", "b-common"]
        assert "### a-project" not in digest
        assert "Tier: global." in digest
        assert "Tier: common." in digest

    def test_untiered_file_on_disk_is_excluded_without_crashing(self, rules):
        write_rule(rules, "example-rule.md", "## a-section\n\nBody.")
        write_rule(rules, "markdown-style.md")
        write_rule(rules, "not-in-manifest.md")
        write_tiers(rules, {"global": ["example-rule.md"], "common": ["markdown-style.md"], "project": []})
        digest = build_rules_digest(rules)
        assert digest is not None
        assert "### example-rule" in digest
        assert "### not-in-manifest" not in digest

    def test_a_tiered_name_with_no_file_is_skipped(self, rules):
        write_rule(rules, "present.md")
        write_tiers(rules, {"global": ["present.md", "absent.md"]})
        digest = build_rules_digest(rules)
        assert digest is not None
        assert headings(digest) == ["present"]

    def test_every_emitted_heading_is_kebab_case(self, rules):
        kebab = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        write_rule(rules, "example-rule.md", "## a-section\n\nBody.")
        write_rule(rules, "markdown-style.md")
        write_tiers(rules, {"global": ["example-rule.md"], "common": ["markdown-style.md"], "project": []})
        digest = build_rules_digest(rules)
        assert digest is not None
        found = [line.lstrip("#").strip() for line in digest.splitlines() if line.startswith("#")]
        assert found
        assert all(kebab.match(heading) for heading in found)

    def test_no_manifest_returns_none(self, rules):
        write_rule(rules, "example.md")
        assert build_rules_digest(rules) is None

    def test_a_missing_tree_returns_none_and_the_note_degrades(self, tmp_path):
        assert build_rules_digest(tmp_path / "absent") is None
        assert "unavailable" in digest_or_note(tmp_path / "absent")

    def test_load_tiers_ignores_non_list_values(self, rules):
        (rules / "tiers.json").write_text(
            json.dumps({"global": ["a.md"], "$comment": "not a tier"}), encoding="utf-8"
        )
        assert load_tiers(rules) == {"global": ["a.md"]}

    def test_load_tiers_tolerates_malformed_json(self, rules):
        (rules / "tiers.json").write_text("{not json", encoding="utf-8")
        assert load_tiers(rules) == {}


class TestPerColumnRendering:
    """Two project columns with different opt-ins each render exactly their opted-in rules, in tier order."""

    @pytest.fixture
    def tree(self, tmp_path) -> Path:
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        for name in ("global-one", "common-one", "common-two", "project-one", "project-two"):
            write_rule(rules_dir, f"{name}.md")
        write_tiers(
            rules_dir,
            {
                "global": ["global-one.md"],
                "common": ["common-one.md", "common-two.md"],
                "project": ["project-one.md", "project-two.md"],
            },
        )
        (tmp_path / "control-plane.md").write_text(
            "\n".join(
                [
                    "# control-plane",
                    "",
                    "## rules",
                    "",
                    "| option | tier | global | alpha | beta |",
                    "|---|---|---|---|---|",
                    "| [rule:global-one](rules/global-one.md) | global | x | x | x |",
                    "| [rule:common-one](rules/common-one.md) | common | x | x | |",
                    "| [rule:common-two](rules/common-two.md) | common | x | x | x |",
                    "| [rule:project-one](rules/project-one.md) | project | | x | |",
                    "| [rule:project-two](rules/project-two.md) | project | | | x |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return tmp_path

    def test_alpha_column_renders_exactly_its_opted_in_rules_in_tier_order(self, tree):
        digest = build_rules_digest(tree / "rules", column="alpha", control_plane_path=tree / "control-plane.md")
        assert digest is not None
        assert headings(digest) == ["global-one", "common-one", "common-two", "project-one"]

    def test_beta_column_renders_exactly_its_opted_in_rules_in_tier_order(self, tree):
        digest = build_rules_digest(tree / "rules", column="beta", control_plane_path=tree / "control-plane.md")
        assert digest is not None
        assert headings(digest) == ["global-one", "common-two", "project-two"]

    def test_global_tier_renders_for_a_column_even_if_its_cell_were_cleared(self, tree):
        """A global rule cannot be opted out; the column render ignores the cell for tier global."""
        cp_path = tree / "control-plane.md"
        cp_path.write_text(
            cp_path.read_text(encoding="utf-8").replace(
                "| [rule:global-one](rules/global-one.md) | global | x | x | x |",
                "| [rule:global-one](rules/global-one.md) | global | x | | x |",
            ),
            encoding="utf-8",
        )
        digest = build_rules_digest(tree / "rules", column="alpha", control_plane_path=cp_path)
        assert digest is not None
        assert "### global-one" in digest

    def test_the_control_plane_defaults_to_the_source_roots(self, tree, monkeypatch):
        monkeypatch.setenv("STRATARC_SOURCE", str(tree))
        digest = build_rules_digest(column="beta")
        assert digest is not None
        assert headings(digest) == ["global-one", "common-two", "project-two"]


class TestBindingPlusPointerRendering:
    """Every surface carries each rule as its binding section plus a pointer to the full text."""

    SPLIT = "## binding\n\nBINDING-SENTINEL binding text.\n\n## rationale\n\nRATIONALE-SENTINEL rationale text.\n"

    def test_default_renders_binding_plus_canonical_pointer(self, rules):
        write_rule(rules, "example-rule.md", self.SPLIT)
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules)
        assert digest is not None
        assert "BINDING-SENTINEL" in digest
        assert "RATIONALE-SENTINEL" not in digest
        assert f"Full rule: `{CANONICAL_RULES_PREFIX}example-rule.md`" in digest

    def test_pointer_prefix_names_another_location(self, rules):
        write_rule(rules, "example-rule.md", self.SPLIT)
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules, pointer_prefix="rules/")
        assert digest is not None
        assert "Full rule: `rules/example-rule.md`" in digest
        assert CANONICAL_RULES_PREFIX not in digest

    def test_without_binding_section_renders_full_body_and_no_pointer(self, rules):
        """A pointer alone would drop the requirement of a rule with no binding."""
        write_rule(rules, "example-rule.md", "No binding section, only body text.\n\n## detail\n\nMore.")
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules)
        assert digest is not None
        assert "No binding section, only body text." in digest
        assert "#### detail" in digest  # headings demoted two levels under the per-rule H3
        assert "Full rule:" not in digest

    def test_the_canonical_prefix_is_the_portable_token_form(self):
        assert CANONICAL_RULES_PREFIX == SOURCE_TOKEN + "/rules/"
        assert not CANONICAL_RULES_PREFIX.startswith("/")
        assert "$HOME" not in CANONICAL_RULES_PREFIX

    def test_binding_extraction_is_fence_aware(self, rules):
        write_rule(
            rules,
            "example-rule.md",
            "## binding\n\nBINDING-SENTINEL start.\n\n```\n## fake-heading in code\n```\n\n"
            "BINDING-SENTINEL end.\n\n## rationale\n\nRATIONALE-SENTINEL\n",
        )
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules)
        assert digest is not None
        assert "BINDING-SENTINEL start." in digest
        assert "BINDING-SENTINEL end." in digest
        assert "## fake-heading in code" in digest
        assert "RATIONALE-SENTINEL" not in digest

    def test_frontmatter_is_not_part_of_a_binding(self):
        text = "---\ndomain: x\n---\n\n# r\n\n## binding\n\nDo it.\n\n## why\n\nBecause.\n"
        assert _extract_binding_section(text) == "Do it."


class TestMarkers:
    def test_the_marker_names_the_stratarc_command(self):
        assert "stratarc gen-rules-digest" in DIGEST_BEGIN
        assert DIGEST_END == "<!-- END RULES-DIGEST -->"

    def test_the_digest_opens_and_closes_on_the_markers(self):
        digest = build_rules_digest(FIXTURE_RULES)
        assert digest is not None
        assert digest.startswith(DIGEST_BEGIN)
        assert digest.endswith(DIGEST_END)

    def test_embed_replaces_only_the_marked_region(self):
        document = f"before\n{DIGEST_BEGIN}\nold\n{DIGEST_END}\nafter\n"
        assert embed_digest(document, "NEW") == "before\nNEW\nafter\n"

    def test_embed_refuses_a_document_without_markers(self):
        with pytest.raises(ValueError, match="marker pair"):
            embed_digest("no markers", "NEW")


class TestSourceRootRendering:
    """Rule text spells the checkout as the token; a render replaces it with the root the digest is for."""

    TOKENED = (
        "## binding\n\nRun `python3 " + SOURCE_TOKEN + "/scripts/tool.py`.\n\n"
        "## rationale\n\nWHY " + SOURCE_TOKEN + "/docs.\n"
    )

    def test_render_root_collapses_the_home_directory(self, stratarc_home):
        assert render_root(stratarc_home / "projects" / "x") == "$HOME/projects/x"
        assert render_root(stratarc_home) == "$HOME"

    def test_render_root_follows_stratarc_home(self, stratarc_home, tmp_path):
        other = tmp_path / "elsewhere"
        assert render_root(other) == str(other)

    def test_render_root_keeps_a_path_outside_home_absolute(self, stratarc_home):
        assert render_root("/opt/stratarc") == "/opt/stratarc"
        assert rules_prefix("/opt/stratarc") == "/opt/stratarc/rules/"

    def test_render_source_replaces_every_token_and_nothing_else(self):
        text = f"a {SOURCE_TOKEN}/x and {SOURCE_TOKEN}/y, not $STRATARC_SOURCE"
        assert render_source(text, "/opt/stratarc") == "a /opt/stratarc/x and /opt/stratarc/y, not $STRATARC_SOURCE"
        text = "no token"
        assert render_source(text, "/opt/stratarc") is text

    def test_a_named_source_root_renders_binding_and_pointer(self, rules):
        write_rule(rules, "example-rule.md", self.TOKENED)
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules, source_root="/opt/stratarc")
        assert digest is not None
        assert SOURCE_TOKEN not in digest
        assert "python3 /opt/stratarc/scripts/tool.py" in digest
        assert "Full rule: `/opt/stratarc/rules/example-rule.md`" in digest
        assert "WHY" not in digest

    def test_a_full_body_render_replaces_the_token_too(self, rules):
        write_rule(rules, "example-rule.md", f"Body at {SOURCE_TOKEN}/here.")
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules, source_root="/opt/stratarc")
        assert digest is not None
        assert "Body at /opt/stratarc/here." in digest

    def test_an_explicit_prefix_still_wins_over_the_source_root(self, rules):
        write_rule(rules, "example-rule.md", self.TOKENED)
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules, pointer_prefix="rules/", source_root="/opt/stratarc")
        assert digest is not None
        assert "Full rule: `rules/example-rule.md`" in digest
        assert "python3 /opt/stratarc/scripts/tool.py" in digest

    def test_without_a_source_root_the_digest_keeps_the_token(self, rules, stratarc_home):
        """The committed render: the token stays so the digest is the same on every machine."""
        write_rule(rules, "example-rule.md", self.TOKENED)
        write_tiers(rules, {"global": ["example-rule.md"]})
        digest = build_rules_digest(rules)
        assert digest is not None
        assert f"python3 {SOURCE_TOKEN}/scripts/tool.py" in digest
        assert f"Full rule: `{SOURCE_TOKEN}/rules/example-rule.md`" in digest
        assert "$HOME" not in digest
        assert str(stratarc_home) not in digest


class TestDefaultRulesRoot:
    """With no argument and no override the digest reads the source root's rules/."""

    def test_default_root_is_the_source_roots_rule_tree(self, source_root):
        assert resolve_rules_root() == source_root / "rules"

    def test_an_explicit_argument_beats_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv(DIGEST_SOURCE_ENV_VAR, str(tmp_path / "env"))
        assert resolve_rules_root(tmp_path / "arg") == tmp_path / "arg"
        assert resolve_rules_root() == tmp_path / "env"

    def test_the_default_digest_renders_from_the_source_root(self, source_root):
        shutil.copytree(FIXTURE_RULES, source_root / "rules")
        digest = build_rules_digest()
        assert digest is not None
        assert "### never-commit-local-databases" in digest

    def test_an_empty_source_root_has_nothing_to_render(self, source_root):
        assert build_rules_digest() is None
