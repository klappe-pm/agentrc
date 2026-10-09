"""Locate packaged data so a checkout and an installed wheel resolve it the same way."""

from importlib.resources import files
from importlib.resources.abc import Traversable


def data_dir(name: str) -> Traversable:
    """Return the Traversable for ``stratarc/data/<name>``.

    ``name`` may contain ``/`` separators, for example ``"templates/source-root"``.
    """
    node = files("stratarc") / "data"
    for part in name.split("/"):
        if part:
            node = node / part
    return node
