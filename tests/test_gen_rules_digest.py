"""Behavioral tests for the gen-rules-digest command: --print, --embed, --check, --source-root and --column.

test_rules_digest.py covers the rendering functions. These cases drive the command's main() against a temporary source root and a temporary copy of each target, and one case runs it as a module in a subprocess.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stratarc.gen_rules_digest import main
from stratarc.rules_digest import DIGEST_BEGIN, DIGEST_END

FIXTURE_RULES = Path(__file__).resolve().parent / "fixtures" / "rules"
TOKEN = "${STRATARC_SOURCE}"

CONTROL_PLANE = """\
# control-plane

## rules

| option | tier | global | demo |
|---|---|---|---|
| [rule:never-commit-local-databases](rules/never-commit-local-databases.md) | global | x | x |
| [rule:conventional-commit-messages](rules/conventional-commit-messages.md) | common | x | x |
| [rule:tests-use-a-temporary-directory](rules/tests-use-a-temporary-directory.md) | project | | x |
"""


def marked_document(body: str = "placeholder digest content") -> str:
    return (
        "# Some Surface\n\n"
        "Text before the markers stays put.\n\n"
        f"{DIGEST_BEGIN}\n{body}\n{DIGEST_END}\n\n"
        "Text after the markers stays put.\n"
    )


@pytest.fixture
def tree(source_root) -> Path:
    """A source root holding the fixture rules and a control plane."""
    shutil.copytree(FIXTURE_RULES, source_root / "rules")
    (source_root / "control-plane.md").write_text(CONTROL_PLANE, encoding="utf-8")
    return source_root


@pytest.fixture
def target(tmp_path) -> Path:
    path = tmp_path / "AGENTS.md"
    path.write_text(marked_document(), encoding="utf-8")
    return path


class TestPrint:
    def test_print_renders_the_source_roots_rules(self, tree, capsys):
        assert main(["--print"]) == 0
        out = capsys.readouterr().out
        assert DIGEST_BEGIN in out
        assert "### never-commit-local-databases" in out

    def test_a_source_root_without_rules_fails(self, source_root, capsys):
        assert main(["--print"]) == 1
        assert "no rule tree found to render" in capsys.readouterr().err

    def test_running_as_a_module_prints_the_digest(self, tree, repo_root):
        env = {k: v for k, v in os.environ.items() if k != "RULES_DIGEST_SOURCE"}
        env["STRATARC_SOURCE"] = str(tree)
        env["PYTHONPATH"] = str(repo_root)
        result = subprocess.run(
            [sys.executable, "-m", "stratarc.gen_rules_digest", "--print"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=tree,
        )
        assert result.returncode == 0, result.stderr
        assert "### conventional-commit-messages" in result.stdout


class TestEmbed:
    def test_embed_replaces_the_marker_region(self, tree, target, capsys):
        assert main(["--embed", str(target)]) == 0
        updated = target.read_text(encoding="utf-8")
        assert "Text before the markers stays put." in updated
        assert "Text after the markers stays put." in updated
        assert DIGEST_BEGIN in updated and DIGEST_END in updated
        assert "### never-commit-local-databases" in updated
        assert "placeholder digest content" not in updated
        assert "refreshed digest" in capsys.readouterr().out

    def test_embed_is_idempotent_once_current(self, tree, target, capsys):
        main(["--embed", str(target)])
        current = target.read_text(encoding="utf-8")
        capsys.readouterr()
        assert main(["--embed", str(target)]) == 0
        assert "already current" in capsys.readouterr().out
        assert target.read_text(encoding="utf-8") == current

    def test_embed_reports_a_missing_marker_pair_and_leaves_the_file(self, tree, tmp_path, capsys):
        unprepared = tmp_path / "unprepared.md"
        before = "# Unprepared\n\nNo marker pair here.\n"
        unprepared.write_text(before, encoding="utf-8")
        assert main(["--embed", str(unprepared)]) != 0
        assert "no well-formed RULES-DIGEST marker pair" in capsys.readouterr().err
        assert unprepared.read_text(encoding="utf-8") == before

    def test_embed_reports_an_unreadable_target(self, tree, tmp_path, capsys):
        missing = tmp_path / "no-such-directory" / "AGENTS.md"
        assert main(["--embed", str(missing)]) != 0
        assert str(missing) in capsys.readouterr().err


class TestCheck:
    def test_check_detects_a_stale_digest_and_never_writes(self, tree, target, capsys):
        assert main(["--check", str(target)]) != 0
        assert "digest is stale; re-run --embed" in capsys.readouterr().err
        assert "placeholder digest content" in target.read_text(encoding="utf-8")

    def test_check_passes_once_the_target_is_current(self, tree, target, capsys):
        assert main(["--embed", str(target)]) == 0
        capsys.readouterr()
        assert main(["--check", str(target)]) == 0
        assert "all targets in sync with the rule tree." in capsys.readouterr().out


class TestSourceRoot:
    """By default the digest is portable and keeps the token; --source-root is the deploy-time render."""

    @pytest.fixture
    def public(self, tmp_path) -> Path:
        root = tmp_path / "public-tree"
        (root / "rules").mkdir(parents=True)
        (root / "rules" / "example-rule.md").write_text(
            f"# example-rule\n\n## binding\n\nRun `python3 {TOKEN}/scripts/x.py`.\n", encoding="utf-8"
        )
        (root / "rules" / "tiers.json").write_text('{"global": ["example-rule.md"]}', encoding="utf-8")
        return root

    def test_the_default_render_keeps_the_token_and_names_no_machine(self, public, stratarc_home, capsys):
        assert main(["--print", "--rules-root", str(public / "rules")]) == 0
        out = capsys.readouterr().out
        assert f"Full rule: `{TOKEN}/rules/example-rule.md`" in out
        assert f"python3 {TOKEN}/scripts/x.py" in out
        assert "$HOME" not in out
        assert str(stratarc_home) not in out

    def test_source_root_renders_pointers_and_tokens_to_that_checkout(self, public, capsys):
        assert main(["--print", "--rules-root", str(public / "rules"), "--source-root", str(public)]) == 0
        out = capsys.readouterr().out
        assert TOKEN not in out
        pointer = re.search(r"Full rule: `(.*)/rules/example-rule\.md`", out)
        assert pointer is not None, out
        assert pointer.group(1).endswith("/public-tree")
        assert f"python3 {pointer.group(1)}/scripts/x.py" in out

    def test_source_root_renders_the_whole_embed_target(self, public, capsys):
        target = public / "CLAUDE.md"
        target.write_text(
            f"# Surface\n\nRun `{TOKEN}/scripts/sync.py`.\n\n{DIGEST_BEGIN}\nold\n{DIGEST_END}\n", encoding="utf-8"
        )
        args = ["--embed", str(target), "--rules-root", str(public / "rules"), "--source-root", str(public)]
        assert main(args) == 0
        rendered = target.read_text(encoding="utf-8")
        assert TOKEN not in rendered
        assert "/public-tree/scripts/sync.py" in rendered
        assert "/public-tree/rules/example-rule.md`" in rendered
        capsys.readouterr()
        assert main(args) == 0
        assert "already current" in capsys.readouterr().out

    def test_an_embed_without_source_root_keeps_the_target_portable(self, public):
        target = public / "AGENTS.md"
        target.write_text(
            f"# Master\n\nRun `{TOKEN}/scripts/sync.py`.\n\n{DIGEST_BEGIN}\nold\n{DIGEST_END}\n", encoding="utf-8"
        )
        assert main(["--embed", str(target), "--rules-root", str(public / "rules")]) == 0
        text = target.read_text(encoding="utf-8")
        assert f"Run `{TOKEN}/scripts/sync.py`." in text
        assert f"Full rule: `{TOKEN}/rules/example-rule.md`" in text
        assert "public-tree" not in text.split(DIGEST_BEGIN, 1)[1]

    def test_prefix_overrides_the_pointer_only(self, public, capsys):
        assert main(["--print", "--rules-root", str(public / "rules"), "--prefix", "rules/"]) == 0
        out = capsys.readouterr().out
        assert "Full rule: `rules/example-rule.md`" in out
        assert f"python3 {TOKEN}/scripts/x.py" in out


class TestColumn:
    """--column renders one control-plane column, or fails naming the known ones."""

    def test_a_valid_column_renders_its_opt_ins(self, tree, capsys):
        assert main(["--print", "--column", "demo"]) == 0
        out = capsys.readouterr().out
        assert "### tests-use-a-temporary-directory" in out

    def test_the_global_column_omits_a_project_rule_it_did_not_select(self, tree, capsys):
        assert main(["--print", "--column", "global"]) == 0
        out = capsys.readouterr().out
        assert "### never-commit-local-databases" in out
        assert "### tests-use-a-temporary-directory" not in out

    def test_an_unknown_column_exits_with_the_known_columns_listed(self, tree, capsys):
        assert main(["--print", "--column", "not-a-real-column"]) == 1
        err = capsys.readouterr().err
        assert "unknown column 'not-a-real-column'; known columns:" in err
        assert "global" in err
