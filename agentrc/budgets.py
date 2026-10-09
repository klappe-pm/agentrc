"""Budget schema, the four levels it is declared at, and the check against spend.

The design: budget what a session loads and spends.
One budget object has the same shape at every level:

    user      components.json "budgets"
    project   projects-root/<project>/budgets.json "budgets"
    loadout   loadouts/<work-item>.json "budgets"
    session   a per-session override, passed by the launcher

Each level can only lower a limit set above it; resolve() keeps the lower of
the two and reports an attempt to raise one. A null or absent limit is no
limit, which is the state every default stays in until two weeks of baseline
exist (the record's baseline-before-limits section).

Units follow the record. Tokens are counted by kind, never estimated from
text; dollars are recorded beside them by the reports and are never a limit;
loaded context (skills, MCP servers, rules, hooks) is counted at launch; tool
calls are counted live by hooks/lib/tool-budget.py.

Flat keys are dotted paths ("tool_calls.total", "tool_calls.per_tool.Bash",
"tokens.cache_read"), so a limit, a measurement and a finding all name the same
thing.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple

# Every limit the schema knows, as dotted paths. tool_calls.per_tool takes any
# tool name beneath it; everything else is a fixed leaf.
LIMIT_KEYS = (
    "tool_calls.total",
    "tokens.input",
    "tokens.output",
    "tokens.cache_read",
    "tokens.cache_write",
    "tokens.total",
    "turns",
    "wall_minutes",
    "skills.count",
    "skills.tokens",
    "mcp_servers.count",
    "mcp_servers.tools",
    "rules.words",
    "hooks.count",
)
PER_TOOL = "tool_calls.per_tool"
WARN_AT = "warn_at"
# WI-101b: a machine-wide cap on live agents of one model, not a per-session
# limit. It is set only at the user level, as model and concurrent together
# (or both null), and never enters the effective limits.
FLEET = "fleet"
FLEET_KEYS = ("fleet.model", "fleet.concurrent")
DEFAULT_WARN_AT = 0.8

# The order levels are declared in. Each narrows the one before it.
LEVELS = ("user", "project", "loadout", "session")


@dataclasses.dataclass
class Finding:
    """One measure at or past its warning threshold, or past its limit."""

    key: str
    measured: float
    limit: float
    state: str  # "warn" or "over"

    def describe(self) -> str:
        verb = "exceeds" if self.state == "over" else "is near"
        return f"{self.key}: {_number(self.measured)} {verb} the budget of {_number(self.limit)}"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _is_count(value: Any) -> bool:
    # bool is an int subclass; a limit of True is a mistake, not a count of 1.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _walk(budget: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    """Every (dotted key, value) leaf of a nested budget object."""
    out: List[Tuple[str, Any]] = []
    if not isinstance(budget, dict):
        return [(prefix, budget)] if prefix else []
    for key, value in budget.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.extend(_walk(value, path))
        else:
            out.append((path, value))
    return out


def _known(key: str) -> bool:
    if key in LIMIT_KEYS or key == WARN_AT:
        return True
    return key.startswith(PER_TOOL + ".") and len(key) > len(PER_TOOL) + 1


def validate(budget: Any) -> List[str]:
    """Problems with one level's budget object; empty when it is well formed."""
    if budget is None:
        return []
    if not isinstance(budget, dict):
        return ["a budget must be a JSON object"]
    errors: List[str] = []
    fleet = budget.get(FLEET)
    if fleet is not None:
        if not isinstance(fleet, dict):
            errors.append("fleet must be an object of model and concurrent")
        else:
            model, cap = fleet.get("model"), fleet.get("concurrent")
            if model is not None and (not isinstance(model, str) or not model.strip()):
                errors.append(f"fleet.model must be a model id or null, got {model!r}")
            if cap is not None and (not _is_count(cap) or cap < 1):
                errors.append(f"fleet.concurrent must be a whole number of at least 1 or null, got {cap!r}")
            if (model is None) != (cap is None):
                errors.append("fleet.model and fleet.concurrent are set together or both null")
    # A flat "fleet.model" or "fleet.concurrent" key at the top level is
    # always invalid, whether or not a nested fleet block is also present
    # (#166): fleet configuration is only ever the whole nested object. This
    # is checked directly against budget's own keys, not the walked leaves
    # below, because a legitimate nested fleet.concurrent leaf and a flat
    # sibling key of the same name produce the identical dotted string once
    # walked, and the walk loop cannot otherwise tell which is which without
    # misreporting the legitimate one too or reporting the flat one twice.
    flat_fleet_keys = [key for key in FLEET_KEYS if key in budget]
    for key in flat_fleet_keys:
        errors.append(f"{key} is not a budget key; fleet is set only as a nested object")
    for key, value in _walk(budget):
        # A fleet block was checked whole above, and a flat fleet key just
        # above: every "fleet.model" or "fleet.concurrent" leaf _walk() can
        # produce is covered by one of those two reports already (a leaf
        # from a nested fleet dict needs isinstance(fleet, dict) true to
        # exist at all; a leaf from a flat top-level key is in
        # flat_fleet_keys by construction), so key in FLEET_KEYS alone is
        # enough to skip it here without also calling it "unknown".
        if key == FLEET or key in FLEET_KEYS:
            continue
        if not _known(key):
            hint = " (dollars are recorded, never a limit)" if "dollar" in key or "usd" in key else ""
            errors.append(f"unknown budget key {key}{hint}")
            continue
        if value is None:
            continue
        if key == WARN_AT:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
                errors.append(f"{key} must be a fraction above 0 and at most 1, got {value!r}")
        elif not _is_count(value):
            errors.append(f"{key} must be a non-negative whole number or null, got {value!r}")
    return errors


def flatten(budget: Any) -> Dict[str, Any]:
    """Dotted key to value for every limit that is set (null means unset)."""
    return {key: value for key, value in _walk(budget or {}) if value is not None}


def resolve(levels: List[Tuple[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """The effective limits, and every problem found on the way down.

    Levels are applied in order. A limit takes the lower of the value already
    in force and the new one; a level that tries to raise a limit is reported
    and ignored, so a loadout cannot spend more than its project allows. A
    level that fails validation is reported and skipped whole, which keeps a
    typo from silently lifting a limit. warn_at is a threshold rather than a
    limit, so the lowest level that sets it wins.
    """
    effective: Dict[str, Any] = {}
    errors: List[str] = []
    for name, budget in levels:
        problems = validate(budget)
        if problems:
            errors.extend(f"{name}: {problem}" for problem in problems)
            continue
        if name != LEVELS[0] and isinstance(budget, dict) and FLEET in budget:
            errors.append(f"{name}: fleet is set only in components.json (the fleet cap is machine wide)")
        for key, value in flatten(budget).items():
            if key in FLEET_KEYS:
                continue
            if key == WARN_AT:
                effective[key] = value
                continue
            current = effective.get(key)
            if current is None or value <= current:
                effective[key] = value
            else:
                errors.append(
                    f"{name}: cannot raise {key} to {_number(value)}; a level above sets it to {_number(current)}"
                )
    return effective, errors


def check(effective: Dict[str, Any], measured: Dict[str, float]) -> List[Finding]:
    """Findings for every measure past its limit, or at or past warn_at of it."""
    warn_at = effective.get(WARN_AT, DEFAULT_WARN_AT)
    findings: List[Finding] = []
    for key in sorted(measured):
        limit = effective.get(key)
        if key == WARN_AT or limit is None:
            continue
        value = measured[key]
        if value > limit:
            findings.append(Finding(key, value, limit, "over"))
        elif limit and value >= warn_at * limit:
            findings.append(Finding(key, value, limit, "warn"))
    return findings


def token_measures(usage: Dict[str, int]) -> Dict[str, int]:
    """Budget keys for a usage total in session-card's shape (in, out, cache_read, cache_write)."""
    measures = {
        "tokens.input": int(usage.get("in", 0)),
        "tokens.output": int(usage.get("out", 0)),
        "tokens.cache_read": int(usage.get("cache_read", 0)),
        "tokens.cache_write": int(usage.get("cache_write", 0)),
    }
    measures["tokens.total"] = sum(measures.values())
    return measures


def _read(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _budget_of(document: Any) -> Dict[str, Any]:
    if isinstance(document, dict) and isinstance(document.get("budgets"), dict):
        return document["budgets"]
    return {}


def levels(
    root: pathlib.Path,
    project: Optional[str] = None,
    loadout: Optional[Dict[str, Any]] = None,
    session: Optional[Dict[str, Any]] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """The four levels for one session, read from their files.

    A missing file is an empty level, so a machine with no budgets declared
    anywhere resolves to no limits at all rather than failing.
    """
    user = _budget_of(_read(root / "components.json"))
    project_budget: Dict[str, Any] = {}
    if project and project != "global":
        project_budget = _budget_of(_read(root / "projects-root" / project / "budgets.json"))
    return [
        ("user", user),
        ("project", project_budget),
        ("loadout", _budget_of(loadout)),
        ("session", session or {}),
    ]


def fleet(root: pathlib.Path) -> Optional[Tuple[str, int]]:
    """(model, concurrent) of components.json budgets.fleet; None when unset or invalid."""
    user = _budget_of(_read(root / "components.json"))
    block = user.get(FLEET)
    if not isinstance(block, dict) or validate({FLEET: block}):
        return None
    model, cap = block.get("model"), block.get("concurrent")
    if model is None:  # validate holds model and concurrent to both set or both null
        return None
    return model.strip(), cap


def load_file(path: pathlib.Path) -> Dict[str, Any]:
    """A resolved budget file written by the launcher: {"effective": {...}, ...}."""
    document = _read(path)
    if isinstance(document, dict) and isinstance(document.get("effective"), dict):
        return document["effective"]
    return {}
