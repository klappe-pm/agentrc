"""hooks/lib/public-targets.py as an importable module for the engine.

The reader lives beside the hooks, in package data, because the deployed hooks
(prompt-capture) import it from there and run under a bare ``python3``. The
engine imports it through this module, from the installed package's data,
never from the source root being staged.
"""

from __future__ import annotations

import importlib.util
from importlib.resources import as_file

from stratarc.resources import data_dir

RESOURCE = "hooks/lib/public-targets.py"


def _load():
    with as_file(data_dir(RESOURCE)) as path:
        if not path.is_file():
            raise FileNotFoundError(
                f"public-targets reader missing at {path}; the stratarc package data is incomplete"
            )
        spec = importlib.util.spec_from_file_location("public_targets_impl", path)
        assert spec is not None and spec.loader is not None, path
        module = importlib.util.module_from_spec(spec)
        # Executed inside the as_file context, so a packed install can extract it first.
        spec.loader.exec_module(module)
    return module


_module = _load()

RELATIVE_PATH = _module.RELATIVE_PATH
PublicTargetsUnreadable = _module.PublicTargetsUnreadable
load_public_targets = _module.load_public_targets
origin_slug = _module.origin_slug
git_toplevel = _module.git_toplevel
git_common_dir = _module.git_common_dir
origin_url = _module.origin_url
checkout_identity = _module.checkout_identity
is_public_checkout = _module.is_public_checkout

__all__ = [
    "RELATIVE_PATH",
    "PublicTargetsUnreadable",
    "checkout_identity",
    "git_common_dir",
    "git_toplevel",
    "is_public_checkout",
    "load_public_targets",
    "origin_slug",
    "origin_url",
]
