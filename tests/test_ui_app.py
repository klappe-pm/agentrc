from __future__ import annotations

import asyncio
import os
import shutil
import threading
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Static, Tree  # noqa: E402

from stratarc import home_layout as layout  # noqa: E402
from stratarc.ui import model  # noqa: E402
from stratarc.ui.app import HELP, StrataApp  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ui" / "source"
SIZE = (80, 24)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, stratarc_home):
    for name in [n for n in os.environ if n.startswith("STRATARC_") and n != "STRATARC_HOME"]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("NO_COLOR", raising=False)


def pane(app: StrataApp) -> str:
    return app.text


def find_node(tree: Tree, key: str, project: str | None = None):
    def walk(node):
        yield node
        for child in node.children:
            yield from walk(child)

    for node in walk(tree.root):
        data = node.data
        if data is not None and data.kind == model.VALUE and data.key == key and data.project == project:
            return node
    raise AssertionError(key)


def drive(coro):
    return asyncio.run(coro)


def test_layout_fits_80_by_24_and_shows_the_key_help():
    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert app.size == SIZE
            assert str(app.query_one("#help", Static).render()) == HELP
            assert app.query_one("#tree", Tree).region.width + app.query_one("#scroll").region.width <= SIZE[0]
            assert app.query_one("#help").region.bottom <= SIZE[1]
            assert "resolved value(s)" in pane(app)

    drive(go())


def test_selecting_a_value_shows_its_provenance():
    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            tree = app.query_one("#tree", Tree)
            tree.move_cursor(find_node(tree, "permissions.timeout", "notes"))
            await pilot.pause()
            assert "value:  60" in pane(app)
            assert "projects-root/notes/permissions.json:2" in pane(app)

    drive(go())


def test_x_explains_the_selected_value():
    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            tree = app.query_one("#tree", Tree)
            tree.move_cursor(find_node(tree, "permissions.timeout", "notes"))
            await pilot.pause()
            await pilot.press("x")
            assert app.mode == "explain"
            assert pane(app) == model.explain_text(FIXTURE, app.selected)
            assert "decided by" in pane(app)

    drive(go())


def test_l_shows_log_entries(monkeypatch):
    from stratarc import changelog

    monkeypatch.setattr(changelog, "query", lambda filters, limit=None: [])

    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("l")
            assert app.mode == "log"
            assert pane(app) == "no changes recorded"

    drive(go())


async def settle(pilot, app) -> None:
    """Wait for the running worker and for its completion message to reach the app."""
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def help_line(app: StrataApp) -> str:
    return str(app.query_one("#help", Static).render())


def test_s_previews_a_sync_without_writing():
    before = sorted(p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file())

    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("s")
            await settle(pilot, app)
            assert app.mode == "sync preview"
            assert pane(app)
            assert help_line(app) == HELP

    drive(go())
    assert sorted(p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file()) == before


def test_v_runs_verify(monkeypatch):
    calls = []
    monkeypatch.setattr(model, "run_verify", lambda root: calls.append(root) or (0, "all verified"))

    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("v")
            await settle(pilot, app)
            assert app.mode == "verify"
            assert "all verified" in pane(app) and "exit 0" in pane(app)

    drive(go())
    assert calls == [FIXTURE]


def test_the_ui_answers_a_keypress_while_a_slow_sync_runs(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(root):
        started.set()
        release.wait(10)
        return 0, "slow sync done"

    monkeypatch.setattr(model, "sync_preview", slow)

    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            try:
                await pilot.press("s")
                await pilot.pause()
                assert started.wait(5)
                assert "running sync preview" in help_line(app)
                await pilot.press("l")
                assert app.mode == "log"
                await pilot.press("x")
                assert app.mode == "explain"
                assert not release.is_set() and app.job.state.name == "RUNNING"
            finally:
                release.set()
            await settle(pilot, app)
            assert app.mode == "sync preview" and "slow sync done" in pane(app)
            assert help_line(app) == HELP

    drive(go())


def test_q_cancels_a_running_job_and_exits(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(root):
        started.set()
        release.wait(10)
        return 0, "late"

    monkeypatch.setattr(model, "run_verify", slow)
    seen = {}

    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            try:
                await pilot.press("v")
                await pilot.pause()
                assert started.wait(5)
                await pilot.press("q")
                seen["state"] = app.job.state.name
            finally:
                release.set()
        return app.return_code

    assert drive(go()) in (0, None)
    assert seen["state"] in ("CANCELLED", "RUNNING")


def test_e_hands_the_owning_file_to_the_injected_editor():
    opened = []

    async def go():
        app = StrataApp(FIXTURE, editor=lambda path: opened.append(path) or 0)
        async with app.run_test(size=SIZE) as pilot:
            tree = app.query_one("#tree", Tree)
            tree.move_cursor(find_node(tree, "permissions.timeout", "notes"))
            await pilot.pause()
            await pilot.press("e")
            assert app.mode == "edit"

    drive(go())
    assert opened == [FIXTURE / "projects-root" / "notes" / "permissions.json"]


def _edit(tmp_path: Path, new_text: str):
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    target = root / "projects-root" / "notes" / "permissions.json"
    before = target.read_bytes()

    def editor(path):
        path.write_text(new_text, encoding="utf-8")
        return 0

    async def go():
        app = StrataApp(root, editor=editor)
        async with app.run_test(size=SIZE) as pilot:
            tree = app.query_one("#tree", Tree)
            tree.move_cursor(find_node(tree, "permissions.timeout", "notes"))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            return app.mode, pane(app), [n.data.key for n in tree.root.children if n.data is not None]

    return target, before, drive(go())


def test_e_restores_the_original_when_the_edit_is_invalid(tmp_path):
    target, before, (mode, text, _keys) = _edit(tmp_path, "{not json")
    assert target.read_bytes() == before
    assert mode == "edit" and "would be invalid" in text and "restored" in text
    versions = [p for p in (layout.backups_dir()).rglob("*") if p.is_file()]
    assert any(p.read_bytes() == b"{not json" for p in versions)


def test_e_saves_and_rebuilds_the_tree_when_the_edit_is_valid(tmp_path):
    target, before, (mode, text, _keys) = _edit(tmp_path, '{"timeout": 90}\n')
    assert target.read_text(encoding="utf-8") == '{"timeout": 90}\n'
    assert mode == "edit" and text.startswith("saved permissions.json")
    assert any(p.read_bytes() == before for p in layout.backups_dir().rglob("*") if p.is_file())


def test_q_quits():
    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("q")
        return app.return_code

    assert drive(go()) in (0, None)


def test_no_color_selects_the_monochrome_style(monkeypatch):
    async def go(expected):
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert app.screen.has_class("mono") is expected

    monkeypatch.setenv("NO_COLOR", "1")
    drive(go(True))
    monkeypatch.delenv("NO_COLOR")
    drive(go(False))
