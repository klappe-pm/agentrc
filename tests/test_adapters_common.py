"""Regression tests for mirroring source configuration without runtime caches."""

from pathlib import Path

import pytest
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


from stratarc.adapters._common import (
    OwnedDir,
    classify_registration,
    extract_script_paths,
    home,
    in_managed_runtime_tree,
    is_existence_guarded,
    mirror_dir,
    mirrored_names,
    owned_dir_entries,
    read_frontmatter,
    repoint_command,
    runtime_registry,
    source_hook_names,
)


@pytest.fixture(autouse=True)
def _isolated_home(stratarc_home):
    """Every test reads the home through STRATARC_HOME, never the real one."""
    return stratarc_home



class TestHome(unittest.TestCase):
    """STRATARC_HOME redirects every runtime target and every $HOME expansion;
    unset or empty, the home is the process's own, as before the accessor."""

    def test_stratarc_home_redirects_every_runtime_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp)
            with patch.dict(os.environ, {"STRATARC_HOME": tmp}):
                self.assertEqual(home(), override)
                for name, runtime in runtime_registry().items():
                    with self.subTest(runtime=name):
                        self.assertEqual(runtime.target(), override / runtime.relative)
                self.assertEqual(
                    extract_script_paths("$HOME/.codex/hooks/guard.sh"),
                    [f"{tmp}/.codex/hooks/guard.sh"],
                )

    def test_unset_falls_back_to_the_process_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}):
                os.environ.pop("STRATARC_HOME", None)
                self.assertEqual(home(), Path(tmp))
                for name, runtime in runtime_registry().items():
                    with self.subTest(runtime=name):
                        self.assertEqual(runtime.target(), Path(tmp) / runtime.relative)

    def test_empty_value_falls_back_to_the_process_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp, "STRATARC_HOME": ""}):
                self.assertEqual(home(), Path(tmp))


class TestReadFrontmatterBlockScalars(unittest.TestCase):
    """A folded or literal block scalar is read as its text, not its indicator.

    Found by WI-32 on 2026-09-23: six source agents open their description
    with `>-`, and ~/.codex/agents/precommit-guard.toml, as deployed, carried
    description = ">-" because the indicator was kept as the value.
    """

    def test_folded_scalar_joins_lines_with_spaces(self):
        meta, body = read_frontmatter(
            "---\nname: a\ndescription: >-\n  Use this agent\n  before a commit.\nmodel: inherit\n---\nBody.\n"
        )
        self.assertEqual(meta["description"], "Use this agent before a commit.")
        self.assertEqual(meta["model"], "inherit")
        self.assertEqual(body, "Body.\n")

    def test_literal_scalar_keeps_line_breaks(self):
        meta, _ = read_frontmatter("---\ndescription: |\n  one\n  two\n---\n")
        self.assertEqual(meta["description"], "one\ntwo")

    def test_every_source_agent_description_is_text(self):
        agents = Path(__file__).resolve().parents[1] / "examples" / "notes-cli" / "source" / "agents"
        for path in sorted(agents.glob("*.md")):
            with self.subTest(agent=path.name):
                meta, _ = read_frontmatter(path.read_text())
                self.assertNotIn(meta.get("description", "x")[:1], (">", "|"))


class TestMirrorCaches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source"
        self.target = Path(self.tmp.name) / "target"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "hook.py").write_text("print('current')\n")

    def test_caches_are_neither_copied_nor_deleted(self):
        for root in (self.source, self.target):
            cache = root / "lib" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "hook.cpython-314.pyc").write_bytes(root.name.encode())
            (root / "legacy.pyc").write_bytes(root.name.encode())
            (root / "legacy.pyo").write_bytes(root.name.encode())
        actions = mirror_dir(self.source, self.target)
        self.assertFalse(
            any("pycache" in action or "legacy.py" in action for action in actions)
        )
        self.assertEqual(
            (self.target / "lib/__pycache__/hook.cpython-314.pyc").read_bytes(),
            b"target",
        )
        self.assertEqual((self.target / "legacy.pyc").read_bytes(), b"target")
        self.assertEqual((self.target / "legacy.pyo").read_bytes(), b"target")
        self.assertEqual(mirror_dir(self.source, self.target, dry_run=True), [])
        self.assertEqual(mirror_dir(self.source, self.target), [])

    def test_empty_cache_is_not_removed(self):
        cache = self.target / "lib" / "__pycache__"
        cache.mkdir(parents=True)
        original = Path.rmdir

        def guarded_rmdir(path):
            if path == cache:
                raise PermissionError("runtime cache is protected")
            return original(path)

        with patch.object(Path, "rmdir", guarded_rmdir):
            mirror_dir(self.source, self.target)
        self.assertTrue(cache.is_dir())

    def test_source_cache_does_not_ship(self):
        cache = self.source / "__pycache__"
        cache.mkdir()
        (cache / "hook.pyc").write_bytes(b"cache")
        mirror_dir(self.source, self.target)
        self.assertFalse((self.target / "__pycache__").exists())

    def test_managed_updates_and_stale_deletions_still_work(self):
        stale = self.target / "old" / "nested"
        stale.mkdir(parents=True)
        (stale / "retired.py").write_text("old\n")
        preview = mirror_dir(self.source, self.target, dry_run=True)
        self.assertTrue(any("would delete" in action for action in preview))
        self.assertTrue((stale / "retired.py").exists())
        mirror_dir(self.source, self.target)
        self.assertFalse((self.target / "old").exists())
        self.assertEqual((self.target / "hook.py").read_text(), "print('current')\n")

    def test_managed_file_permission_errors_are_not_suppressed(self):
        (self.target / "retired.py").write_text("old\n")
        with patch.object(
            Path, "unlink", side_effect=PermissionError("managed file blocked")
        ):
            with self.assertRaisesRegex(PermissionError, "managed file blocked"):
                mirror_dir(self.source, self.target)


class TestOwnedDirEntries(unittest.TestCase):
    """owned_dir_entries: the reverse pass's view of what is on disk."""

    def test_files_reported_relative_and_caches_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a.md").write_text("a\n")
            (base / "nested").mkdir()
            (base / "nested" / "b.md").write_text("b\n")
            cache = base / "__pycache__"
            cache.mkdir()
            (cache / "x.pyc").write_bytes(b"x")
            entries = owned_dir_entries(base)
            self.assertEqual(entries, {"a.md", "nested/b.md"})

    def test_per_item_reports_only_immediate_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "skill-one").mkdir()
            (base / "skill-one" / "SKILL.md").write_text("one\n")
            (base / "skill-two").mkdir()
            (base / "stray-file.md").write_text("not a skill dir\n")
            entries = owned_dir_entries(base, per_item=True)
            self.assertEqual(entries, {"skill-one", "skill-two"})

    def test_absent_directory_is_empty(self):
        self.assertEqual(owned_dir_entries(Path("/does/not/exist")), set())


class TestMirroredNames(unittest.TestCase):
    """mirrored_names: the name mapping owned_outputs must reuse."""

    def test_matches_what_mirror_dir_would_copy(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            source, target = Path(src), Path(dst)
            (source / "keep.sh").write_text("#!/bin/sh\n")
            (source / "keep.test.sh").write_text("#!/bin/sh\n")
            (source / "lib").mkdir()
            (source / "lib" / "helper.sh").write_text("#!/bin/sh\n")
            names = mirrored_names(source, exclude_patterns=["*.test.sh"])
            mirror_dir(source, target, exclude_patterns=["*.test.sh"])
            copied = {
                str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()
            }
            self.assertEqual(names, frozenset(copied))
            self.assertEqual(names, frozenset({"keep.sh", "lib/helper.sh"}))

    def test_absent_source_is_empty(self):
        self.assertEqual(mirrored_names(Path("/does/not/exist")), frozenset())


class TestSourceHookNames(unittest.TestCase):
    def test_collects_shipped_scripts_excluding_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "hooks").mkdir()
            (source / "hooks" / "guard.sh").write_text("#!/bin/sh\n")
            (source / "hooks" / "guard.test.sh").write_text("#!/bin/sh\n")
            (source / "hooks" / "hooks.json").write_text("{}")
            self.assertEqual(source_hook_names(source), {"guard.sh"})


class TestExtractScriptPaths(unittest.TestCase):
    def test_home_and_tilde_expanded(self):
        home_dir = str(home())
        self.assertEqual(
            extract_script_paths("$HOME/.codex/hooks/guard.sh"),
            [f"{home_dir}/.codex/hooks/guard.sh"],
        )
        self.assertEqual(
            extract_script_paths("~/.codex/hooks/guard.sh"),
            [f"{home_dir}/.codex/hooks/guard.sh"],
        )

    def test_repeated_path_deduplicated(self):
        cmd = "if [ -x '/a/b/guard.sh' ]; then /bin/sh '/a/b/guard.sh'; fi"
        self.assertEqual(extract_script_paths(cmd), ["/a/b/guard.sh"])

    def test_non_script_tokens_ignored(self):
        self.assertEqual(extract_script_paths("echo hi"), [])
        self.assertEqual(extract_script_paths("/bin/sh -c true"), [])

    def test_bash_prefixed_command(self):
        self.assertEqual(
            extract_script_paths("bash $HOME/.codex/hooks/x.sh"),
            [f"{home()}/.codex/hooks/x.sh"],
        )


class TestExistenceGuarded(unittest.TestCase):
    def test_guard_on_the_invoked_path_counts(self):
        cmd = "if [ -x '/a/b/guard.sh' ]; then /bin/sh '/a/b/guard.sh'; fi"
        self.assertTrue(is_existence_guarded(cmd, extract_script_paths(cmd)))

    def test_guard_on_an_unrelated_path_does_not_count(self):
        cmd = "if [ -f \"$file\" ]; then '/a/b/guard.sh' \"$file\"; fi"
        self.assertFalse(is_existence_guarded(cmd, extract_script_paths(cmd)))

    def test_unguarded_command_is_false(self):
        cmd = "/a/b/guard.sh"
        self.assertFalse(is_existence_guarded(cmd, extract_script_paths(cmd)))

    def test_negated_guard_on_the_invoked_path_counts(self):
        cmd = "[ ! -f '/a/b/hook.mjs' ] || node '/a/b/hook.mjs'"
        self.assertTrue(is_existence_guarded(cmd, extract_script_paths(cmd)))


class TestInManagedRuntimeTree(unittest.TestCase):
    def test_inside_a_managed_root(self):
        self.assertTrue(in_managed_runtime_tree("/home/x/.codex/hooks/guard.sh"))
        self.assertTrue(in_managed_runtime_tree("/home/x/.config/opencode/rules/a.md"))

    def test_outside_every_managed_root(self):
        self.assertFalse(in_managed_runtime_tree("/home/x/.orca/agent-hooks/codex-hook.sh"))


class TestClassifyRegistration(unittest.TestCase):
    """The dispositions F-17 adds: ok, mislocated, dangling, foreign."""

    def test_correctly_located_source_script_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_dir = Path(tmp) / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            cmd = f"{hooks_dir}/guard.sh"
            self.assertEqual(
                classify_registration(cmd, source_names={"guard.sh"}, hooks_dir=hooks_dir),
                "ok",
            )

    def test_source_script_from_the_wrong_managed_directory_is_mislocated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_dir = root / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            cmd = f"{root}/.claude/scripts/worktree-create.sh"
            self.assertEqual(
                classify_registration(
                    cmd, source_names={"worktree-create.sh"}, hooks_dir=hooks_dir
                ),
                "mislocated",
            )

    def test_unshipped_script_missing_in_our_own_tree_is_dangling(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_dir = Path(tmp) / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            cmd = f"{hooks_dir}/regen-surfaces.sh"
            self.assertEqual(
                classify_registration(cmd, source_names=set(), hooks_dir=hooks_dir),
                "dangling",
            )

    def test_unshipped_script_present_in_our_own_tree_is_foreign(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_dir = Path(tmp) / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            (hooks_dir / "graft.cjs").write_text("// third party\n")
            cmd = f'node "{hooks_dir}/graft.cjs" post-edit-sync'
            self.assertEqual(
                classify_registration(cmd, source_names=set(), hooks_dir=hooks_dir),
                "foreign",
            )

    def test_missing_script_outside_managed_tree_is_foreign_not_dangling(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_dir = Path(tmp) / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            cmd = "/usr/local/bin/some-other-tool.sh"
            self.assertEqual(
                classify_registration(cmd, source_names=set(), hooks_dir=hooks_dir),
                "foreign",
            )

    def test_guarded_missing_script_is_foreign_not_dangling(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_dir = Path(tmp) / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            missing = Path(tmp) / ".orca" / "agent-hooks" / "codex-hook.sh"
            cmd = f"if [ -x '{missing}' ]; then /bin/sh '{missing}'; fi"
            self.assertEqual(
                classify_registration(cmd, source_names=set(), hooks_dir=hooks_dir),
                "foreign",
            )

    def test_no_extractable_path_is_foreign(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_dir = Path(tmp) / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            self.assertEqual(
                classify_registration("echo hi", source_names=set(), hooks_dir=hooks_dir),
                "foreign",
            )

    def test_negated_guard_missing_script_is_foreign_not_dangling(self):
        """The `[ ! -f X ] || cmd X` idiom (run only if the script exists)
        is the same deliberate no-op guard as `[ -x X ]`, just phrased with
        a negated test and an `||` short-circuit. It must never classify
        dangling regardless of whether X resolves on this machine."""
        with tempfile.TemporaryDirectory() as tmp:
            hooks_dir = Path(tmp) / ".cursor" / "hooks"
            hooks_dir.mkdir(parents=True)
            missing = Path(tmp) / ".cursor" / "skills" / "impeccable" / "scripts" / "hook-before-edit.mjs"
            cmd = f"[ ! -f '{missing}' ] || node '{missing}'"
            self.assertEqual(
                classify_registration(cmd, source_names=set(), hooks_dir=hooks_dir),
                "foreign",
            )


class TestRepointCommand(unittest.TestCase):
    """F-17: a mislocated source script is repointed; nothing else is touched."""

    def test_mislocated_source_script_is_repointed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_dir = root / ".codex" / "hooks"
            hooks_dir.mkdir(parents=True)
            cmd = f"{root}/.claude/scripts/worktree-create.sh"
            result = repoint_command(
                cmd,
                source_names={"worktree-create.sh"},
                hooks_prefix="$HOME/.codex/hooks",
                hooks_dir=hooks_dir,
            )
            self.assertEqual(result, "$HOME/.codex/hooks/worktree-create.sh")

    def test_dollar_home_spelling_is_repointed(self):
        hooks_dir = home() / ".codex" / "hooks"
        cmd = "$HOME/.claude/scripts/worktree-remove.sh"
        result = repoint_command(
            cmd,
            source_names={"worktree-remove.sh"},
            hooks_prefix="$HOME/.codex/hooks",
            hooks_dir=hooks_dir,
        )
        self.assertEqual(result, "$HOME/.codex/hooks/worktree-remove.sh")

    def test_already_correct_command_is_unchanged(self):
        hooks_dir = home() / ".codex" / "hooks"
        cmd = "$HOME/.codex/hooks/guard.sh"
        result = repoint_command(
            cmd,
            source_names={"guard.sh"},
            hooks_prefix="$HOME/.codex/hooks",
            hooks_dir=hooks_dir,
        )
        self.assertEqual(result, cmd)

    def test_unshipped_name_is_left_alone(self):
        hooks_dir = home() / ".codex" / "hooks"
        cmd = "/usr/local/bin/some-other-tool.sh"
        result = repoint_command(
            cmd, source_names=set(), hooks_prefix="$HOME/.codex/hooks", hooks_dir=hooks_dir
        )
        self.assertEqual(result, cmd)

    def test_guarded_command_keeps_its_guard(self):
        """Repointing rewrites only the path; an existence guard survives."""
        home_dir = str(home())
        hooks_dir = Path(home_dir) / ".codex" / "hooks"
        stale = f"{home_dir}/.claude/scripts/worktree-create.sh"
        cmd = f"if [ -x '{stale}' ]; then '{stale}'; fi"
        result = repoint_command(
            cmd,
            source_names={"worktree-create.sh"},
            hooks_prefix="$HOME/.codex/hooks",
            hooks_dir=hooks_dir,
        )
        self.assertEqual(
            result,
            "if [ -x '$HOME/.codex/hooks/worktree-create.sh' ]; "
            "then '$HOME/.codex/hooks/worktree-create.sh'; fi",
        )


class TestOwnedDirDataclass(unittest.TestCase):
    def test_defaults(self):
        od = OwnedDir(kind="rule", path=Path("/tmp/x"), names=frozenset({"a.md"}))
        self.assertFalse(od.per_item)
        self.assertEqual(od.exclude, frozenset())


ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "stratarc" / "adapters"
SIXTH_ADAPTER = (Path(__file__).resolve().parent / "fixtures" / "adapters" / "registry" / "sixth.py").read_text()


def fixture_adapters_with_a_sixth(directory: Path) -> Path:
    """A copy of the real adapter files plus sixth.py, read only by the registry."""
    fixture = directory / "adapters"
    fixture.mkdir()
    for path in ADAPTERS_DIR.glob("*.py"):
        (fixture / path.name).write_text(path.read_text())
    (fixture / "sixth.py").write_text(SIXTH_ADAPTER)
    return fixture


class TestRuntimeRegistry(unittest.TestCase):
    """One runtime registry, declared by each adapter and listed by one function in _common.py, so a new runtime is one adapter file, not several lists."""

    def registry(self, adapters_dir=None):
        import stratarc.adapters._common as common

        return common.runtime_registry(adapters_dir)

    def test_every_adapter_file_declares_its_runtime(self):
        adapters = sorted(
            path.stem
            for path in ADAPTERS_DIR.glob("*.py")
            if not path.stem.startswith("_")
        )
        registry = self.registry()
        self.assertEqual(sorted(registry), adapters)
        for name, runtime in registry.items():
            with self.subTest(runtime=name):
                self.assertEqual(runtime.name, name)
                self.assertEqual(runtime.module, f"stratarc.adapters.{name}")

    def test_targets_and_hook_registries_match_what_sync_managed_before(self):
        registry = self.registry()
        home = Path("/fixture-home")
        self.assertEqual(
            {name: runtime.target(home) for name, runtime in registry.items()},
            {
                "claude": home / ".claude",
                "codex": home / ".codex",
                "cursor": home / ".cursor",
                "gemini": home / ".gemini",
                "opencode": home / ".config" / "opencode",
            },
        )
        self.assertEqual(
            {name: runtime.hook_registry for name, runtime in registry.items()},
            {
                "claude": "settings.json",
                "codex": "hooks.json",
                "cursor": "hooks.json",
                "gemini": "settings.json",
                "opencode": None,
            },
        )

    def test_a_sixth_adapter_file_is_listed_without_other_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.registry(fixture_adapters_with_a_sixth(Path(tmp)))
        self.assertIn("sixth", registry)
        self.assertEqual(len(registry), 6)
        self.assertEqual(registry["sixth"].target(Path("/h")), Path("/h/.sixth"))
        self.assertIn("SessionStart", registry["sixth"].hook_events)

    def test_an_adapter_without_the_constant_is_an_error_naming_the_file(self):
        import stratarc.adapters._common as common

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "seventh.py").write_text("def sync(source, target, dry_run=False):\n    return []\n")
            with self.assertRaisesRegex(common.RegistryError, "seventh.py"):
                common.runtime_registry(directory)

    def test_a_name_that_differs_from_the_file_is_an_error(self):
        import stratarc.adapters._common as common

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "eighth.py").write_text(SIXTH_ADAPTER)
            with self.assertRaisesRegex(common.RegistryError, "eighth.py"):
                common.runtime_registry(directory)

    def test_managed_runtime_roots_come_from_the_registry(self):
        import stratarc.adapters._common as common

        for runtime in self.registry().values():
            with self.subTest(runtime=runtime.name):
                self.assertTrue(
                    common.in_managed_runtime_tree(str(runtime.target(Path("/home/x")) / "hooks" / "a.sh"))
                )
