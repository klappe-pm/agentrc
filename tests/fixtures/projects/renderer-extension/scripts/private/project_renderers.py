"""A sample per-project renderer extension.

A project opts in by carrying projects-root/<name>/notice.txt. The text is delivered to NOTICE.txt at the checkout root. The state the renderer returns is the digest of what it delivered, so an opt-out removes the file only while it still matches.
"""

from __future__ import annotations

from agentrc import projects

OPT_IN = "notice.txt"
TARGET = "NOTICE.txt"


def render(name, dst, dry, acts, recorded):
    source = projects.PROJECTS_SRC / name / OPT_IN
    target = dst / TARGET
    if source.is_file():
        text = source.read_text(encoding="utf-8")
        if recorded is None and target.is_file() and target.read_text(encoding="utf-8") != text:
            return None
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            acts.append(f"render notice into {TARGET}")
            if not dry:
                target.write_text(text, encoding="utf-8")
        return projects.hash_provider_entry({"text": text})
    if recorded is None or not target.is_file():
        return None
    current = {"text": target.read_text(encoding="utf-8")}
    if projects.provider_locally_edited(current, recorded):
        return recorded
    acts.append(f"remove notice from {TARGET}")
    if not dry:
        target.unlink()
    return None


def register(register_renderer):
    register_renderer("notice", render)
