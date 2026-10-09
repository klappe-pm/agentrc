"""A source root's private validator checks, as tests/test_validate.py installs them under scripts/private/."""

from agentrc.validate import finding


def check_widgets(root):
    if (root / "widget.txt").exists():
        return [finding("error", "widget", "widget.txt", "a widget is present")]
    return []


VALIDATE_CHECKS = [check_widgets]
