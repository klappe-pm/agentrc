from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Static, Tree  # noqa: E402

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


def test_s_previews_a_sync_without_writing():
    before = sorted(p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file())

    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("s")
            assert app.mode == "sync preview"
            assert pane(app)

    drive(go())
    assert sorted(p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file()) == before


def test_v_runs_verify(monkeypatch):
    calls = []
    monkeypatch.setattr(model, "run_verify", lambda root: calls.append(root) or (0, "all verified"))

    async def go():
        app = StrataApp(FIXTURE)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("v")
            assert app.mode == "verify"
            assert "all verified" in pane(app) and "exit 0" in pane(app)

    drive(go())
    assert calls == [FIXTURE]


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
