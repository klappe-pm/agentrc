"""Tests for stratarc.source_graph: edges are read from a synthetic tree, conflicts are flagged with evidence, impact stops at hubs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from stratarc import source_graph as sg

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write(root, "AGENTS.md", "# agents\n\n### alpha (global)\n\nbody\n\n### nowhere (common)\n\nbody\n")
    write(root, "README.md", "# README.MD\n\nSee [scripts](SCRIPTS.md).\n")
    write(root, "SCRIPTS.md", "# scripts\n\n- [tool](docs/scripts/tool.md): a tool.\n")
    write(root, "control-plane.md", "# control-plane\n\n## projects\n\n| project | status |\n|---|---|\n| proj-a | active |\n\n## rules\n\n| option | global | proj-a | proj-b |\n|---|---|---|---|\n| [rule:alpha](rules/alpha.md) | x | x |  |\n\n## hooks\n\n| option | global | proj-a | proj-b |\n|---|---|---|---|\n| [hook:present](hooks/present.sh) | x |  | x |\n")
    write(root, "rules/alpha.md", "# alpha\n\nSee `beta-rule` and the project's `docs/adr/` folder.\n")
    write(root, "rules/beta-rule.md", "# beta-rule\n\nThis rule absorbed `gone-rule`. The detector lives in `hooks/lib/missing.sh`.\n")
    write(root, "rules/README.md", "# rules\n\n| File | Purpose |\n|---|---|\n| `alpha.md` | first |\n| `gamma.md` | third |\n| `ghost.md` | gone |\n")
    write(root, "rules/retired.json", json.dumps({"retired": ["gone-rule.md"]}))
    write(root, "hooks/hooks.json", json.dumps({"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/present.sh"}, {"type": "command", "command": "bash $HOME/.claude/hooks/vanished.sh"}]}]}, indent=1))
    write(root, "hooks/present.sh", '#!/usr/bin/env bash\n# see hooks/old-guard.sh for history\nset -euo pipefail\nsource "$HOOK_DIR/lib/helper.sh"\nroot="$HOME/x"\npython3 "$root/scripts/tool.py"\n')
    write(root, "hooks/present.test.sh", "#!/usr/bin/env bash\n# exercises present.sh\n")
    write(root, "hooks/lonely.sh", "#!/usr/bin/env bash\nset -euo pipefail\n")
    write(root, "hooks/lib/helper.sh", "#!/usr/bin/env bash\nhelper() { :; }\n")
    write(root, "scripts/tool.py", '"""Tool."""\nimport helper_mod\n')
    write(root, "scripts/helper_mod.py", '"""Helper."""\n')
    write(root, "scripts/tool.test.py", "# builds scripts/fixture-only.py in a temp dir and runs tool.py\n")
    write(root, "scripts/rules_digest.py", 'COMMON_RULES = (\n    "alpha.md",\n    "gone-rule.md",\n    "nowhere.md",\n)\n')
    write(root, "skills/good/SKILL.md", "---\nname: good\ndescription: Use the `real` agent. Hand off to the missing-agent agent when stuck (use spec-scribe).\n---\n\n# good\n")
    write(root, "skills/good/extra.md", "# extra\n")
    (root / "skills" / "empty").mkdir(parents=True)
    (root / "skills" / "excluded").mkdir(parents=True)
    write(root, "skills/sync-exclude.json", json.dumps({"exclude": {"excluded": "third party"}}))
    write(root, "agents/real.md", "---\nname: real\n---\n\n# real\n")
    write(root, "commands/go.md", "# go\n")
    write(root, "projects-root/proj-a/surfaces.json", json.dumps({"copy": ["AGENTS.md", "CODEX.md"]}))
    write(root, "projects-root/proj-a/AGENTS.md", "# agents\n")
    write(root, "projects-root/proj-b/surfaces.json", json.dumps({"copy": ["AGENTS.md"]}))
    write(root, "docs/scripts/tool.md", "# tool\n\nSource: [scripts/tool.py](../../scripts/tool.py).\n")
    write(root, "docs/scripts/README.md", "# scripts\n\n- [tool](tool.md)\n")
    write(root, "docs/notes/old.md", "# old\n\nOnce there was `scripts/retired-tool.py`.\n")
    write(root, "docs/notes/fenced.md", "# fenced\n\nSee [old](old.md) for the real note.\n\n```text\nQuoted verbatim: [ghost](ghost.md).\n```\n")
    # A dot directory is never walked, tracked or not; classify() alone would model this as a doc.
    write(root, "docs/.hidden/x.md", "# hidden\n")
    # The three documentation roots cross-link, and .docs/ is the one dot directory the graph walks.
    write(root, "rules/gamma.md", "# gamma\n\nRecords live in `.docs/adr/decision.md`; the missing one is `.docs/adr/absent.md`.\n")
    write(root, ".docs/adr/decision.md", "# decision\n\nSee [the guide](../../developer-docs/guides/guide.md) and [old](../../docs/notes/old.md).\n")
    write(root, ".docs/prompts/capture.md", "# capture\n")
    write(root, ".docs/.hidden/y.md", "# hidden\n")
    write(root, "developer-docs/guides/guide.md", "# guide\n\nSee [the decision](../../.docs/adr/decision.md).\n")
    write(root, "developer-docs/scripts/helper-mod.md", "# helper-mod\n\nSource: [scripts/helper_mod.py](../../scripts/helper_mod.py).\n")
    return root


def edges(graph: sg.Graph, source: str | None = None, target: str | None = None, kind: str | None = None) -> list[dict]:
    return [e for e in graph.edges if (source is None or e["source"] == source) and (target is None or e["target"] == target) and (kind is None or e["type"] == kind)]


def findings(graph: sg.Graph, check: str | None = None, node: str | None = None) -> list[dict]:
    return [f for f in graph.check(live=False) if (check is None or f["check"] == check) and (node is None or f["node"] == node)]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_root(tmp_path / "tree")


@pytest.fixture
def graph(root: Path) -> sg.Graph:
    return sg.build_graph(root)


class TestSourceGraph:
    def test_nodes_are_classified(self, graph: sg.Graph) -> None:
        types = {n: graph.nodes[n]["type"] for n in graph.nodes}
        assert types["rules/alpha.md"] == "rule"
        assert types["rules/README.md"] == "index"
        assert types["hooks/hooks.json"] == "registry"
        assert types["hooks/present.sh"] == "hook"
        assert types["hooks/present.test.sh"] == "test"
        assert types["hooks/lib/helper.sh"] == "hook-lib"
        assert types["scripts/tool.py"] == "script"
        assert types["skills/good"] == "skill"
        assert types["agents/real.md"] == "agent"
        assert types["commands/go.md"] == "command"
        assert types["docs/notes/old.md"] == "doc"
        assert "docs/.hidden/x.md" not in types, "a dot directory is outside the graph"
        assert types["projects-root/proj-a/surfaces.json"] == "project-config"
        assert types["project:proj-a"] == "project"
        assert types["project:proj-b"] == "project"
        assert graph.nodes["skills/good"]["entry"]
        assert not graph.nodes["skills/empty"]["entry"]
        assert "skills/good/extra.md" not in graph.nodes

    def test_top_directories_follow_the_documentation_convention(self) -> None:
        assert set(sg.DOC_TOPS) == {".docs", "docs", "developer-docs"}
        assert set(sg.DOC_TOPS) <= set(sg.TOP_DIRS)
        assert sg.DOC_SKIP == tuple(f"{top}/{name}/" for top in sg.DOC_TOPS for name in ("prompts", "questions"))

    def test_hook_registrations(self, graph: sg.Graph) -> None:
        present = edges(graph, "hooks/hooks.json", "hooks/present.sh", "registers")
        assert len(present) == 1
        assert not present[0]["dangling"]
        assert present[0]["event"] == "PreToolUse"
        vanished = edges(graph, "hooks/hooks.json", "hooks/vanished.sh", "registers")
        assert vanished and vanished[0]["dangling"]
        assert any(f["node"] == "hooks/hooks.json" and "vanished" in f["message"] for f in findings(graph, "dangling-reference"))
        assert [f["node"] for f in findings(graph, "unregistered-hook")] == ["hooks/lonely.sh"]

    def test_code_edges(self, graph: sg.Graph) -> None:
        assert edges(graph, "hooks/present.sh", "hooks/lib/helper.sh", "invokes")
        assert edges(graph, "hooks/present.sh", "scripts/tool.py", "invokes")
        assert edges(graph, "scripts/tool.py", "scripts/helper_mod.py", "imports")
        stale = findings(graph, "stale-comment", "hooks/present.sh")
        assert len(stale) == 1
        assert stale[0]["evidence"] == "hooks/present.sh:2"
        assert stale[0]["severity"] == "warning"

    def test_source_root_variable_reference_is_an_edge(self, tmp_path: Path) -> None:
        tree = tmp_path / "vars"
        write(tree, "hooks/runner.sh", '#!/usr/bin/env bash\npython3 "$STRATARC_SOURCE/scripts/tool.py"\npython3 "$LLM_ROOT/scripts/other.py"\n')
        write(tree, "scripts/tool.py", '"""Tool."""\n')
        built = sg.build_graph(tree)
        assert edges(built, "hooks/runner.sh", "scripts/tool.py", "invokes")
        assert not edges(built, "hooks/runner.sh", "scripts/other.py"), "only the engine's own variable names a source file"

    def test_rule_citations(self, graph: sg.Graph) -> None:
        assert edges(graph, "rules/alpha.md", "rules/beta-rule.md", "cites")
        assert not edges(graph, target="docs/adr"), "a docs directory convention is not a repository path"
        retired = findings(graph, "retired-reference", "rules/beta-rule.md")
        assert len(retired) == 1
        assert retired[0]["severity"] == "warning"
        missing = findings(graph, "dangling-reference", "rules/beta-rule.md")
        assert [(f["severity"], f["evidence"]) for f in missing] == [("error", "rules/beta-rule.md:3")]

    def test_common_rules_selector(self, graph: sg.Graph) -> None:
        assert edges(graph, "scripts/rules_digest.py", "rules/alpha.md", "selects")
        retired = findings(graph, "retired-reference", "scripts/rules_digest.py")
        assert [f["severity"] for f in retired] == ["error"]
        dangling = findings(graph, "dangling-reference", "scripts/rules_digest.py")
        assert [f["severity"] for f in dangling] == ["error"]
        assert "rules/nowhere.md" in dangling[0]["message"]

    def test_rules_index(self, graph: sg.Graph) -> None:
        assert any("ghost" in f["message"] for f in findings(graph, "dangling-reference", "rules/README.md"))
        assert [f["node"] for f in findings(graph, "index-mismatch")] == ["rules/beta-rule.md"]

    def test_skills(self, graph: sg.Graph) -> None:
        assert [f["node"] for f in findings(graph, "skill-without-entry")] == ["skills/empty"]
        assert edges(graph, "skills/good", "agents/real.md", "cites")
        handoff = findings(graph, "dangling-reference", "skills/good")
        assert [(f["severity"], f["message"].split()[1]) for f in handoff] == [("error", "capability:spec-scribe")]
        phrase = findings(graph, "possible-dangling-reference", "skills/good")
        assert len(phrase) == 1
        assert "agents/missing-agent" in phrase[0]["message"]

    def test_control_plane_deploys(self, graph: sg.Graph) -> None:
        assert edges(graph, "rules/alpha.md", "project:proj-a", "deploys")
        assert not edges(graph, "rules/alpha.md", "project:proj-b", "deploys")
        assert graph.nodes["rules/alpha.md"]["global"]
        assert edges(graph, "hooks/present.sh", "project:proj-b", "deploys")

    def test_project_configuration(self, graph: sg.Graph) -> None:
        assert edges(graph, "projects-root/proj-a/surfaces.json", "project:proj-a", "configures")
        assert graph.nodes["projects-root/proj-a/surfaces.json"]["surfaces"] == ["AGENTS.md", "CODEX.md"]
        assert not findings(graph, node="projects-root/proj-a/surfaces.json"), "surface names are rendered outputs, not files beside the master"
        missing = findings(graph, "dangling-reference", "projects-root/proj-b/surfaces.json")
        assert [f["severity"] for f in missing] == ["error"]
        assert "projects-root/proj-b/AGENTS.md" in missing[0]["message"]

    def test_digest_headings(self, graph: sg.Graph) -> None:
        assert edges(graph, "AGENTS.md", "rules/alpha.md", "cites")
        nowhere = edges(graph, "AGENTS.md", "rules/nowhere.md", "cites")
        assert nowhere and nowhere[0]["dangling"]

    def test_tests_are_soft_and_counted(self, graph: sg.Graph) -> None:
        assert not findings(graph, node="scripts/tool.test.py")
        assert edges(graph, "scripts/tool.test.py", "scripts/tool.py", "tests")
        untested = [f["node"] for f in findings(graph, "missing-test")]
        assert "scripts/helper_mod.py" in untested
        assert "scripts/tool.py" not in untested
        assert "hooks/present.sh" not in untested

    def test_doc_references_are_warnings(self, graph: sg.Graph) -> None:
        old = findings(graph, "dangling-reference", "docs/notes/old.md")
        assert [f["severity"] for f in old] == ["warning"]

    def test_the_three_documentation_roots_are_in_the_graph(self, graph: sg.Graph) -> None:
        types = {n: graph.nodes[n]["type"] for n in graph.nodes}
        assert types["docs/notes/old.md"] == "doc"
        assert types[".docs/adr/decision.md"] == "doc"
        assert types["developer-docs/guides/guide.md"] == "doc"
        assert ".docs/prompts/capture.md" not in types, "a capture under .docs/ is skipped like one under docs/"
        assert ".docs/.hidden/y.md" not in types, "a dot directory below .docs/ is still outside the graph"
        for source, target in ((".docs/adr/decision.md", "developer-docs/guides/guide.md"), (".docs/adr/decision.md", "docs/notes/old.md"), ("developer-docs/guides/guide.md", ".docs/adr/decision.md")):
            links = edges(graph, source, target, "links")
            assert links and not links[0]["dangling"], f"{source} -> {target}"
        assert edges(graph, "rules/gamma.md", ".docs/adr/decision.md", "cites"), "a backticked .docs/ path is a citation"
        absent = findings(graph, "dangling-reference", "rules/gamma.md")
        assert [(f["severity"], f["message"].split()[1]) for f in absent] == [("warning", ".docs/adr/absent.md")]
        without_page = [f["node"] for f in findings(graph, "missing-doc-page")]
        assert "scripts/helper_mod.py" not in without_page, "a script page under developer-docs/scripts/ counts"
        assert "scripts/tool.py" not in without_page, "a script page under docs/scripts/ still counts"
        assert "scripts/rules_digest.py" in without_page

    def test_markdown_link_inside_a_fence_is_not_a_reference(self, graph: sg.Graph) -> None:
        assert edges(graph, "docs/notes/fenced.md", "docs/notes/old.md", "links")
        assert not edges(graph, "docs/notes/fenced.md", "docs/notes/ghost.md", "links"), "a link quoted inside a fenced code block is an example of the syntax, not a reference"
        assert not findings(graph, "dangling-reference", "docs/notes/fenced.md"), "the fenced link should not be flagged as dangling"

    def test_impact_stops_at_hubs(self, graph: sg.Graph) -> None:
        reached = {node: (depth, edge["type"]) for depth, node, edge in graph.dependents(["hooks/lib/helper.sh"])}
        assert reached["hooks/present.sh"] == (1, "invokes")
        assert reached["hooks/hooks.json"] == (2, "registers")
        assert reached["project:proj-b"] == (2, "deploys")
        assert "control-plane.md" in reached
        assert "rules/alpha.md" not in reached, "the inventory links every file and must not be walked through"
        alpha = {node for _, node, _ in graph.dependents(["rules/alpha.md"])}
        assert "project:proj-a" in alpha
        assert "AGENTS.md" in alpha

    def test_node_for_maps_skill_files(self, graph: sg.Graph) -> None:
        assert graph.node_for("skills/good/SKILL.md") == "skills/good"
        assert graph.node_for("skills/good/extra.md") == "skills/good"
        assert graph.node_for("skills/nope/SKILL.md") is None


class TestCommandLine:
    def test_check_exit_codes(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert sg.main(["--root", str(root), "check", "--no-live", "--strict"]) == 1
        assert "error   dangling-reference" in capsys.readouterr().out
        assert sg.main(["--root", str(root), "check", "--no-live"]) == 0

    def test_root_comes_from_the_environment_without_a_flag(self, root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setenv("STRATARC_SOURCE", str(root))
        assert sg.main(["check", "--no-live"]) == 0
        assert "source-graph:" in capsys.readouterr().out

    def test_root_defaults_to_the_source_root_discovery(self, root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.delenv("STRATARC_SOURCE", raising=False)
        write(root, "stratarc.toml", 'name = "acme"\n')
        monkeypatch.chdir(root / "rules")
        assert sg.main(["impact", "rules/alpha.md"]) == 0
        assert "impact of rules/alpha.md" in capsys.readouterr().out

    def test_build_writes_graph_and_viewer(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out = root / "out"
        assert sg.main(["--root", str(root), "build", "--out", str(out), "--fragment", str(out / "fragment.html")]) == 0
        capsys.readouterr()
        fragment = (out / "fragment.html").read_text(encoding="utf-8")
        assert fragment.startswith("<title>")
        assert "<!doctype" not in fragment
        assert '"findings"' in fragment
        payload = json.loads((out / "source-graph.json").read_text(encoding="utf-8"))
        assert payload["directed"]
        assert payload["graph"] == {"root": "stratarc", "generator": sg.GENERATOR}
        ids = {n["id"] for n in payload["nodes"]}
        assert "hooks/vanished.sh" in ids
        assert next(n for n in payload["nodes"] if n["id"] == "hooks/vanished.sh")["type"] == "missing"
        assert all({"source", "target", "type", "evidence"} <= set(link) for link in payload["links"])
        html = (out / "source-graph.html").read_text(encoding="utf-8")
        assert '"findings"' in html
        assert "d3.min.js" in html
        assert "</script>" not in html.split('id="data"')[1].split("</script>")[0]
        assert "llm-root" not in html

    def test_impact_reports_unknown_paths(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert sg.main(["--root", str(root), "impact", "skills/good/extra.md", "nothing.txt"]) == 0
        text = capsys.readouterr().out
        assert "nothing.txt is not in the graph" in text
        assert "impact of skills/good" in text

    def test_impact_with_nothing_to_assess_exits_two(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert sg.main(["--root", str(root), "impact"]) == 2
        assert "nothing to assess" in capsys.readouterr().err


class TestRootName:
    def test_the_default_name_is_the_engine_name(self, root: Path) -> None:
        assert sg.build_graph(root).to_json()["graph"]["root"] == "stratarc"

    def test_a_configured_name_is_the_graph_root(self, root: Path) -> None:
        write(root, "stratarc.toml", 'name = "acme"\n')
        assert sg.build_graph(root).to_json()["graph"]["root"] == "acme"

    def test_the_environment_name_wins(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        write(root, "stratarc.toml", 'name = "acme"\n')
        monkeypatch.setenv("STRATARC_NAME", "other")
        assert sg.build_graph(root).to_json()["graph"]["root"] == "other"

    def test_the_directory_name_is_never_the_root(self, tmp_path: Path) -> None:
        first = make_root(tmp_path / "one")
        second = make_root(tmp_path / "two")
        assert sg.build_graph(first).to_json() == sg.build_graph(second).to_json()


class TestLiveChecks:
    def test_live_adds_nothing_without_a_digest_source_or_inventory(self, tmp_path: Path) -> None:
        tree = tmp_path / "bare"
        write(tree, "scripts/tool.py", '"""Tool."""\n')
        built = sg.build_graph(tree)
        assert built.check(live=True) == built.check(live=False)

    def test_a_control_plane_that_disagrees_with_source_is_drift(self, tmp_path: Path) -> None:
        tree = tmp_path / "drift"
        write(tree, "stratarc.toml", 'name = "stratarc"\n')
        write(tree, "rules/alpha.md", "# alpha\n")
        write(tree, "control-plane.md", "# control-plane\n\nnot a generated inventory\n")
        built = sg.build_graph(tree)
        live = [f for f in built.check(live=True) if f["check"] == "control-plane-drift"]
        assert [f["node"] for f in live] == ["control-plane.md"]
        assert not [f for f in built.check(live=False) if f["check"] == "control-plane-drift"]


class TestValidateIntegration:
    def test_the_reference_check_runs_instead_of_skipping(self, tmp_path: Path) -> None:
        from stratarc import validate

        write(tmp_path, "hooks/hooks.json", json.dumps({"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "hooks/vanished.sh"}]}]}))
        items = validate.check_reference_graph(tmp_path)
        assert not [item for item in items if item["check"] == "reference-graph"], "the graph is installed, so the skip notice is gone"
        assert [item["check"] for item in items] == ["dangling-reference"]


class TestDefinitionChecks:
    @pytest.fixture
    def definitions(self, tmp_path: Path) -> sg.Graph:
        """A tree whose skills, agents and commands carry one definition defect each, beside a clean control."""
        tree = tmp_path / "definitions"
        write(tree, "rules/alpha.md", "# alpha\n")
        write(tree, "rules/README.md", "# rules\n\n| File | Purpose |\n|---|---|\n| `alpha.md` | first |\n")
        write(tree, "hooks/lib/present.sh", "#!/usr/bin/env bash\n")
        write(tree, "skills/clean/SKILL.md", "---\nname: clean\ndescription: \"A clean skill. Read `references/present.md` and follow the `alpha` rule.\"\n---\n\n# clean\n\nThe deploying project's `docs/adr/0001-company-layer.md` is not a source root path. The runtime copy is `~/.claude/skills/clean/references/present.md`, scratch lives in `~/.claude/tmp/active-task.md`, and the helper is `~/.claude/hooks/lib/present.sh`. A source line reads `- [Title](url), publisher`, and a loop belongs in a script under `scripts/`. Unquoted, the helper is ~/.claude/hooks/lib/present.sh.\n")
        write(tree, "docs/notes/runtime.md", "# runtime\n\n`~/.claude/skills/CLAUDE.md` is foreign runtime content nothing in source writes.\n")
        write(tree, "skills/clean/references/present.md", "# present\n")
        write(tree, "skills/renamed/SKILL.md", "---\nname: other-name\ndescription: A skill whose name is not its directory.\n---\n\n# renamed\n")
        write(tree, "skills/wordy/SKILL.md", "---\nname: wordy\ndescription: \"" + ("word " * 210).strip() + "\"\n---\n\n# wordy\n")
        write(tree, "skills/folded/SKILL.md", "---\nname: folded\ndescription: >-\n  " + ("word " * 110).strip() + "\n  " + ("word " * 110).strip() + "\nlicense: MIT\n---\n\n# folded\n")
        write(tree, "skills/wrapped/SKILL.md", "---\nname: wrapped\ndescription: " + ("word " * 110).strip() + "\n  " + ("word " * 110).strip() + "\n---\n\n# wrapped\n")
        write(tree, "skills/lost/SKILL.md", "---\nname: lost\ndescription: A skill that loads a reference it does not ship.\n---\n\n# lost\n\nLoad `references/ADVERSARIAL_REVIEW.md` and see [the rubric](references/rubric.md).\n")
        write(tree, "skills/pointer/SKILL.md", "---\nname: pointer\ndescription: A skill whose procedure is a source root path.\n---\n\n# pointer\n\nThe procedure is `rules/alpha.md`. Read it whole.\n")
        write(tree, "agents/planner.md", "---\nname: planner\ndescription: An agent that runs a deployed helper nobody ships.\ntools: Read, Bash\n---\n\n# planner\n\nRun `bash ~/.claude/hooks/lib/deletion-plan.sh \"<the rm command>\"`.\n")
        return sg.build_graph(tree)

    def test_clean_definition_has_no_findings(self, definitions: sg.Graph) -> None:
        assert findings(definitions, node="skills/clean") == []
        assert findings(definitions, node="docs/notes/runtime.md") == [], "a document may name runtime output no source file writes"

    def test_skill_name_must_match_its_directory(self, definitions: sg.Graph) -> None:
        mismatch = findings(definitions, "skill-name-mismatch")
        assert [(f["severity"], f["node"]) for f in mismatch] == [("error", "skills/renamed")]
        assert "other-name" in mismatch[0]["message"]

    def test_skill_description_over_the_limit_is_an_error(self, definitions: sg.Graph) -> None:
        long = findings(definitions, "description-too-long")
        assert sorted((f["severity"], f["node"]) for f in long) == [("error", "skills/folded"), ("error", "skills/wordy"), ("error", "skills/wrapped")]

    def test_missing_skill_local_reference_is_an_error(self, definitions: sg.Graph) -> None:
        lost = findings(definitions, "dangling-reference", "skills/lost")
        assert sorted((f["severity"], f["message"].split()[1]) for f in lost) == [("error", "skills/lost/references/ADVERSARIAL_REVIEW.md"), ("error", "skills/lost/references/rubric.md")]

    def test_runtime_home_path_resolves_to_source(self, definitions: sg.Graph) -> None:
        planner = findings(definitions, "dangling-reference", "agents/planner.md")
        assert [(f["severity"], f["message"].split()[1]) for f in planner] == [("error", "hooks/lib/deletion-plan.sh")]
        assert edges(definitions, "skills/clean", "hooks/lib/present.sh")

    def test_source_root_path_in_a_definition_is_an_error(self, definitions: sg.Graph) -> None:
        pointer = findings(definitions, "project-relative-path")
        assert [(f["severity"], f["node"]) for f in pointer] == [("error", "skills/pointer")]
        assert "rules/alpha.md" in pointer[0]["message"]
        assert "llm-root" not in pointer[0]["message"]

    def test_strict_check_fails_on_definition_defects(self, definitions: sg.Graph, capsys: pytest.CaptureFixture[str]) -> None:
        assert sg.main(["--root", str(definitions.root), "check", "--no-live", "--strict"]) == 1
        assert "skill-name-mismatch" in capsys.readouterr().out


def git(cwd: Path, *args: str) -> str:
    """Run git in a fixture with the machine's own configuration (signing, hooks path, excludes file) kept out."""
    result = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args], check=True, capture_output=True, text=True, env=GIT_ENV)
    return result.stdout


def make_repository(base: Path) -> Path:
    """The fixture tree as one commit in a real repository, with a gitignored derived store cited by a page."""
    origin = base / "origin"
    make_root(origin)
    write(origin, ".gitignore", "docs/prompts/prompts.json\ndocs/prompts/prompts.sqlite\ndocs/prompts/actual:output\ndocs/prompts/*.example\n!docs/prompts/missing.example\nbuild/\nbuild.v1/\n*.env\ncache\n!cache/\n")
    write(origin, "docs/scripts/capture.md", "# capture\n\nThe store writes `docs/prompts/prompts.json`, `docs/prompts/prompts.sqlite`, and `docs/prompts/actual:output` beside the notes, and the helper is `hooks/lib/helper.sh`. A local build lands in [build](../../build/) and [build.v1](../../build.v1/), `docs/prompts/missing.example` should exist, and `scripts/local-only.py` is one machine's own. The [cache](../../cache/) directory is not ignored.\n")
    write(origin, "skills/good/SKILL.md", "---\nname: good\ndescription: Use the `real` agent. Hand off to the missing-agent agent when stuck (use spec-scribe).\n---\n\n# good\n\nSee [local environment](.env).\n")
    git(origin, "init", "-q")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "fixture")
    return origin


class TestDeterminism:
    """The same commit builds the same graph in every checkout."""

    @pytest.fixture
    def checkouts(self, tmp_path: Path) -> tuple[Path, Path, Path, sg.Graph, sg.Graph]:
        origin = make_repository(tmp_path)
        clean = tmp_path / "clean"
        dirty = tmp_path / "dirty"
        git(origin, "clone", "-q", str(origin), str(clean))
        git(origin, "clone", "-q", str(origin), str(dirty))
        # The dirty checkout holds what a used checkout holds: the gitignored derived store, the ignored
        # build directory, an untracked file, and a machine-local exclude for a path the page cites.
        write(dirty, "docs/prompts/prompts.json", "{}\n")
        write(dirty, "docs/prompts/prompts.sqlite", "")
        write(dirty, "build/out.json", "{}\n")
        write(dirty, "hooks/scratch.sh", "#!/usr/bin/env bash\n")
        write(dirty, ".git/info/exclude", "scripts/local-only.py\n")
        return origin, clean, dirty, sg.build_graph(clean), sg.build_graph(dirty)

    def test_two_checkouts_of_one_commit_build_identical_graphs(self, checkouts) -> None:
        _, _, _, clean_graph, dirty_graph = checkouts
        assert clean_graph.to_json() == dirty_graph.to_json()
        assert clean_graph.check(live=False) == dirty_graph.check(live=False)

    def test_root_is_the_engine_name(self, checkouts) -> None:
        _, _, _, clean_graph, dirty_graph = checkouts
        assert clean_graph.to_json()["graph"]["root"] == "stratarc"
        assert dirty_graph.to_json()["graph"]["root"] == "stratarc"

    def test_gitignored_path_is_a_derived_output(self, checkouts) -> None:
        for graph in checkouts[3:]:
            store = edges(graph, "docs/scripts/capture.md", "docs/prompts/prompts.json")
            assert len(store) == 1
            assert store[0]["type"] == sg.DERIVED_EDGE
            assert not store[0]["dangling"]
            assert [f for f in findings(graph, node="docs/scripts/capture.md") if "prompts.json" in f["message"]] == [], "a derived output is never a finding"
            derived = [n for n in graph.to_json()["nodes"] if n["id"] == "docs/prompts/prompts.json"]
            assert [n["type"] for n in derived] == ["derived"]

    def test_ignored_directory_pattern_matches_the_bare_directory(self, checkouts) -> None:
        for graph in checkouts[3:]:
            build = edges(graph, "docs/scripts/capture.md", "build")
            assert [(e["type"], e["dangling"]) for e in build] == [(sg.DERIVED_EDGE, False)]

    def test_ignored_directory_with_suffix_is_derived(self, checkouts) -> None:
        for graph in checkouts[3:]:
            build = edges(graph, "docs/scripts/capture.md", "build.v1")
            assert [(e["type"], e["dangling"]) for e in build] == [(sg.DERIVED_EDGE, False)]

    def test_ignored_skill_local_path_is_derived(self, checkouts) -> None:
        for graph in checkouts[3:]:
            local = edges(graph, "skills/good", "skills/good/.env")
            assert [(e["type"], e["dangling"]) for e in local] == [(sg.DERIVED_EDGE, False)]
            assert [f for f in findings(graph, "dangling-reference", "skills/good") if ".env" in f["message"]] == []

    def test_negated_gitignore_pattern_leaves_a_missing_reference_dangling(self, checkouts) -> None:
        for graph in checkouts[3:]:
            missing = edges(graph, "docs/scripts/capture.md", "docs/prompts/missing.example")
            assert [(e["type"], e["dangling"]) for e in missing] == [("cites", True)]
            assert any("docs/prompts/missing.example" in f["message"] for f in findings(graph, "dangling-reference", "docs/scripts/capture.md"))

    def test_directory_negation_after_a_bare_pattern_leaves_the_directory_dangling(self, checkouts) -> None:
        # ``cache`` then ``!cache/``: git ignores a file named cache but not the directory, so a link to ``cache/`` is missing, not derived.
        for graph in checkouts[3:]:
            cache = edges(graph, "docs/scripts/capture.md", "cache")
            assert [(e["type"], e["dangling"]) for e in cache] == [("links", True)]
            assert any("cache" in f["message"] for f in findings(graph, "dangling-reference", "docs/scripts/capture.md"))

    def test_ignored_checks_only_the_form_the_caller_names(self, checkouts) -> None:
        for graph in checkouts[3:]:
            assert not graph.ignored("cache", directory=True), "the !cache/ negation leaves the directory unignored"
            assert graph.ignored("cache", directory=False), "a plain file named cache is still ignored"

    def test_colon_in_ignore_pattern_still_marks_output_derived(self, checkouts) -> None:
        for graph in checkouts[3:]:
            assert graph.ignored("docs/prompts/actual:output")

    def test_only_a_tracked_gitignore_makes_a_path_derived(self, checkouts) -> None:
        for graph in checkouts[3:]:
            local = edges(graph, "docs/scripts/capture.md", "scripts/local-only.py")
            assert [(e["type"], e["dangling"]) for e in local] == [("cites", True)], "a .git/info/exclude match is one checkout's opinion"

    def test_untracked_nested_gitignore_cannot_override_tracked_rules(self, checkouts) -> None:
        _, _, dirty, clean_graph, _ = checkouts
        write(dirty, "docs/prompts/.gitignore", "!prompts.json\nmissing.example\n")
        rebuilt = sg.build_graph(dirty)
        assert clean_graph.to_json() == rebuilt.to_json()
        assert clean_graph.check(live=False) == rebuilt.check(live=False)

    def test_untracked_files_are_outside_the_graph(self, checkouts) -> None:
        graph = checkouts[4]
        assert "hooks/scratch.sh" not in graph.nodes
        assert "docs/.hidden/x.md" not in graph.nodes, "a tracked file under a dot directory is outside the graph"
        assert graph.exists("hooks/lib/helper.sh")
        assert not graph.exists("hooks/scratch.sh")

    def test_tracked_dot_docs_root_is_walked(self, checkouts) -> None:
        graph = checkouts[3]
        assert graph.tracked is not None
        assert graph.nodes[".docs/adr/decision.md"]["type"] == "doc"
        assert graph.nodes["developer-docs/guides/guide.md"]["type"] == "doc"
        assert ".docs/.hidden/y.md" not in graph.nodes
        assert "docs/.hidden/x.md" not in graph.nodes
        assert edges(graph, "developer-docs/guides/guide.md", ".docs/adr/decision.md", "links")

    def test_a_plain_directory_still_reads_the_filesystem(self, tmp_path: Path) -> None:
        plain = make_root(tmp_path / "plain")
        graph = sg.build_graph(plain)
        assert graph.tracked is None
        assert graph.exists("hooks/lib/helper.sh")
        assert "hooks/present.sh" in graph.nodes

    def test_git_discovery_error_does_not_read_untracked_files(self, checkouts, monkeypatch: pytest.MonkeyPatch) -> None:
        dirty = checkouts[2]
        original_run = subprocess.run

        def fail_rev_parse(args, **kwargs):
            if args[:2] == ["git", "-C"] and Path(args[2]).resolve() == dirty.resolve() and args[3:] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership in repository")
            return original_run(args, **kwargs)

        monkeypatch.setattr(sg.subprocess, "run", fail_rev_parse)
        with pytest.raises(RuntimeError, match="git rev-parse exited 128.*dubious ownership"):
            sg.build_graph(dirty)

    def test_nested_worktree_error_is_not_a_plain_directory(self, checkouts, monkeypatch: pytest.MonkeyPatch) -> None:
        nested = checkouts[2] / "hooks"
        failed = subprocess.CompletedProcess([], 128, "", "fatal: not a git repository")
        monkeypatch.setattr(sg.subprocess, "run", lambda *args, **kwargs: failed)
        with pytest.raises(RuntimeError, match="git rev-parse exited 128"):
            sg.tracked_files(nested)


class TestUntrackedRegistries:
    @pytest.fixture
    def checkouts(self, tmp_path: Path) -> tuple[Path, Path]:
        origin = make_repository(tmp_path)
        write(origin, "skills/empty/extra.md", "# extra\n")
        write(origin, "skills/excluded/extra.md", "# extra\n")
        git(origin, "rm", "rules/retired.json", "skills/sync-exclude.json")
        git(origin, "add", "skills/empty/extra.md", "skills/excluded/extra.md")
        git(origin, "commit", "-q", "-m", "remove registries")
        clean = tmp_path / "clean"
        dirty = tmp_path / "dirty"
        git(origin, "clone", "-q", str(origin), str(clean))
        git(origin, "clone", "-q", str(origin), str(dirty))
        return clean, dirty

    def test_untracked_retired_registry_does_not_change_findings(self, checkouts) -> None:
        clean_root, dirty_root = checkouts
        write(dirty_root, "rules/retired.json", json.dumps({"retired": ["gone-rule.md"]}))
        clean = sg.build_graph(clean_root)
        dirty = sg.build_graph(dirty_root)
        assert findings(clean, "retired-reference") == findings(dirty, "retired-reference")
        assert clean.check(live=False) == dirty.check(live=False)

    def test_untracked_skill_exclusion_does_not_suppress_finding(self, checkouts) -> None:
        clean_root, dirty_root = checkouts
        write(dirty_root, "skills/sync-exclude.json", json.dumps({"exclude": {"empty": "local"}}))
        clean = sg.build_graph(clean_root)
        dirty = sg.build_graph(dirty_root)
        assert [f["node"] for f in findings(clean, "skill-without-entry")] == ["skills/empty", "skills/excluded"]
        assert findings(clean, "skill-without-entry") == findings(dirty, "skill-without-entry")


class TestSourceRootBelowTheWorkTreeTop:
    def test_a_tracked_subdirectory_is_read_from_the_commit(self, tmp_path: Path) -> None:
        repository = tmp_path / "repository"
        make_root(repository / "nested" / "source")
        write(repository, "README.md", "# repository\n")
        git(repository, "init", "-q")
        git(repository, "add", "-A")
        git(repository, "commit", "-q", "-m", "fixture")
        write(repository, "nested/source/hooks/scratch.sh", "#!/usr/bin/env bash\n")
        graph = sg.build_graph(repository / "nested" / "source")
        assert graph.tracked is not None
        assert "hooks/present.sh" in graph.nodes
        assert "hooks/scratch.sh" not in graph.nodes, "an untracked file is outside the graph"
        assert "README.md" in graph.nodes and "nested/source/README.md" not in graph.nodes, "paths are relative to the source root"

    def test_an_untracked_subdirectory_reads_the_filesystem(self, tmp_path: Path) -> None:
        repository = tmp_path / "repository"
        write(repository, "README.md", "# repository\n")
        git(repository, "init", "-q")
        git(repository, "add", "-A")
        git(repository, "commit", "-q", "-m", "fixture")
        make_root(repository / "scratch")
        graph = sg.build_graph(repository / "scratch")
        assert graph.tracked is None
        assert "hooks/present.sh" in graph.nodes
