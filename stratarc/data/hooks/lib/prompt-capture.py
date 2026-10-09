#!/usr/bin/env python3
"""Capture, refactor, and store user prompts as Markdown, SQLite, and JSON.

The Markdown note is the source of truth. The SQLite database and the JSON
index next to it are derived and regenerable with ``reindex``.

This file ships with ``hooks/prompt-capture.sh`` under ``hooks/lib/`` so every
runtime adapter (Claude Code, Codex, Gemini, Cursor) deploys it next to the
hook; the hook resolves it relative to its own directory.

Subcommands::

    capture   read one hook payload (JSON) or raw text on stdin, write a note
    reindex   rebuild SQLite and JSON from the notes, recompute related links
    query     list or search the store (SQLite backed)
    refine    optionally rewrite a note's refactored prompt with ``claude -p``

Store root resolution, first match wins: ``--root``, ``$PROMPT_CAPTURE_ROOT``,
``<git toplevel of the project>/.docs/prompts``. When there is no git
repository at all, or the toplevel is an Obsidian vault or linked worktree,
``$PROMPT_CAPTURE_FALLBACK_ROOT`` is consulted next; a note captured there
carries a ``source-cwd`` frontmatter key naming the directory it came from.
Linked worktrees default to ``~/.agent-hooks/prompts`` when no fallback is set.
Otherwise, with none of those set the prompt is skipped (reason ``no-git-repo`` or
``obsidian-vault``), never written. Captures made before the store moved from
``docs/prompts`` to ``.docs/prompts`` stay where they are; nothing here moves them.

A public target receives no capture at all: when the checkout is listed in
``<source root>/projects-root/public-targets.json`` (a JSON list of project
directory names or ``owner/repo`` slugs, or an object whose ``targets`` key
holds one) by its directory name, its main worktree's directory name or its
origin slug, as ``public-targets.py`` beside this file decides, the prompt is
skipped (reason ``public-target``) before any root, override or fallback is
consulted. The source root is ``$STRATARC_SOURCE`` when set, else
``$LLM_ROOT``; with neither set there is no list. A missing list names nobody.
A list that exists and cannot be read fails closed: no prompt is captured
anywhere (reason ``public-targets-unreadable``) until it is fixed, the same
refusal the reconciler and the project sync make.

Everything in the hook path is deterministic and stdlib-only. Secret-shaped
values are redacted before any write through ``SECRET_PATTERNS``, a Python
mirror of the ``token-shaped-values`` detector in ``hooks/lib/guard-utils.sh``
(the bash lib is not called: a subprocess per prompt hits the argv limit on
large pastes and its substitution loop is quadratic). Each match becomes
``[REDACTED:<kind>]`` per ``the captured-content-redaction rule``. The unit test
asserts every label in the lib has a mirror entry here, so the two cannot drift
silently. Telemetry carries no prompt text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

LIB_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = LIB_DIR / "prompt-capture.json"
GUARD_UTILS = LIB_DIR / "guard-utils.sh"  # parity reference only; never executed here
DEFAULT_TELEMETRY_DIR = Path.home() / ".agent-hooks" / "telemetry"
DB_NAME = "prompts.sqlite"
JSON_NAME = "prompts.json"
SCHEMA_VERSION = 1

STOPWORDS = set(
    """a an and are as at be but by can could do does for from has have how i if in
    into is it its just me my no not of on or our should so that the their them then
    there these they this to us was we were what when where which who why will with
    would you your please want need like also any all every each one two into out
    up down over under about after before again more most some such than too very
    dont don't im i'm it's thats""".split()
)

CONSTRAINT_MARKERS = re.compile(
    r"\b(must|never|do not|don't|dont|only|always|avoid|without|should not|shouldn't|no longer|cannot|can't|forbid|require[sd]?)\b",
    re.IGNORECASE,
)
IMPERATIVE_VERBS = (
    "add build capture change check clean compare create debug delete describe design "
    "document explain extract find fix generate implement include integrate investigate list "
    "make migrate move output plan produce read refactor remove rename replace research review "
    "rewrite run set show simplify store summarize support test update use validate verify wire write"
).split()
IMPERATIVE_RE = re.compile(r"^(?:please\s+)?(" + "|".join(IMPERATIVE_VERBS) + r")\b", re.IGNORECASE)
DELIVERABLE_RE = re.compile(
    r"\b(output|produce|store[sd]?|file|markdown|json|sqlite|database|report|doc|docs|table|schema|return)\b",
    re.IGNORECASE,
)
REFERENCE_RE = re.compile(r"`[^`\n]+`|https?://\S+|(?<![\w.])(?:~|\.{0,2}/)?[\w.-]+(?:/[\w.()-]+)+/?")
SLASH_ONLY = re.compile(r"^/[\w-]+\s*$")
# The leading /name token, when the prompt opens with a slash command, bare
# or not ("/commit" and "/commit fix the staging path" both yield "commit").
# This is the source for the telemetry capability field (WI-16/F-21): the
# skill, agent, or command in effect, when the payload exposes one.
LEADING_CAPABILITY_RE = re.compile(r"^/([A-Za-z][\w-]*)")
# A turn the harness injects rather than one the user typed: a queued task
# notification, a wake event, a system reminder, a relayed webhook payload.
# UserPromptSubmit fires for these exactly as it does for a real prompt, so
# without this every background wake writes a note whose original-prompt block
# is an XML envelope, and the refactor step then dresses that envelope up as a
# role-and-goal prompt. The store is for what the user asked, so they are
# skipped. Matching is anchored at the start: a prompt that merely quotes one
# of these tags further down is still the user's own writing and is kept.
HARNESS_ENVELOPE = re.compile(
    r"^<(?:task-notification|wake\b|system-reminder|event\s+source=|webhook-payload|untrusted_external_data)",
    re.IGNORECASE,
)

ROLE_BY_CATEGORY = {
    "debugging": "software engineer who isolates root causes before changing code",
    "code-review": "senior reviewer who reads diffs adversarially for correctness and simplicity",
    "git-and-shipping": "release engineer who keeps history clean and every push validated",
    "testing": "test engineer who proves behavior with failing-first tests",
    "planning": "principal product manager and technical planner",
    "research": "technical researcher who cites sources and separates fact from inference",
    "documentation": "technical writer who leads with the answer and writes in plain prose",
    "data-and-analysis": "data engineer and analyst fluent in SQL and experimentation",
    "harness-and-hooks": "Claude Code harness engineer who knows hooks, skills, and settings",
    "refactoring": "engineer who makes surgical, behavior-preserving changes",
    "feature-work": "software engineer who ships complete, tested features",
    "questions": "expert advisor who answers directly and states assumptions",
    "uncategorized": "expert assistant for developer tooling and product work",
}

# Mirror of hooks/lib/guard-utils.sh, keyed by the lib's own labels. Every
# pattern is anchored so a prefix inside an ordinary word (desk-notification,
# tokenizer) is not a match, and the generic assignment shape is bounded so
# matching stays linear on long word runs. url_credentials is an addition the
# bash lib does not carry: a user:pass@host URL is a secret the note must not keep.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    "aws_access_key": re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])", re.IGNORECASE),
    "github_fine_grained_pat": re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}", re.IGNORECASE),
    "github_token": re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}", re.IGNORECASE),
    "slack_token": re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}", re.IGNORECASE),
    "sk_style_key": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}", re.IGNORECASE),
    "google_api_key": re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{20,}", re.IGNORECASE),
    "jwt": re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}", re.IGNORECASE),
    "bearer_token": re.compile(r"Bearer[ \t]+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    # F-22:
    # a quoted value is a literal in any shape. An unquoted value must not be
    # immediately followed by "(" (a function call: extract_keyspace(info))
    # and must not be a bare dotted chain of identifiers (a code reference:
    # api.env.INTERNAL_TOKEN), matching the boundary and shape checks
    # hooks/lib/guard-utils.sh applies without lookahead support. A value that
    # opens with // is the authority of a secret://namespace/key reference
    # (the secret-resolution rule), which the lib exempts in
    # _ss_generic_assignment_is_excluded; review cluster F2 found the mirror
    # redacting every such reference the canonical detector passes.
    "generic_assignment": re.compile(
        r"(?<![A-Za-z0-9_])[A-Za-z0-9_]{0,64}?(KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]{0,64}[ \t]*[=:][ \t]*"
        r"(?!//)"
        r"(?:"
        r"['\"][A-Za-z0-9_./+=-]{16,}['\"]"
        r"|"
        r"(?!(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*(?:[ \t'\",;)\]}]|$))"
        r"[A-Za-z0-9_./+=-]{16,}(?=[ \t'\",;)\]}]|$)"
        r")",
        re.IGNORECASE,
    ),
    "url_credentials": re.compile(r"(?<![A-Za-z0-9])[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
}
CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


# --------------------------------------------------------------------------
# configuration and environment
# --------------------------------------------------------------------------


def load_config(path: Path | None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("min_prompt_chars", 20)
    data.setdefault("related_limit", 5)
    data.setdefault("related_threshold", 0.12)
    data.setdefault("skip_prefixes", [])
    data.setdefault("categories", [])
    return data


def git_toplevel(directory: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    top = out.stdout.strip()
    return Path(top) if top else None


def resolve_project(directory: Path) -> tuple[str, Path | None]:
    """Return (project name, git toplevel): project.yml name, else the repo directory name."""
    top = git_toplevel(directory)
    base = top or directory
    project_yml = base / "project.yml"
    if project_yml.is_file():
        try:
            match = re.search(r"^name:[ \t]*(\S.*)$", project_yml.read_text(encoding="utf-8"), re.MULTILINE)
        except OSError:
            match = None
        if match:
            return match.group(1).strip().strip("'\""), top
    return base.name, top


def is_obsidian_vault(toplevel: Path | None) -> bool:
    """A vault's folder structure is canonical (the obsidian-vault-writes rule),
    so the store is never created inside one without an explicit root."""
    return toplevel is not None and (toplevel / ".obsidian").is_dir()


def is_linked_worktree(toplevel: Path | None) -> bool:
    """Distinguish linked worktrees from primary checkouts with a gitfile."""
    if toplevel is None or not (toplevel / ".git").is_file():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(toplevel), "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # Keep captures outside a checkout whose type cannot be verified.
    paths = result.stdout.splitlines()
    if result.returncode != 0 or len(paths) != 2:
        return True
    return (toplevel / paths[0]).resolve() != (toplevel / paths[1]).resolve()


def source_root() -> Path | None:
    """The source checkout: ``$STRATARC_SOURCE``, else ``$LLM_ROOT``, else None (no file-location fallback)."""
    override = os.environ.get("STRATARC_SOURCE") or os.environ.get("LLM_ROOT")
    return Path(override).expanduser() if override else None


class PublicTargetsUnreadable(Exception):
    """The public-target list exists and cannot be trusted, or the module that reads it is missing."""


_PUBLIC_TARGETS_MODULE = None


def _public_targets_module():
    """``public-targets.py`` beside this file, loaded once; None when it is missing or broken."""
    global _PUBLIC_TARGETS_MODULE
    if _PUBLIC_TARGETS_MODULE is not None:
        return _PUBLIC_TARGETS_MODULE
    import importlib.util

    spec = importlib.util.spec_from_file_location("public_targets", LIB_DIR / "public-targets.py")
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, SyntaxError):
        return None
    _PUBLIC_TARGETS_MODULE = module
    return module


def public_targets(root: Path | None = None) -> frozenset[str]:
    """The entries of ``projects-root/public-targets.json`` under the source root; empty when the file is absent.

    Raises PublicTargetsUnreadable when the file exists and cannot be read or
    has another shape, or when ``public-targets.py`` is not beside this file,
    so the caller fails closed instead of capturing into a public checkout.
    """
    module = _public_targets_module()
    if module is None:
        raise PublicTargetsUnreadable("public-targets.py is missing beside prompt-capture.py")
    resolved = root or source_root()
    if resolved is None:
        return frozenset()
    try:
        return module.load_public_targets(resolved)
    except module.PublicTargetsUnreadable as error:
        raise PublicTargetsUnreadable(str(error)) from None


def is_public_target(toplevel: Path | None, root: Path | None = None) -> bool:
    """True when the checkout is a public repository that must receive no capture.

    The list is read before the checkout is looked at, so an unreadable list
    raises for every prompt, not only the ones inside a repository.
    """
    targets = public_targets(root)
    if toplevel is None or not targets:
        return False
    module = _public_targets_module()
    return module is not None and module.is_public_checkout(toplevel, targets)


def resolve_root(explicit: str | None, toplevel: Path | None, *, vault: bool = False,
                 linked: bool | None = None) -> Path | None:
    """None means there is nowhere safe to write.

    First match wins: ``--root``, ``$PROMPT_CAPTURE_ROOT``, the project's own
    ``.docs/prompts`` (only for a primary checkout outside an Obsidian vault),
    then ``$PROMPT_CAPTURE_FALLBACK_ROOT`` for other sessions. A linked
    worktree defaults to ``~/.agent-hooks/prompts`` when that variable is unset.
    The fallback lives in a
    variable separate from ``PROMPT_CAPTURE_ROOT`` on purpose (F-31, WI-21):
    setting it globally must not reroute every repository session into one
    shared store, only the sessions that would otherwise have nowhere safe to
    write.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("PROMPT_CAPTURE_ROOT")
    if env:
        return Path(env).expanduser()
    if linked is None:
        linked = is_linked_worktree(toplevel)
    if toplevel is not None and not vault and not linked:
        return toplevel / ".docs" / "prompts"
    fallback = os.environ.get("PROMPT_CAPTURE_FALLBACK_ROOT")
    if fallback:
        return Path(fallback).expanduser()
    if linked:
        return Path.home() / ".agent-hooks" / "prompts"
    return None


# --------------------------------------------------------------------------
# text handling
# --------------------------------------------------------------------------


def extract_prompt(payload: dict[str, Any]) -> str:
    candidates: list[Any] = [payload.get("prompt"), payload.get("user_prompt"), payload.get("message"), payload.get("input")]
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        candidates.extend([tool_input.get("prompt"), tool_input.get("message"), tool_input.get("input")])
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def redact(text: str) -> str:
    """Replace every token-shaped value with ``[REDACTED:<kind>]``."""
    text = text.replace("\x00", "")
    for kind, pattern in SECRET_PATTERNS.items():
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


def safe_label(value: str, fallback: str) -> str:
    """Category and runtime become directory names and bare YAML scalars."""
    value = str(value or "").strip().lower()
    return value if CATEGORY_RE.match(value) else fallback


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def prompt_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def skip_reason(text: str, cfg: dict[str, Any]) -> str | None:
    stripped = text.strip()
    if not stripped:
        return "empty"
    if SLASH_ONLY.match(stripped):
        return "bare-slash-command"
    if HARNESS_ENVELOPE.match(stripped):
        return "harness-envelope"
    lowered = stripped.lower()
    for prefix in cfg.get("skip_prefixes", []):
        if lowered.startswith(str(prefix).lower()):
            return "skip-prefix"
    if len(stripped) < int(cfg.get("min_prompt_chars", 20)):
        return "too-short"
    return None


def strip_leading_command(text: str) -> str:
    """Drop a leading slash command token so the title reads as prose."""
    return re.sub(r"^\s*/[\w-]+\s*", "", text, count=1)


def leading_capability(text: str) -> str:
    """The prompt's leading /name token, or "" when it does not open with one."""
    match = LEADING_CAPABILITY_RE.match(text.strip())
    return match.group(1) if match else ""


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!;])\s+|\n+|\s+(?=\d+[.)]\s)", text)
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"^\s*(?:\d+[.)]|[-*])\s+", "", part)
        if part:
            out.append(part)
    return out


def classify(text: str, cfg: dict[str, Any]) -> str:
    lowered = normalize(text)
    best_name = "uncategorized"
    best_score = 0
    for entry in cfg.get("categories", []):
        name = safe_label(str(entry.get("name", "")), "")
        score = 0
        for raw in entry.get("keywords", []):
            keyword = str(raw).lower().strip()
            if not keyword:
                continue
            if keyword == "?":
                score += lowered.count("?")
            elif " " in keyword or not keyword.isalnum():
                score += 2 * lowered.count(keyword)
            else:
                score += len(re.findall(r"\b" + re.escape(keyword) + r"\b", lowered))
        if name and score > best_score:
            best_name, best_score = name, score
    return best_name


def make_title(text: str) -> str:
    body = strip_leading_command(text)
    prose = re.sub(r"`{3,}.*?(?:`{3,}|$)", " ", body, flags=re.DOTALL).strip() or body
    first = sentences(prose)
    candidate = first[0] if first else prose.strip()
    candidate = re.sub(r"^(?:[#>*-]+|\[\[|\d+[.)])\s*", "", candidate.strip())
    candidate = re.sub(r"\s+", " ", candidate).strip().rstrip(".:;,")
    if len(candidate) > 80:
        cut = candidate[:80]
        candidate = cut[: cut.rfind(" ")] if " " in cut else cut
    candidate = candidate.strip("`").rstrip(".:;,")
    return candidate or "untitled prompt"


def slugify(title: str, digest: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > 60:
        slug = slug[:60]
        slug = slug[: slug.rfind("-")] if "-" in slug else slug
    slug = slug.strip("-") or "prompt"
    return f"{slug}-{digest[:6]}"


def refactor(text: str, category: str) -> dict[str, list[str] | str]:
    """Deterministically split a raw prompt into structured sections."""
    body = strip_leading_command(text)
    tasks: list[str] = []
    constraints: list[str] = []
    questions: list[str] = []
    deliverables: list[str] = []
    context: list[str] = []
    for sentence in sentences(body):
        if sentence.endswith("?"):
            questions.append(sentence)
        elif CONSTRAINT_MARKERS.search(sentence):
            constraints.append(sentence)
        elif IMPERATIVE_RE.match(sentence):
            tasks.append(sentence)
        elif DELIVERABLE_RE.search(sentence):
            deliverables.append(sentence)
        else:
            context.append(sentence)
    references: list[str] = []
    for match in REFERENCE_RE.findall(body):
        token = match.strip("`").rstrip(".,;:)").rstrip("{}/")
        if token and token not in references and len(token) > 2:
            references.append(token)
    goal = tasks[0] if tasks else (deliverables[0] if deliverables else (context[0] if context else (questions[0] if questions else body.strip())))
    return {
        "role": ROLE_BY_CATEGORY.get(category, ROLE_BY_CATEGORY["uncategorized"]),
        "goal": goal,
        "tasks": tasks,
        "constraints": constraints,
        "questions": questions,
        "deliverables": deliverables,
        "references": references,
        "context": context,
    }


def render_refactored(parts: dict[str, Any]) -> str:
    role = str(parts["role"])
    lead = "You are an " if role.startswith("expert") else "You are an expert "
    lines = ["# Role", f"{lead}{role}.", "", "# Goal", str(parts["goal"]), ""]
    if parts["context"]:
        lines += ["# Context"] + [f"- {s}" for s in parts["context"]] + [""]
    if parts["tasks"]:
        lines += ["# Tasks"] + [f"{i}. {s}" for i, s in enumerate(parts["tasks"], start=1)] + [""]
    if parts["constraints"]:
        lines += ["# Constraints"] + [f"- {s}" for s in parts["constraints"]] + [""]
    if parts["deliverables"]:
        lines += ["# Deliverables"] + [f"- {s}" for s in parts["deliverables"]] + [""]
    if parts["references"]:
        lines += ["# References"] + [f"- `{s}`" for s in parts["references"]] + [""]
    if parts["questions"]:
        lines += ["# Questions to answer"] + [f"- {s}" for s in parts["questions"]] + [""]
    lines += [
        "# Success criteria",
        "- Every task above is complete and verified, not just described.",
        "- Every question above gets a direct answer, with sources when it depends on external facts.",
        "- State assumptions explicitly instead of guessing at ambiguous scope.",
    ]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# similarity
# --------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9'-]{1,}", text.lower()) if t not in STOPWORDS and len(t) >= 3]


def tfidf_vectors(docs: dict[str, str]) -> dict[str, dict[str, float]]:
    counts = {key: Counter(tokens(text)) for key, text in docs.items()}
    df: Counter[str] = Counter()
    for counter in counts.values():
        df.update(counter.keys())
    n = max(len(docs), 1)
    vectors: dict[str, dict[str, float]] = {}
    for key, counter in counts.items():
        total = sum(counter.values()) or 1
        vec = {term: (c / total) * math.log(1.0 + n / df[term]) for term, c in counter.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors[key] = {term: v / norm for term, v in vec.items()}
    return vectors


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def related_for(target: str, vectors: dict[str, dict[str, float]], limit: int, threshold: float) -> list[tuple[str, float]]:
    base = vectors.get(target, {})
    scored = []
    for key, vec in vectors.items():
        if key == target:
            continue
        score = cosine(base, vec)
        if score >= threshold:
            scored.append((key, round(score, 4)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]


# --------------------------------------------------------------------------
# markdown notes
# --------------------------------------------------------------------------


def yaml_list(key: str, values: Iterable[str]) -> list[str]:
    """Deduped, case-insensitively sorted; an empty array renders as ``key: []``."""
    items = sorted({v for v in values if v}, key=str.lower)
    if not items:
        return [f"{key}: []"]
    return [f"{key}:"] + [f"  - {yaml_scalar(v)}" for v in items]


def yaml_scalar(value: str) -> str:
    """Bare only for kebab identifiers; everything else is JSON-quoted so YAML
    never reinterprets a title such as ``true``, ``2026-09-07`` or ``123``."""
    if re.fullmatch(r"[a-z][a-z0-9-]*", value) and value not in {"true", "false", "null", "yes", "no", "on", "off"}:
        return value
    return json.dumps(value, ensure_ascii=False)


def fence_for(*blocks: str) -> str:
    """A backtick fence one longer than the longest run inside the content, so
    a prompt that contains fences of its own cannot close ours. Never below 4:
    the audit's fence stripper matches triple backticks, and a longer outer
    fence keeps prompt-borne ``#`` lines out of its heading scan."""
    longest = 3
    for block in blocks:
        for run in re.findall(r"`{3,}", block):
            longest = max(longest, len(run))
    return "`" * (longest + 1)


# Provider behind each runtime label, for the `providers` provenance key
# (the docs-provenance-frontmatter rule). A runtime that fronts many providers
# (cursor, opencode) maps to nothing, and the key renders as an empty array.
RUNTIME_PROVIDERS: dict[str, list[str]] = {
    "claude": ["Anthropic"],
    "codex": ["OpenAI"],
    "gemini": ["Google"],
}


def render_note(note: dict[str, Any]) -> str:
    front = [
        "---",
        f"domain: {yaml_scalar(note['project'])}",
        f"category: {note['category']}",
        f"sub-category: {note['runtime']}",
    ]
    front += yaml_list("topics", [])
    front += yaml_list("types", ["prompt"])
    front += [
        f"date-created: {note['created_at'][:10]}",
        f"date-revised: {note['revised_at'][:10]}",
        "status: DRAFT",
    ]
    front += yaml_list("aliases", [note["title"]])
    front += yaml_list("tags", ["prompt", note["category"], note["runtime"]] + list(note.get("tags", [])))
    # File-specific keys after tags, sorted A to Z. The three provenance keys
    # (models, providers, session-link) are required by
    # the docs-provenance-frontmatter rule; the hook payload carries a session
    # id but no model id and no web link, so models is empty and session-link
    # is the empty spelling. providers follows from the runtime label.
    # source-cwd is conditional (F-31, WI-21): it appears only on a note
    # captured through PROMPT_CAPTURE_FALLBACK_ROOT, since that store is not
    # scoped to one project and needs the originating directory recorded to
    # stay attributable. Sorted between session-link and word-count.
    front += [f"captured-at: {note['created_at']}"]
    front += yaml_list("models", [])
    front += [f"prompt-hash: {note['id']}"]
    front += yaml_list("providers", RUNTIME_PROVIDERS.get(note["runtime"], []))
    front += yaml_list("related", [r["slug"] for r in note.get("related", [])])
    front += [
        f"session-id: {yaml_scalar(note['session_id'] or 'unknown')}",
        'session-link: ""',
    ]
    source_cwd = note.get("source_cwd") or ""
    if source_cwd:
        front += [f"source-cwd: {yaml_scalar(source_cwd)}"]
    front += [
        f"word-count: {note['word_count']}",
        "---",
        "",
    ]
    fence = fence_for(note["refactored"], note["original"])
    # The body carries the two prompts and nothing else: the refactored prompt
    # first because it is the one worth reusing, the verbatim original last as
    # the record of what was actually typed. A generated summary restated the
    # title, and the related list duplicated the `related` frontmatter key that
    # parse_note already reads, so both were noise between the two prompts.
    body = [
        f"# {note['slug']}",
        "",
        "## refactored-prompt",
        "",
        fence + "markdown",
        note["refactored"].rstrip("\n"),
        fence,
        "",
        "## original-prompt",
        "",
        fence + "markdown",
        note["original"].rstrip("\n"),
        fence,
        "",
    ]
    return "\n".join(front + body)


FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
FENCE_RE = re.compile(r"^## (refactored-prompt|original-prompt)\n\n(`{3,})(?:markdown|text)\n(.*?)\n\2$", re.DOTALL | re.MULTILINE)


def parse_note(path: Path) -> dict[str, Any] | None:
    """Return the note's fields, or None when the file is not a readable prompt
    note. Notes are hand-editable, so a malformed one is skipped rather than
    allowed to break every later capture in the project."""
    try:
        return _parse_note(path)
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, IndexError):
        return None


def _parse_note(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    match = FRONT_RE.match(text)
    if not match:
        return None
    front: dict[str, Any] = {}
    current_list: str | None = None
    for line in match.group(1).splitlines():
        if line.startswith("  - ") and current_list:
            front.setdefault(current_list, []).append(_unquote(line[4:].strip()))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value == "" or value == "[]":
            current_list = key if value == "" else None
            front.setdefault(key, [])
        else:
            current_list = None
            front[key] = _unquote(value)
    if "prompt" not in (front.get("types") or []) or not front.get("prompt-hash"):
        return None
    sections = {name: body for name, _fence, body in FENCE_RE.findall(text)}
    original = sections.get("original-prompt", "")
    aliases = front.get("aliases") or []
    try:
        word_count = int(str(front.get("word-count", "0")).strip() or 0)
    except ValueError:
        word_count = len(original.split())
    return {
        "id": str(front["prompt-hash"]),
        "slug": path.stem,
        "path": path,
        "title": aliases[0] if aliases else path.stem,
        "category": safe_label(str(front.get("category", path.parent.name)), path.parent.name),
        "runtime": safe_label(str(front.get("sub-category", "unknown")), "unknown"),
        "project": str(front.get("domain", "unknown")),
        "session_id": str(front.get("session-id", "unknown")),
        "created_at": str(front.get("captured-at", front.get("date-created", ""))),
        "revised_at": str(front.get("date-revised", "")),
        "source_cwd": str(front.get("source-cwd", "")),
        "word_count": word_count,
        "tags": [t for t in (front.get("tags") or []) if t not in {"prompt", front.get("category"), front.get("sub-category")}],
        "related_slugs": list(front.get("related") or []),
        "original": original,
        "refactored": sections.get("refactored-prompt", ""),
    }


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def load_notes(root: Path) -> list[dict[str, Any]]:
    notes = []
    if not root.is_dir():
        return notes
    for path in sorted(root.glob("*/*.md")):
        if path.name.upper() == "README.MD":
            continue
        note = parse_note(path)
        if note:
            notes.append(note)
    return notes


# --------------------------------------------------------------------------
# sqlite and json
# --------------------------------------------------------------------------


def open_db(root: Path, db_path: Path | None = None) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path or (root / DB_NAME))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompts (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            runtime TEXT NOT NULL,
            project TEXT NOT NULL,
            session_id TEXT,
            created_at TEXT NOT NULL,
            revised_at TEXT NOT NULL,
            word_count INTEGER NOT NULL,
            path TEXT NOT NULL,
            tags TEXT NOT NULL,
            original TEXT NOT NULL,
            refactored TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prompt_links (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            score REAL NOT NULL,
            PRIMARY KEY (source_id, target_id)
        );
        CREATE INDEX IF NOT EXISTS prompts_category ON prompts(category);
        CREATE INDEX IF NOT EXISTS prompts_created ON prompts(created_at);
        """
    )
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(id UNINDEXED, title, original, refactored)")
    except sqlite3.OperationalError:
        pass
    return conn


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prompts_fts'").fetchone()
    return row is not None


def upsert_note(conn: sqlite3.Connection, note: dict[str, Any], root: Path) -> None:
    rel_path = os.path.relpath(note["path"], root)
    conn.execute(
        """
        INSERT OR REPLACE INTO prompts
        (id, slug, title, category, runtime, project, session_id, created_at, revised_at, word_count, path, tags, original, refactored)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note["id"],
            note["slug"],
            note["title"],
            note["category"],
            note["runtime"],
            note["project"],
            note["session_id"],
            note["created_at"],
            note["revised_at"],
            note["word_count"],
            rel_path,
            json.dumps(sorted(note.get("tags", []))),
            note["original"],
            note["refactored"],
        ),
    )
    if has_fts(conn):
        conn.execute("DELETE FROM prompts_fts WHERE id = ?", (note["id"],))
        conn.execute(
            "INSERT INTO prompts_fts (id, title, original, refactored) VALUES (?, ?, ?, ?)",
            (note["id"], note["title"], note["original"], note["refactored"]),
        )


def replace_links(conn: sqlite3.Connection, source_id: str, related: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM prompt_links WHERE source_id = ?", (source_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO prompt_links (source_id, target_id, score) VALUES (?, ?, ?)",
        [(source_id, r["id"], r["score"]) for r in related],
    )


def export_json(conn: sqlite3.Connection, root: Path) -> Path:
    rows = conn.execute("SELECT * FROM prompts ORDER BY created_at, slug").fetchall()
    columns = [d[0] for d in conn.execute("SELECT * FROM prompts LIMIT 0").description]
    links: dict[str, list[dict[str, Any]]] = {}
    slug_by_id = {row[0]: row[1] for row in rows}
    for source, target, score in conn.execute("SELECT source_id, target_id, score FROM prompt_links ORDER BY source_id, score DESC"):
        links.setdefault(source, []).append({"id": target, "slug": slug_by_id.get(target, ""), "score": score})
    prompts = []
    for row in rows:
        record = dict(zip(columns, row))
        record["tags"] = json.loads(record["tags"])
        record["related"] = links.get(record["id"], [])
        prompts.append(record)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "count": len(prompts),
        "categories": sorted({p["category"] for p in prompts}),
        "prompts": prompts,
    }
    out = root / JSON_NAME
    out.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# telemetry
# --------------------------------------------------------------------------


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def payload_session_id(payload: dict[str, Any]) -> str:
    """The session a prompt belongs to, or "unknown".

    This is the join key between a captured prompt and the transcript that
    recorded what happened to it. Without it the prompt store sits beside the
    transcripts with no way to line the two up.
    """
    value = payload.get("session_id") if isinstance(payload, dict) else None
    return str(value or os.environ.get("CLAUDE_SESSION_ID") or "unknown")


def payload_prompt_ids(payload: dict[str, Any]) -> dict[str, str]:
    """The payload's own identifiers for this prompt, empty when absent.

    Claude Code's UserPromptSubmit payload documents `message_id` as the UUID
    of the message in the conversation, and carries `prompt_id` beside it.
    Whether `message_id` equals the transcript record's `uuid` is not
    documented, so this records both as given and claims no join it has not
    verified.
    """
    ids = {}
    for key in ("message_id", "prompt_id"):
        value = payload.get(key) if isinstance(payload, dict) else None
        ids[key] = value if isinstance(value, str) else ""
    return ids


def emit_telemetry(
    project: str,
    event: str,
    metadata: dict[str, Any],
    session_id: str = "unknown",
    capability: str = "",
    prompt_ids: dict[str, str] | None = None,
) -> None:
    if os.environ.get("RUNTIME_TELEMETRY_DISABLE") == "1":
        return
    tdir = Path(os.environ.get("PROMPT_CAPTURE_TELEMETRY_DIR") or DEFAULT_TELEMETRY_DIR)
    try:
        tdir.mkdir(parents=True, exist_ok=True)
        # session_id sits at the top level rather than inside metadata because
        # it is the join key, not a detail of one event kind. capability sits
        # beside it for the same reason: the skill, agent, or command in
        # effect is a fact about the turn, not a detail of one event kind.
        record = {
            "timestamp": now_iso(),
            "source": "prompt-capture",
            "project": project,
            "event": event,
            "session_id": session_id or "unknown",
            "capability": capability or "",
            "metadata": metadata,
            # The finer join key: which record in the session this prompt is.
            **(prompt_ids or payload_prompt_ids({})),
        }
        with (tdir / f"prompt-capture-{dt.date.today().isoformat()}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_capture(args: argparse.Namespace) -> int:
    """Hook entry point. Never raises: a failure is one telemetry event and a
    non-zero exit that the hook wrapper logs, never a blocked prompt."""
    try:
        return _capture(args)
    except Exception as exc:  # noqa: BLE001 - the hook must fail closed on the prompt, open on the write
        emit_telemetry("unknown", "prompt-failed", {"runtime": args.runtime, "error": type(exc).__name__})
        print(f"prompt-capture: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _capture(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    runtime = safe_label(args.runtime, "unknown")
    raw = sys.stdin.read().replace("\x00", "")
    payload: dict[str, Any] = {}
    if args.stdin_text:
        prompt = raw
    else:
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            emit_telemetry("unknown", "prompt-skipped", {"reason": "malformed-payload", "runtime": runtime})
            return 0
        if not isinstance(payload, dict):
            payload = {}
        prompt = extract_prompt(payload)
    # Resolved before the first skip so every event carries the join key, not
    # only the ones that reach the end of the function.
    session_id = payload_session_id(payload)
    prompt_ids = payload_prompt_ids(payload)
    capability = leading_capability(prompt)
    cwd = payload.get("cwd")
    project_dir = Path(args.project or (cwd if isinstance(cwd, str) and cwd else "") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).expanduser()
    project, toplevel = resolve_project(project_dir)
    reason = skip_reason(prompt, cfg)
    if reason:
        emit_telemetry(project, "prompt-skipped", {"reason": reason, "runtime": runtime}, session_id, capability, prompt_ids)
        return 0
    # A public repository receives no capture at all, so this is decided
    # before any root: an explicit --root or PROMPT_CAPTURE_ROOT does not
    # reroute the prompt, it is dropped. A list that cannot be read drops
    # every prompt the same way, since reading it as empty would capture
    # into the checkout it protects. Telemetry carries no prompt text.
    try:
        public = is_public_target(toplevel)
    except PublicTargetsUnreadable as error:
        emit_telemetry(
            project,
            "prompt-skipped",
            {"reason": "public-targets-unreadable", "runtime": runtime, "error": type(error).__name__},
            session_id,
            capability,
            prompt_ids,
        )
        return 0
    if public:
        emit_telemetry(project, "prompt-skipped", {"reason": "public-target", "runtime": runtime}, session_id, capability, prompt_ids)
        return 0
    vault = is_obsidian_vault(toplevel)
    explicit_override = bool(args.root or os.environ.get("PROMPT_CAPTURE_ROOT"))
    linked = is_linked_worktree(toplevel)
    root = resolve_root(args.root, toplevel, vault=vault, linked=linked)
    if root is None:
        skip = "no-git-repo" if toplevel is None else "obsidian-vault"
        emit_telemetry(project, "prompt-skipped", {"reason": skip, "runtime": runtime}, session_id, capability, prompt_ids)
        return 0
    # root came from PROMPT_CAPTURE_FALLBACK_ROOT rather than an explicit
    # override or the project's own store (F-31, WI-21): this capture is no
    # longer a skip, but the note is not scoped to one project, so it carries
    # the directory it came from.
    used_fallback = not explicit_override and (toplevel is None or vault or linked)
    prompt = redact(prompt)
    digest = prompt_hash(prompt)
    existing = load_notes(root)
    if any(n["id"] == digest for n in existing):
        emit_telemetry(project, "prompt-skipped", {"reason": "duplicate", "runtime": runtime, "hash": digest[:12]}, session_id, capability, prompt_ids)
        return 0
    category = classify(prompt, cfg)
    title = make_title(prompt)
    slug = slugify(title, digest)
    parts = refactor(prompt, category)
    timestamp = now_iso()
    note = {
        "id": digest,
        "slug": slug,
        "title": title,
        "category": category,
        "runtime": runtime,
        "project": project,
        "session_id": session_id,
        "created_at": timestamp,
        "revised_at": timestamp,
        "word_count": len(prompt.split()),
        "source_cwd": str(project_dir) if used_fallback else "",
        "tags": [],
        "original": prompt,
        "refactored": render_refactored(parts),
        "path": root / category / f"{slug}.md",
    }
    docs = {n["id"]: n["original"] for n in existing}
    docs[digest] = prompt
    vectors = tfidf_vectors(docs)
    by_id = {n["id"]: n for n in existing}
    note["related"] = [
        {"id": rid, "slug": by_id[rid]["slug"], "category": by_id[rid]["category"], "score": score}
        for rid, score in related_for(digest, vectors, int(cfg["related_limit"]), float(cfg["related_threshold"]))
        if rid in by_id
    ]
    note["path"].parent.mkdir(parents=True, exist_ok=True)
    note["path"].write_text(render_note(note), encoding="utf-8")
    conn = open_db(root)
    with conn:
        upsert_note(conn, note, root)
        replace_links(conn, digest, note["related"])
    export_json(conn, root)
    conn.close()
    emit_telemetry(
        project,
        "prompt-captured",
        {"category": category, "runtime": runtime, "hash": digest[:12], "word_count": note["word_count"], "related": len(note["related"])},
        session_id,
        capability,
        prompt_ids,
    )
    if not args.quiet:
        print(note["path"])
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    project, toplevel = resolve_project(Path(args.project or os.getcwd()).expanduser())
    root = resolve_root(args.root, toplevel)
    if root is None:
        print("no git repository here and no --root or PROMPT_CAPTURE_ROOT given", file=sys.stderr)
        return 1
    notes = load_notes(root)
    db_path = root / DB_NAME
    if not notes:
        if not args.quiet:
            print(f"no prompt notes under {root}; nothing to reindex")
        return 0
    tmp_path = root / (DB_NAME + ".rebuild")
    if tmp_path.exists():
        tmp_path.unlink()
    conn = open_db(root, tmp_path)
    vectors = tfidf_vectors({n["id"]: n["original"] for n in notes})
    by_id = {n["id"]: n for n in notes}
    with conn:
        for note in notes:
            related = [
                {"id": rid, "slug": by_id[rid]["slug"], "category": by_id[rid]["category"], "score": score}
                for rid, score in related_for(note["id"], vectors, int(cfg["related_limit"]), float(cfg["related_threshold"]))
            ]
            note["related"] = related
            if sorted(r["slug"] for r in related) != sorted(note["related_slugs"]):
                note["revised_at"] = now_iso()
                note["path"].write_text(render_note(note), encoding="utf-8")
            upsert_note(conn, note, root)
            replace_links(conn, note["id"], related)
    out = export_json(conn, root)
    conn.close()
    os.replace(tmp_path, db_path)
    if not args.quiet:
        print(f"reindexed {len(notes)} prompt(s) into {db_path} and {out}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    _, toplevel = resolve_project(Path(args.project or os.getcwd()).expanduser())
    root = resolve_root(args.root, toplevel)
    if root is None or not (root / DB_NAME).exists():
        print(f"no database at {root / DB_NAME if root else '<no store root>'}; run reindex first", file=sys.stderr)
        return 1
    conn = open_db(root)
    where: list[str] = []
    params: list[Any] = []
    if args.category:
        where.append("category = ?")
        params.append(args.category)
    if args.runtime:
        where.append("runtime = ?")
        params.append(args.runtime)
    if args.text:
        if has_fts(conn):
            where.append("id IN (SELECT id FROM prompts_fts WHERE prompts_fts MATCH ?)")
            params.append('"' + args.text.replace('"', '""') + '"')
        else:
            where.append("(title LIKE ? OR original LIKE ?)")
            params.extend([f"%{args.text}%", f"%{args.text}%"])
    sql = "SELECT id, slug, title, category, runtime, created_at, path FROM prompts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(args.limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    if args.json:
        keys = ["id", "slug", "title", "category", "runtime", "created_at", "path"]
        print(json.dumps([dict(zip(keys, row)) for row in rows], indent=2))
    else:
        for row in rows:
            print(f"{row[5]}  {row[3]:<20} {row[1]}  {row[6]}")
    return 0


def cmd_refine(args: argparse.Namespace) -> int:
    """Optional model-backed rewrite. Never runs from the hook path."""
    _, toplevel = resolve_project(Path(args.project or os.getcwd()).expanduser())
    root = resolve_root(args.root, toplevel)
    if root is None:
        print("no git repository here and no --root or PROMPT_CAPTURE_ROOT given", file=sys.stderr)
        return 1
    target = Path(args.note)
    if not target.exists():
        matches = list(root.glob(f"*/{args.note}.md"))
        if not matches:
            print(f"no note named {args.note} under {root}", file=sys.stderr)
            return 1
        target = matches[0]
    note = parse_note(target)
    if not note:
        print(f"{target} is not a prompt note", file=sys.stderr)
        return 1
    instruction = (
        "Rewrite the following raw user prompt as a high-quality, self-contained LLM prompt. Fix typos, keep every "
        "requirement and question, use the sections Role, Goal, Context, Tasks, Constraints, Deliverables, References, "
        "Questions to answer, Success criteria. Output only the rewritten prompt in Markdown.\n\n" + note["original"]
    )
    try:
        out = subprocess.run([args.command, "-p", instruction], capture_output=True, text=True, timeout=args.timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"refine failed: {exc}", file=sys.stderr)
        return 1
    if out.returncode != 0 or not out.stdout.strip():
        print(f"refine failed: {out.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    note["refactored"] = out.stdout.strip() + "\n"
    note["revised_at"] = now_iso()
    notes = load_notes(root)
    by_slug = {n["slug"]: n for n in notes}
    note["related"] = [
        {"id": by_slug[s]["id"], "slug": s, "category": by_slug[s]["category"], "score": 0.0} for s in note["related_slugs"] if s in by_slug
    ]
    target.write_text(render_note(note), encoding="utf-8")
    conn = open_db(root)
    with conn:
        upsert_note(conn, note, root)
    export_json(conn, root)
    conn.close()
    print(target)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", help="store root (default: <git toplevel>/.docs/prompts)")
        p.add_argument("--project", help="project directory used for name and root resolution")
        p.add_argument("--config", help="category table (default: hooks/lib/prompt-capture.json next to this file)")
        p.add_argument("--quiet", action="store_true")

    cap = sub.add_parser("capture", help="capture one prompt from stdin")
    common(cap)
    cap.add_argument("--runtime", default="claude", help="runtime label, e.g. claude or codex")
    mode = cap.add_mutually_exclusive_group()
    mode.add_argument("--stdin-json", action="store_true", help="stdin is a hook payload (default)")
    mode.add_argument("--stdin-text", action="store_true", help="stdin is the raw prompt text")
    cap.set_defaults(func=cmd_capture)

    rei = sub.add_parser("reindex", help="rebuild sqlite and json, recompute related links")
    common(rei)
    rei.set_defaults(func=cmd_reindex)

    qry = sub.add_parser("query", help="list or search the store")
    common(qry)
    qry.add_argument("--category")
    qry.add_argument("--runtime")
    qry.add_argument("--text", help="full-text search term (FTS5 when available)")
    qry.add_argument("--limit", default=20)
    qry.add_argument("--json", action="store_true")
    qry.set_defaults(func=cmd_query)

    ref = sub.add_parser("refine", help="rewrite one note's refactored prompt with a model (opt-in)")
    common(ref)
    ref.add_argument("note", help="note slug or path")
    ref.add_argument("--command", default="claude", help="CLI that accepts -p <prompt>")
    ref.add_argument("--timeout", type=int, default=120)
    ref.set_defaults(func=cmd_refine)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
