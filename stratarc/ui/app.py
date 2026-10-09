"""The Textual application behind `stratarc ui`.

Left, the tree of source, projects and agents. Right, the selected node with its resolved value and where it came from. Below, one line of key help. It fits 80 columns by 24 rows and draws without color when `NO_COLOR` is set.

Every key calls the function in `stratarc.ui.model` that wraps the library call its command uses; the screen holds no logic of its own. Textual is imported here and nowhere else, so this module loads only when the extra is installed.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static, Tree

from stratarc.layers import LayerError
from stratarc.ui import model
from stratarc.ui.model import Editor, Node

HELP = "e edit  x explain  l log  s sync preview  v verify  q quit"

CSS = """
Screen { layout: vertical; }
#body { height: 1fr; }
#tree { width: 30; border: round $primary; }
#scroll { width: 1fr; border: round $primary; }
#help { height: 1; dock: bottom; }
Screen.mono #tree, Screen.mono #scroll { border: round white; }
Screen.mono Tree > .tree--cursor { text-style: reverse; background: transparent; color: auto; }
Screen.mono Tree > .tree--highlight { text-style: bold; background: transparent; }
Screen.mono #help { text-style: reverse; }
"""


def monochrome() -> bool:
    """True when `NO_COLOR` is set to any non-empty value, per https://no-color.org."""
    return bool(os.environ.get("NO_COLOR"))


class StrataApp(App[int]):
    CSS = CSS
    TITLE = "stratarc"
    BINDINGS = [
        Binding("e", "edit", "edit"),
        Binding("x", "explain", "explain"),
        Binding("l", "log", "log"),
        Binding("s", "sync_preview", "sync preview"),
        Binding("v", "verify", "verify"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, root: Path, *, editor: Editor = model.open_in_editor) -> None:
        super().__init__()
        self.root = Path(root)
        self.editor = editor
        self.tree_model: Node = model.build_tree(self.root)
        self.selected: Node = self.tree_model
        self.mode = "detail"
        self.text = ""

    # ----- layout -----

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Tree(self.tree_model.label, data=self.tree_model, id="tree")
            with VerticalScroll(id="scroll"):
                yield Static("", id="pane")
        yield Static(HELP, id="help")

    def on_mount(self) -> None:
        self.screen.set_class(monochrome(), "mono")
        self._fill_tree()
        self.query_one("#tree", Tree).focus()
        self._show("detail", self._safe(model.detail_text, self.selected))

    def _fill_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.clear()
        tree.root.data = self.tree_model
        tree.root.set_label(self.tree_model.label)

        def add(parent, node: Node) -> None:
            for child in node.children:
                if child.children:
                    add(parent.add(child.label, data=child), child)
                else:
                    parent.add_leaf(child.label, data=child)

        add(tree.root, self.tree_model)
        tree.root.expand_all()

    # ----- the pane -----

    def _show(self, mode: str, text: str) -> None:
        self.mode = mode
        self.text = text
        scroll = self.query_one("#scroll", VerticalScroll)
        scroll.border_title = f"{mode}: {self.selected.label}"
        self.query_one("#pane", Static).update(Text(text))

    def _safe(self, call, node: Node) -> str:
        try:
            return call(self.root, node)
        except LayerError as error:
            return f"error {error.code}  {error.message}"

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self.selected = event.node.data
            self._show("detail", self._safe(model.detail_text, self.selected))

    # ----- the keys -----

    def action_explain(self) -> None:
        self._show("explain", self._safe(model.explain_text, self.selected))

    def action_log(self) -> None:
        self._show("log", model.log_text(self.selected))

    def action_sync_preview(self) -> None:
        _code, text = model.sync_preview(self.root)
        self._show("sync preview", text)

    def action_verify(self) -> None:
        code, text = model.run_verify(self.root)
        self._show("verify", f"{text}\n\nexit {code}")

    def action_edit(self) -> None:
        try:
            target = model.owning_file(self.root, self.selected)
        except LayerError as error:
            self._show("edit", f"error {error.code}  {error.message}")
            return
        if target is None:
            self._show("edit", "No file owns this node yet.")
            return
        try:
            with self.suspend():
                status = self.editor(target)
        except Exception as error:  # the driver cannot suspend (a headless run), or the editor failed to start
            from textual.app import SuspendNotSupported

            if not isinstance(error, SuspendNotSupported):
                self._show("edit", f"The editor could not start: {error}")
                return
            status = self.editor(target)
        self.tree_model = model.build_tree(self.root)
        self._fill_tree()
        self._show("edit", f"edited {target.name}  (editor status {status})")


def run(root: Path) -> int:
    StrataApp(root).run()
    return 0
