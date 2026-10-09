"""Tests for the source validator: each check gets its own fixture tree, shown failing before the defect is corrected and clean after.

Every tree here is a temporary fixture. The worked example under examples/ is the one tree read from the repository, and only to show that a published example passes the gate the sync runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stratarc import validate as vs


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def by_check(items: list[dict], check: str) -> list[dict]:
    return [item for item in items if item["check"] == check]


def installed_ruff() -> list[str] | None:
    """The ruff on PATH when it reports the pinned version; never asks uvx, which would need the network."""
    ruff = shutil.which("ruff")
    if ruff and vs._ruff_version([ruff]) == vs.RUFF_PINNED_VERSION:
        return [ruff]
    return None


@pytest.fixture(autouse=True)
def no_ruff_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off ruff and uvx unless it asks for the installed one."""
    monkeypatch.setattr(vs, "_ruff_command", lambda: None)


GOOD_DOC = (
    "---\n"
    "domain: example\n"
    "category: testing\n"
    "sub-category: fixtures\n"
    "topics: []\n"
    "types: []\n"
    "date-created: 2026-09-21\n"
    "date-revised: 2026-09-21\n"
    "status: DRAFT\n"
    "aliases: []\n"
    "tags: []\n"
    "models:\n"
    "  - model-a\n"
    "providers:\n"
    "  - Provider A\n"
    "session-link: https://example.test/session\n"
    "---\n"
    "\n"
    "# good\n"
    "\n"
    "## a-body-heading\n"
)


class TestAgentTools:
    def test_missing_line_and_lowercase_name_fail(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/missing-tools.md", "---\nname: missing-tools\ndescription: does a thing.\nmodel: sonnet\n---\n\n# missing-tools\n")
        write(tmp_path, "agents/bad-name.md", "---\nname: bad-name\ndescription: does a thing.\ntools: read, Grep\nmodel: sonnet\n---\n\n# bad-name\n")
        items = by_check(vs.check_agent_tools(tmp_path), "agent-tools-declaration")
        assert any("missing-tools.md" in item["path"] and "no tools" in item["message"] for item in items)
        assert any("bad-name.md" in item["path"] and "'read'" in item["message"] for item in items)

    def test_corrected_fixture_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/missing-tools.md", "---\nname: missing-tools\ndescription: does a thing.\ntools: Read, Glob\nmodel: sonnet\n---\n\n# missing-tools\n")
        write(tmp_path, "agents/bad-name.md", "---\nname: bad-name\ndescription: does a thing.\ntools: Read, Grep\nmodel: sonnet\n---\n\n# bad-name\n")
        assert vs.check_agent_tools(tmp_path) == []

    def test_mcp_tool_names_pass_and_a_malformed_one_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/graph.md", "---\nname: graph\ndescription: does a thing.\ntools: Read, mcp__code-index__search_graph\nmodel: sonnet\n---\n\n# graph\n")
        write(tmp_path, "agents/broken.md", "---\nname: broken\ndescription: does a thing.\ntools: Read, mcp__nothing\nmodel: sonnet\n---\n\n# broken\n")
        items = by_check(vs.check_agent_tools(tmp_path), "agent-tools-declaration")
        assert [item["path"].split(":")[0] for item in items] == ["agents/broken.md"]

    def test_a_file_without_frontmatter_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/bare.md", "# bare\n")
        items = by_check(vs.check_agent_tools(tmp_path), "agent-tools-declaration")
        assert [item["path"] for item in items] == ["agents/bare.md:1"]

    def test_no_agents_directory_is_no_finding(self, tmp_path: Path) -> None:
        assert vs.check_agent_tools(tmp_path) == []

    def test_the_directory_readme_is_not_an_agent_definition(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/README.md", "# agents\n\nWhat this directory holds.\n")
        assert vs.check_agent_tools(tmp_path) == []


class TestAgentToolUse:
    BODY = "\n# verifier\n\n1. Read the project files.\n2. Use WebFetch to reference the official docs.\n3. Keep a TodoWrite-style checklist.\n\n```text\nUse WebSearch here: inside a fence this is an example, not an instruction.\n```\n"

    def test_body_tool_missing_from_the_declaration_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/verifier.md", "---\nname: verifier\ndescription: checks a thing.\ntools: Read, Grep, Glob, Bash\n---\n" + self.BODY)
        write(tmp_path, "commands/fetcher.md", "---\ndescription: fetches a thing.\nallowed-tools: Read, Bash\n---\n\n# fetcher\n\nFetch the page with `WebFetch`.\n")
        findings = by_check(vs.check_agent_tool_use(tmp_path), "agent-tool-undeclared")
        assert [(item["path"], "WebFetch" in item["message"]) for item in findings] == [("agents/verifier.md:10", True), ("commands/fetcher.md:8", True)]

    def test_declared_tool_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/verifier.md", "---\nname: verifier\ndescription: checks a thing.\ntools: Read, Grep, Glob, Bash, WebFetch\n---\n" + self.BODY)
        assert vs.check_agent_tool_use(tmp_path) == []

    def test_a_command_without_allowed_tools_inherits_every_tool(self, tmp_path: Path) -> None:
        write(tmp_path, "commands/open.md", "---\ndescription: opens a thing.\n---\n\n# open\n\nFetch the page with `WebFetch`.\n")
        assert vs.check_agent_tool_use(tmp_path) == []


class TestAgentFrontmatter:
    def test_plain_description_with_colon_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/broken.md", "---\nname: broken\ndescription: Includes a label: value.\ntools: Read\nmodel: sonnet\n---\n\n# broken\n")
        items = by_check(vs.check_agent_frontmatter(tmp_path), "agent-frontmatter-yaml")
        assert [item["path"] for item in items] == ["agents/broken.md:3"]

    def test_block_description_with_colon_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "agents/fixed.md", "---\nname: fixed\ndescription: >-\n  Includes a label: value.\ntools: Read\nmodel: sonnet\n---\n\n# fixed\n")
        assert vs.check_agent_frontmatter(tmp_path) == []


class TestRulePathsLine:
    def test_leading_paths_line_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/leaky.md", 'paths:\n  - "**/*.sh"\n\n# leaky\n\nbody\n')
        items = by_check(vs.check_rule_paths_line(tmp_path), "rule-leading-paths-line")
        assert [item["path"] for item in items] == ["rules/leaky.md:1"]

    def test_corrected_fixture_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/leaky.md", "# leaky\n\nA paths: mention inside the body is fine; only the leading line is checked.\n")
        assert vs.check_rule_paths_line(tmp_path) == []

    def test_readme_is_not_checked(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/README.md", "paths:\nnot really, this file is exempt\n")
        assert vs.check_rule_paths_line(tmp_path) == []


class TestRuleBindingBudget:
    def test_a_binding_over_the_budget_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/long.md", "# long\n\n## binding\n\n" + "word " * 121 + "\n\n## detail\n\nmore\n")
        items = by_check(vs.check_rule_binding_budget(tmp_path), "rule-binding-budget")
        assert [item["path"] for item in items] == ["rules/long.md"]
        assert "121 words" in items[0]["message"]

    def test_a_binding_at_the_budget_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/fits.md", "# fits\n\n## binding\n\n" + "word " * 120 + "\n\n## detail\n\n" + "more " * 500 + "\n")
        assert vs.check_rule_binding_budget(tmp_path) == []

    def test_a_rule_with_no_binding_section_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/bare.md", "# bare\n\nbody\n")
        assert len(by_check(vs.check_rule_binding_budget(tmp_path), "rule-binding-budget")) == 1


class TestComponentsManifest:
    def test_an_invalid_manifest_is_an_error(self, tmp_path: Path) -> None:
        write(tmp_path, "components.json", '{"version": 2, "budgets": {"turns": -1}}')
        items = by_check(vs.check_components_manifest(tmp_path), "components-manifest")
        assert len(items) == 2
        assert all(item["severity"] == "error" for item in items)

    def test_an_unreadable_manifest_is_an_error(self, tmp_path: Path) -> None:
        write(tmp_path, "components.json", "{not json")
        assert len(by_check(vs.check_components_manifest(tmp_path), "components-manifest")) == 1

    def test_no_manifest_is_no_finding(self, tmp_path: Path) -> None:
        assert vs.check_components_manifest(tmp_path) == []

    def test_a_valid_manifest_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "components.json", '{"version": 1}')
        assert vs.check_components_manifest(tmp_path) == []


class TestDocFrontmatter:
    def test_good_fixture_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/good.md", GOOD_DOC)
        assert vs.check_doc_frontmatter(tmp_path) == []

    def test_a_dot_docs_directory_is_held_to_the_same_rules(self, tmp_path: Path) -> None:
        """A .docs/ page carries the same frontmatter, heading and provenance shape as a docs/ page, and a defect there is reported at its own path."""
        write(tmp_path, ".docs/good.md", GOOD_DOC)
        assert vs.check_doc_frontmatter(tmp_path) == []
        write(tmp_path, ".docs/bare.md", "# bare\n\nno frontmatter at all.\n")
        write(tmp_path, ".docs/prov.md", GOOD_DOC.replace("# good\n", "# prov\n").replace("models:\n  - model-a\n", "models: [model-a]\n"))
        write(tmp_path, ".docs/graph/index.json", "{}")
        findings = vs.check_doc_frontmatter(tmp_path)
        assert [item["check"] for item in findings if item["path"].startswith(".docs/bare.md")] == ["doc-frontmatter-missing"]
        assert [item["path"] for item in by_check(findings, "doc-provenance-shape")] == [".docs/prov.md:12"]
        assert not any("graph" in item["path"] for item in findings)

    def test_docs_and_dot_docs_are_both_walked_in_one_run(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/bare.md", "# bare\n")
        write(tmp_path, ".docs/bare.md", "# bare\n")
        findings = vs.check_doc_frontmatter(tmp_path)
        assert sorted(item["path"] for item in findings) == [".docs/bare.md:1", "docs/bare.md:1"]

    def test_missing_frontmatter_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/bare.md", "# bare\n\nno frontmatter at all.\n")
        assert [item["check"] for item in vs.check_doc_frontmatter(tmp_path)] == ["doc-frontmatter-missing"]

    def test_frontmatter_that_never_closes_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/open.md", "---\ndomain: example\n\n# open\n")
        assert "doc-frontmatter-missing" in [item["check"] for item in vs.check_doc_frontmatter(tmp_path)]

    def test_missing_canonical_key_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/short.md", GOOD_DOC.replace("domain: example\n", ""))
        items = by_check(vs.check_doc_frontmatter(tmp_path), "doc-frontmatter-keys")
        assert any("'domain'" in item["message"] for item in items)

    def test_out_of_order_keys_fail(self, tmp_path: Path) -> None:
        text = (
            "---\n"
            "domain: example\n"
            "category: testing\n"
            "status: DRAFT\n"
            "sub-category: fixtures\n"
            "topics: []\n"
            "types: []\n"
            "date-created: 2026-09-21\n"
            "date-revised: 2026-09-21\n"
            "aliases: []\n"
            "tags: []\n"
            "---\n\n# shuffled\n"
        )
        write(tmp_path, "docs/shuffled.md", text)
        assert by_check(vs.check_doc_frontmatter(tmp_path), "doc-frontmatter-order")

    def test_further_key_before_tags_fails(self, tmp_path: Path) -> None:
        text = GOOD_DOC.replace("aliases: []\ntags: []\n", "models:\n  - model-a\naliases: []\ntags: []\n").replace(
            "models:\n  - model-a\nproviders:\n  - Provider A\nsession-link: https://example.test/session\n", "providers:\n  - Provider A\nsession-link: https://example.test/session\n"
        )
        write(tmp_path, "docs/early-models.md", text)
        items = by_check(vs.check_doc_frontmatter(tmp_path), "doc-frontmatter-order")
        assert any("further key" in item["message"] for item in items)

    def test_further_keys_out_of_alphabetical_order_fail(self, tmp_path: Path) -> None:
        text = GOOD_DOC.replace("models:\n  - model-a\nproviders:\n  - Provider A\n", "providers:\n  - Provider A\nmodels:\n  - model-a\n")
        write(tmp_path, "docs/unsorted.md", text)
        items = by_check(vs.check_doc_frontmatter(tmp_path), "doc-frontmatter-order")
        assert any("sorted A to Z" in item["message"] for item in items)

    def test_invalid_status_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/final.md", GOOD_DOC.replace("status: DRAFT\n", "status: FINAL\n"))
        items = by_check(vs.check_doc_frontmatter(tmp_path), "doc-frontmatter-status")
        assert any("'FINAL'" in item["message"] for item in items)

    def test_heading_not_kebab_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/loud.md", GOOD_DOC.replace("## a-body-heading\n", "## A Body Heading\n"))
        items = by_check(vs.check_doc_frontmatter(tmp_path), "doc-heading-case")
        assert any("A Body Heading" in item["message"] for item in items)

    def test_a_heading_inside_a_fence_is_not_checked(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/good.md", GOOD_DOC + "\n```text\n## Not A Heading\n```\n")
        assert vs.check_doc_frontmatter(tmp_path) == []

    def test_h1_mismatch_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/good.md", GOOD_DOC.replace("# good\n", "# not-the-filename\n"))
        assert by_check(vs.check_doc_frontmatter(tmp_path), "doc-h1-mismatch")

    def test_readme_h1_must_be_literal(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/README.md", GOOD_DOC.replace("# good\n", "# readme\n"))
        items = by_check(vs.check_doc_frontmatter(tmp_path), "doc-heading-case")
        assert any("README.MD" in item["message"] for item in items)

    def test_readme_literal_h1_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/README.md", GOOD_DOC.replace("# good\n", "# README.MD\n"))
        assert vs.check_doc_frontmatter(tmp_path) == []

    def test_provenance_flow_style_and_unsorted_fail(self, tmp_path: Path) -> None:
        text = GOOD_DOC.replace("models:\n  - model-a\n", "models: [model-a]\n").replace(
            "providers:\n  - Provider A\n", "providers:\n  - Provider B\n  - Provider A\n"
        )
        write(tmp_path, "docs/prov.md", text)
        items = by_check(vs.check_doc_frontmatter(tmp_path), "doc-provenance-shape")
        assert any("flow style" in item["message"] for item in items)
        assert any("sorted case-insensitively" in item["message"] for item in items)

    def test_session_link_empty_string_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/good.md", GOOD_DOC.replace("session-link: https://example.test/session\n", 'session-link: ""\n'))
        assert vs.check_doc_frontmatter(tmp_path) == []

    def test_session_link_without_a_value_fails(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/good.md", GOOD_DOC.replace("session-link: https://example.test/session\n", "session-link:\n"))
        assert any("session-link has no value" in item["message"] for item in vs.check_doc_frontmatter(tmp_path))

    def test_prompt_with_empty_provenance_passes(self, tmp_path: Path) -> None:
        text = (
            GOOD_DOC.replace("models:\n  - model-a\n", "models: []\n")
            .replace("providers:\n  - Provider A\n", "providers: []\n")
            .replace("session-link: https://example.test/session\n", 'session-link: ""\n')
            .replace("# good\n", "# prompt-note\n")
        )
        write(tmp_path, "docs/prompts/prompt-note.md", text)
        assert vs.check_doc_frontmatter(tmp_path) == []

    def test_prompt_with_noncanonical_key_order_fails(self, tmp_path: Path) -> None:
        text = (
            GOOD_DOC.replace("models:\n  - model-a\nproviders:\n  - Provider A\n", "providers: []\nmodels: []\n")
            .replace("session-link: https://example.test/session\n", 'session-link: ""\n')
            .replace("# good\n", "# prompt-note\n")
        )
        write(tmp_path, "docs/prompts/prompt-note.md", text)
        assert by_check(vs.check_doc_frontmatter(tmp_path), "doc-frontmatter-order")

    def test_generated_marker_is_skipped(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/scripts/tool.md", "# tool\n\n<!-- script-doc:begin -->\n\ngenerated body, no frontmatter\n")
        assert vs.check_doc_frontmatter(tmp_path) == []


class TestDocIgnoredAndReserved:
    def test_a_git_ignored_doc_is_skipped_and_a_tracked_sibling_still_reports(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        write(tmp_path, ".gitignore", ".docs/COMMENTS-PROCESS.md\n")
        write(tmp_path, ".docs/COMMENTS-PROCESS.md", "# COMMENTS-PROCESS.MD\n\n## Loud Heading\n")
        write(tmp_path, ".docs/sibling.md", "# sibling\n\nno frontmatter.\n")
        assert [item["path"] for item in vs.check_doc_frontmatter(tmp_path)] == [".docs/sibling.md:1"]

    def test_a_non_git_root_validates_every_file(self, tmp_path: Path) -> None:
        write(tmp_path, ".gitignore", ".docs/COMMENTS-PROCESS.md\n")
        write(tmp_path, ".docs/COMMENTS-PROCESS.md", "# bare\n")
        write(tmp_path, ".docs/sibling.md", "# sibling\n")
        findings = vs.check_doc_frontmatter(tmp_path)
        assert sorted({item["path"] for item in findings}) == [".docs/COMMENTS-PROCESS.md:1", ".docs/sibling.md:1"]

    def test_reserved_names_keep_their_literal_uppercase_h1(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/PLAN.md", GOOD_DOC.replace("# good\n", "# PLAN.md\n"))
        write(tmp_path, "docs/COMMENTS-PROCESS.md", GOOD_DOC.replace("# good\n", "# COMMENTS-PROCESS.MD\n"))
        write(tmp_path, "docs/NOTES.md", GOOD_DOC.replace("# good\n", "# NOTES.md\n"))
        findings = vs.check_doc_frontmatter(tmp_path)
        assert [item["path"].split(":")[0] for item in findings] == ["docs/NOTES.md"] * 2
        assert {item["check"] for item in findings} == {"doc-heading-case", "doc-h1-mismatch"}


class TestRuleTiers:
    def test_missing_manifest_is_skipped_with_an_info_line(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/alpha.md", "# alpha\n")
        findings = vs.check_rule_tiers(tmp_path)
        assert len(findings) == 1
        assert findings[0]["severity"] == "info"
        assert "does not exist" in findings[0]["message"]

    def test_unknown_name_untiered_rule_and_duplicate_fail(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/alpha.md", "# alpha\n")
        write(tmp_path, "rules/beta.md", "# beta\n")
        write(tmp_path, "rules/retired.json", json.dumps({"retired": ["gamma.md"]}))
        write(tmp_path, "rules/tiers.json", json.dumps({"global": ["alpha.md", "ghost.md", "gamma.md"], "common": ["alpha.md"]}))
        items = by_check(vs.check_rule_tiers(tmp_path), "rule-tier-membership")
        assert any("ghost.md" in item["message"] and "not a file" in item["message"] for item in items)
        assert any("gamma.md" in item["message"] and "retired" in item["message"] for item in items)
        assert any("alpha.md" in item["message"] and "more than one tier" in item["message"] for item in items)
        assert any(item["path"] == "rules/beta.md" for item in items)

    def test_corrected_fixture_passes(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/alpha.md", "# alpha\n")
        write(tmp_path, "rules/beta.md", "# beta\n")
        write(tmp_path, "rules/tiers.json", json.dumps({"global": ["alpha.md"], "common": ["beta.md"]}))
        assert vs.check_rule_tiers(tmp_path) == []

    def test_non_string_tier_member_produces_a_finding(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/alpha.md", "# alpha\n")
        write(tmp_path, "rules/tiers.json", json.dumps({"global": ["alpha.md", {"bad": "member"}]}))
        items = by_check(vs.check_rule_tiers(tmp_path), "rule-tier-membership")
        assert any("non-string member" in item["message"] for item in items)

    def test_invalid_json_is_one_error(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/tiers.json", "{nope")
        items = by_check(vs.check_rule_tiers(tmp_path), "rule-tier-membership")
        assert [item["severity"] for item in items] == ["error"]


class TestReferenceGraphDelegation:
    def test_errors_pass_through_and_warnings_are_dropped(self, tmp_path: Path) -> None:
        pytest.importorskip("stratarc.source_graph")
        write(tmp_path, "hooks/hooks.json", json.dumps({"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "hooks/vanished.sh"}]}]}))
        write(tmp_path, "docs/notes.md", "# notes\n\nSee [gone](scripts/gone.py) for the old approach.\n")
        findings = vs.check_reference_graph(tmp_path)
        errors = by_check(findings, "dangling-reference")
        assert any(item["path"] == "hooks/hooks.json" for item in errors)
        assert not any(item["path"] == "docs/notes.md" for item in findings), "a warning-level doc reference must not surface here"
        assert all(item["severity"] == "error" for item in findings)

    def test_clean_tree_has_no_findings(self, tmp_path: Path) -> None:
        pytest.importorskip("stratarc.source_graph")
        write(tmp_path, "scripts/tool.py", '"""Tool."""\n')
        assert vs.check_reference_graph(tmp_path) == []

    def test_a_graph_that_is_not_installed_is_an_info_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(name: str):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

        monkeypatch.setattr(vs.importlib, "import_module", missing)
        findings = vs.check_reference_graph(tmp_path)
        assert [(item["severity"], item["check"]) for item in findings] == [("info", "reference-graph")]


GOOD_SHA = "11d5960a326750d5838078e36cf38b85af677262"


class TestWorkflowActionPins:
    def test_an_unpinned_tag_is_an_error(self, tmp_path: Path) -> None:
        write(tmp_path, ".github/workflows/x.yml", "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
        findings = by_check(vs.check_workflow_action_pins(tmp_path), "workflow-action-pin")
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert "not pinned" in findings[0]["message"]

    def test_a_sha_with_no_version_comment_is_an_error(self, tmp_path: Path) -> None:
        write(tmp_path, ".github/workflows/x.yml", f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{GOOD_SHA}\n")
        findings = by_check(vs.check_workflow_action_pins(tmp_path), "workflow-action-pin")
        assert len(findings) == 1
        assert "no released-version comment" in findings[0]["message"]

    def test_a_sha_with_a_version_comment_is_clean(self, tmp_path: Path) -> None:
        write(tmp_path, ".github/workflows/x.yml", f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{GOOD_SHA} # v4.4.0\n")
        assert by_check(vs.check_workflow_action_pins(tmp_path), "workflow-action-pin") == []

    def test_a_local_action_needs_no_pin(self, tmp_path: Path) -> None:
        write(tmp_path, ".github/workflows/x.yml", "jobs:\n  a:\n    steps:\n      - uses: ./local-action\n")
        assert by_check(vs.check_workflow_action_pins(tmp_path), "workflow-action-pin") == []

    def test_scripts_ci_workflows_are_also_checked(self, tmp_path: Path) -> None:
        write(tmp_path, "scripts/ci/x.yml", "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
        assert len(by_check(vs.check_workflow_action_pins(tmp_path), "workflow-action-pin")) == 1

    def test_no_workflows_is_no_finding(self, tmp_path: Path) -> None:
        assert vs.check_workflow_action_pins(tmp_path) == []


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the files the project delivery copies, so these tests do not depend on that module's own list."""
    from stratarc import projects

    monkeypatch.setattr(projects, "CARRIED_GUARD_FILES", ("lib/example-detect.py", "lib/other-detect.py", "example-guard.sh"), raising=False)
    monkeypatch.setattr(projects, "CHECK_SCRIPT_SOURCE", "scripts/ci/example-check.py", raising=False)


class TestDeliveredPythonFormat:
    def test_no_delivered_files_present_is_no_finding(self, tmp_path: Path, delivered: None) -> None:
        assert vs.check_delivered_python_format(tmp_path) == []

    def test_no_delivered_files_present_never_even_asks_for_ruff(self, tmp_path: Path, delivered: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """The empty-paths guard must short-circuit before touching ruff at all. Calling `ruff format --check --isolated` with zero file arguments quietly succeeds against the current directory, which would hide the guard's removal; a spy that blows up if invoked catches it on any machine."""

        def must_not_be_called():
            raise AssertionError("_ruff_command was called although there are no delivered files to check")

        monkeypatch.setattr(vs, "_ruff_command", must_not_be_called)
        assert vs.check_delivered_python_format(tmp_path) == []

    def test_unavailable_ruff_is_an_info_level_skip_not_an_error(self, tmp_path: Path, delivered: None) -> None:
        write(tmp_path, "hooks/lib/example-detect.py", "x = 1\n")
        findings = by_check(vs.check_delivered_python_format(tmp_path), "delivered-python-format")
        assert len(findings) == 1
        assert findings[0]["severity"] == "info"
        assert "skipping" in findings[0]["message"]

    def test_only_python_files_the_root_carries_are_checked(self, tmp_path: Path, delivered: None) -> None:
        write(tmp_path, "hooks/example-guard.sh", "#!/bin/sh\n")
        assert vs.check_delivered_python_format(tmp_path) == []

    def test_an_unformatted_delivered_file_is_an_error(self, tmp_path: Path, delivered: None, monkeypatch: pytest.MonkeyPatch) -> None:
        ruff = installed_ruff()
        if ruff is None:
            pytest.skip("no ruff at the pinned version is installed")
        monkeypatch.setattr(vs, "_ruff_command", lambda: ruff)
        write(tmp_path, "hooks/lib/example-detect.py", "x=1\n")
        findings = by_check(vs.check_delivered_python_format(tmp_path), "delivered-python-format")
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert findings[0]["path"] == "hooks/lib/example-detect.py"

    def test_a_ruff_formatted_delivered_file_is_clean(self, tmp_path: Path, delivered: None, monkeypatch: pytest.MonkeyPatch) -> None:
        ruff = installed_ruff()
        if ruff is None:
            pytest.skip("no ruff at the pinned version is installed")
        monkeypatch.setattr(vs, "_ruff_command", lambda: ruff)
        write(tmp_path, "hooks/lib/other-detect.py", "x = 1\n\n\ndef f():\n    return x\n")
        assert by_check(vs.check_delivered_python_format(tmp_path), "delivered-python-format") == []

    def test_the_check_script_source_is_also_covered(self, tmp_path: Path, delivered: None, monkeypatch: pytest.MonkeyPatch) -> None:
        ruff = installed_ruff()
        if ruff is None:
            pytest.skip("no ruff at the pinned version is installed")
        monkeypatch.setattr(vs, "_ruff_command", lambda: ruff)
        write(tmp_path, "scripts/ci/example-check.py", "x=1\n")
        findings = by_check(vs.check_delivered_python_format(tmp_path), "delivered-python-format")
        assert len(findings) == 1
        assert findings[0]["path"] == "scripts/ci/example-check.py"

    def test_a_broken_projects_module_is_an_info_finding(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(root: Path):
            raise ImportError("projects module is not importable")

        monkeypatch.setattr(vs, "_delivered_python_files", broken)
        findings = vs.check_delivered_python_format(tmp_path)
        assert [(item["severity"], item["check"]) for item in findings] == [("info", "delivered-python-format")]


class TestMainEntryPoint:
    def test_strict_exits_nonzero_on_error_and_zero_without(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        write(tmp_path, "agents/bad.md", "---\nname: bad\ndescription: does a thing.\nmodel: sonnet\n---\n\n# bad\n")
        assert vs.main(["--root", str(tmp_path), "--strict"]) == 1
        assert "agent-tools-declaration" in capsys.readouterr().out
        assert vs.main(["--root", str(tmp_path)]) == 0
        assert "validate:" in capsys.readouterr().out

    def test_clean_tree_exits_zero_even_strict(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/good.md", GOOD_DOC)
        assert vs.main(["--root", str(tmp_path), "--strict"]) == 0

    def test_the_root_defaults_to_the_resolved_source_root(self, source_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        write(source_root, "agents/bad.md", "---\nname: bad\ndescription: does a thing.\nmodel: sonnet\n---\n\n# bad\n")
        assert vs.main(["--strict"]) == 1
        assert "agents/bad.md" in capsys.readouterr().out

    def test_the_root_is_bound_as_the_source_root_while_it_runs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRATARC_SOURCE", "/previous/value")
        seen: list[str | None] = []
        monkeypatch.setattr(vs, "CHECKS", (lambda root: seen.append(os.environ.get("STRATARC_SOURCE")) or [],))
        vs.main(["--root", str(tmp_path)])
        assert seen == [str(tmp_path.resolve())]
        assert os.environ["STRATARC_SOURCE"] == "/previous/value"

    def test_an_unset_source_variable_is_unset_again_afterwards(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STRATARC_SOURCE", raising=False)
        with vs.bound_source(tmp_path):
            assert os.environ["STRATARC_SOURCE"] == str(tmp_path)
        assert "STRATARC_SOURCE" not in os.environ

    def test_the_worked_example_passes_the_generic_strict_gate(self, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert vs.main(["--root", str(repo_root / "examples" / "notes-cli" / "source"), "--strict", "--checks", "generic"]) == 0
        assert "0 errors" in capsys.readouterr().out


PRIVATE_MODULE = (Path(__file__).resolve().parent / "fixtures" / "validate" / "private-checks" / "validate_checks.py").read_text(encoding="utf-8")


class TestPrivateChecks:
    """A source root adds checks in scripts/private/validate_checks.py; --checks selects which class runs."""

    BAD_AGENT = "---\nname: bad\ndescription: does a thing.\nmodel: sonnet\n---\n\n# bad\n"

    def test_every_built_in_check_is_listed_once(self) -> None:
        names = [check.__name__ for check in vs.CHECKS]
        assert len(names) == len(set(names))
        module_checks = {
            name
            for name in dir(vs)
            if name.startswith("check_") and callable(getattr(vs, name)) and name not in ("check_frontmatter_keys", "check_provenance_shape", "check_headings", "check_one_doc")
        }
        assert module_checks == set(names)

    def test_the_built_in_set_carries_no_operator_input(self) -> None:
        for gone in ("PRIVATE_INPUTS", "has_private_inputs", "check_loadout_budgets", "check_project_remotes"):
            assert not hasattr(vs, gone)

    def test_no_module_is_no_checks_and_no_findings(self, tmp_path: Path) -> None:
        assert vs.private_checks(tmp_path) == ([], [])
        assert vs.run_private(tmp_path) == []

    def _root_with_module(self, tmp_path: Path, text: str = PRIVATE_MODULE) -> Path:
        write(tmp_path, vs.PRIVATE_CHECKS_PATH, text)
        write(tmp_path, "widget.txt", "")
        write(tmp_path, "agents/bad.md", self.BAD_AGENT)
        return tmp_path

    def test_the_modules_checks_run_under_private_and_all(self, tmp_path: Path) -> None:
        root = self._root_with_module(tmp_path)
        assert [item["check"] for item in vs.run_all(root, "private")] == ["widget"]
        assert {"widget", "agent-tools-declaration"} <= {item["check"] for item in vs.run_all(root, "all")}
        assert {"widget", "agent-tools-declaration"} <= {item["check"] for item in vs.run_all(root)}

    def test_generic_skips_the_private_defect(self, tmp_path: Path) -> None:
        root = self._root_with_module(tmp_path)
        checks = {item["check"] for item in vs.run_all(root, "generic")}
        assert "agent-tools-declaration" in checks
        assert "widget" not in checks

    def test_private_skips_the_generic_defect(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        root = self._root_with_module(tmp_path)
        assert vs.main(["--root", str(root), "--strict", "--checks", "private"]) == 1
        out = capsys.readouterr().out
        assert "widget" in out
        assert "agent-tools-declaration" not in out

    def test_a_module_that_cannot_be_imported_is_an_error_finding(self, tmp_path: Path) -> None:
        write(tmp_path, vs.PRIVATE_CHECKS_PATH, "raise RuntimeError('boom')\n")
        items = vs.run_all(tmp_path, "private")
        assert [(item["severity"], item["check"], item["path"]) for item in items] == [("error", "private-checks", vs.PRIVATE_CHECKS_PATH)]
        assert "boom" in items[0]["message"]

    def test_a_module_without_the_declaration_is_an_error_finding(self, tmp_path: Path) -> None:
        write(tmp_path, vs.PRIVATE_CHECKS_PATH, "CHECKS = []\n")
        items = vs.run_all(tmp_path, "private")
        assert len(items) == 1
        assert "VALIDATE_CHECKS" in items[0]["message"]

    def test_a_declaration_of_non_callables_is_an_error_finding(self, tmp_path: Path) -> None:
        write(tmp_path, vs.PRIVATE_CHECKS_PATH, "VALIDATE_CHECKS = ['not callable']\n")
        assert len(vs.run_all(tmp_path, "private")) == 1

    def test_a_check_that_raises_is_one_finding_and_the_rest_still_run(self, tmp_path: Path) -> None:
        module = (
            "from stratarc.validate import finding\n\n\n"
            "def check_breaks(root):\n    raise ValueError('bad input')\n\n\n"
            "def check_fine(root):\n    return [finding('warning', 'fine', 'x', 'still ran')]\n\n\n"
            "VALIDATE_CHECKS = [check_breaks, check_fine]\n"
        )
        write(tmp_path, vs.PRIVATE_CHECKS_PATH, module)
        items = vs.run_all(tmp_path, "private")
        assert {item["check"] for item in items} == {"private-checks", "fine"}
        assert any("check_breaks raised ValueError" in item["message"] for item in items)

    def test_an_unknown_class_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            vs.run_all(tmp_path, "some")

    def test_findings_sort_errors_first(self, tmp_path: Path) -> None:
        write(tmp_path, "rules/alpha.md", "# alpha\n")
        write(tmp_path, "agents/bad.md", self.BAD_AGENT)
        severities = [item["severity"] for item in vs.run_all(tmp_path, "generic")]
        assert severities == sorted(severities, key=vs.SEVERITY_ORDER.get)
        assert severities[0] == "error"

    def test_an_example_root_with_a_projects_root_passes_generic_strict(self, tmp_path: Path) -> None:
        write(tmp_path, "docs/good.md", GOOD_DOC)
        write(tmp_path, "rules/example.md", "# example\n\n## binding\n\nBody.\n")
        write(tmp_path, "rules/tiers.json", json.dumps({"common": ["example.md"]}))
        (tmp_path / "projects-root" / "example").mkdir(parents=True)
        assert vs.main(["--root", str(tmp_path), "--strict", "--checks", "generic"]) == 0
