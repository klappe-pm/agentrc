"""Parse control-plane.md into a query object.

The control plane is a set of Markdown tables: rows are option ids, columns
are target names (global plus every project), cells are x for opt-in. This
module is the only reader; every adapter and the project sync ask it what is
enabled.

    from stratarc.control_plane import ControlPlane
    cp = ControlPlane.load()            # <source root>/control-plane.md
    cp.enabled("global", "hook:prose-guard") -> bool
    cp.enabled_ids("my-app", prefix="rule:") -> {"command-cwd-scoping", ...}
    cp.columns                                   -> ["global", "my-app", ...]
    cp.rows                                      -> ["AGENTS.md", "rule:...", ...]

Rules:
  - Lines starting with # inside the first column are comments and are skipped.
  - Option labels may be Markdown links. Their label is the option id.
  - A cell counts as opted in if, after stripping, it is x or X.
  - Unknown columns and rows are allowed; the sync reports rows that name
    nothing under the source root but never fails on them.
  - Header row must start with the literal cell `option`.
"""

from __future__ import annotations

import pathlib
import re
import sys
from dataclasses import dataclass, field

from stratarc.paths import home, source_root

CONTROL_PLANE_NAME = "control-plane.md"


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


_MARKDOWN_LINK = re.compile(r"^\[([^\]]+)\]\([^)]+\)$")


def _option_id(cell: str) -> str:
    """Return a stable option id from a plain or linked first-column cell."""
    match = _MARKDOWN_LINK.match(cell.strip())
    return match.group(1).strip() if match else cell


def set_cell(path: pathlib.Path, option_id: str, column: str, on: bool = True) -> bool:
    """Set one cell of the option table, leaving every other byte as it was.

    The row is the one whose first-column option id is *option_id*; the cell
    is the one under the header named *column*, found in the header of the
    table the row sits in (a tier column, when present, shifts the cells but
    is not a column). An opted-in cell reads `x`, an opted-out one is empty,
    the same shapes the reconciler renders, so the next reconcile finds the
    file already current. Returns True when the file changed, False when the
    cell already held that value. Raises KeyError when the file, the row or
    the column is absent, so a caller never believes a selection was made
    that was not.
    """
    if not path.is_file():
        raise KeyError(f"{path} does not exist")
    lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
    header: list[str] | None = None
    offset = 1
    found = False
    changed = False
    for index, raw in enumerate(lines):
        if not raw.lstrip().startswith("|"):
            header = None
            continue
        body = raw.rstrip("\r\n")
        ending = raw[len(body) :]
        cells = _cells(body)
        if not cells:
            continue
        if cells[0].lower() == "option":
            has_tier = len(cells) > 1 and cells[1].strip().lower() == "tier"
            offset = 2 if has_tier else 1
            header = cells[offset:]
            continue
        if header is None or _option_id(cells[0]) != option_id:
            continue
        found = True
        if column not in header:
            continue
        position = header.index(column) + offset
        if position >= len(cells):
            raise KeyError(f"row {option_id} is shorter than its table header")
        if (cells[position].lower() == "x") == on:
            continue
        # Split on the raw pipes so the padding of every other cell survives;
        # parts[0] precedes the leading pipe, so cell n is parts[n + 1].
        parts = body.split("|")
        parts[position + 1] = " x " if on else "  "
        lines[index] = "|".join(parts) + ending
        changed = True
    if not found:
        raise KeyError(f"no row {option_id}")
    if not changed:
        # Either the cell already held the value, or the column is absent
        # from every table the row sits in.
        if column not in _header_of(lines, option_id):
            raise KeyError(f"no column {column} for row {option_id}")
        return False
    path.write_bytes("".join(lines).encode("utf-8"))
    return True


def _header_of(lines: list[str], option_id: str) -> list[str]:
    """The column names of the table(s) holding *option_id*, for set_cell's error path."""
    header: list[str] | None = None
    out: list[str] = []
    for raw in lines:
        if not raw.lstrip().startswith("|"):
            header = None
            continue
        cells = _cells(raw)
        if cells and cells[0].lower() == "option":
            has_tier = len(cells) > 1 and cells[1].strip().lower() == "tier"
            header = cells[2:] if has_tier else cells[1:]
        elif cells and header is not None and _option_id(cells[0]) == option_id:
            out.extend(header)
    return out


@dataclass
class ControlPlane:
    columns: list[str] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)
    _grid: dict[str, dict[str, bool]] = field(default_factory=dict)
    rule_tiers: dict[str, str] = field(default_factory=dict)
    manifest: dict[str, dict[str, str]] = field(default_factory=dict)
    path: pathlib.Path | None = None

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> "ControlPlane":
        """Parse *path*, or `<source root>/control-plane.md` when omitted."""
        if path is None:
            path = source_root() / CONTROL_PLANE_NAME
        cp = cls(path=path)
        if not path.exists():
            return cp
        # The file holds more than one table. A table is entered by its header
        # row and left at the first line that is not a table row, so the prose
        # and the policy tables between them are skipped rather than parsed.
        #
        # The rules table carries one further column, `tier`, immediately
        # after `option`. A header row is only ever `| option | ... |`
        # (no tier) or `| option | tier | ... |`; every other table keeps the
        # plain shape, so `has_tier` is decided fresh at each header row and
        # `cp.columns` never includes the literal `tier` cell.
        mode: str | None = None
        man_header: list[str] = []
        has_tier = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.lstrip().startswith("|"):
                mode = None
                continue
            cells = _cells(raw)
            if not cells:
                continue
            head = cells[0].lower()
            if head == "option":
                mode = "options"
                has_tier = len(cells) > 1 and cells[1].strip().lower() == "tier"
                cp.columns = cells[2:] if has_tier else cells[1:]
                continue
            if head == "project":
                mode = "manifest"
                man_header = [c.lower() for c in cells[1:]]
                continue
            if mode is None:
                continue
            if set(cells[0]) <= {"-", ":", " "}:
                continue  # separator row
            key = _option_id(cells[0])
            if not key or key.startswith("#"):
                continue
            if mode == "manifest":
                rec = {}
                for i, fld in enumerate(man_header, start=1):
                    val = cells[i] if i < len(cells) else ""
                    rec[fld] = "" if val == "-" else val
                cp.manifest[key] = rec
                continue
            if key not in cp._grid:
                cp.rows.append(key)
            offset = 1
            if has_tier:
                offset = 2
                tier_val = cells[1].strip() if len(cells) > 1 else ""
                if tier_val and tier_val != "-":
                    cp.rule_tiers[key] = tier_val
            row = {}
            for i, col in enumerate(cp.columns, start=offset):
                val = cells[i] if i < len(cells) else ""
                row[col] = val.lower() == "x"
            cp._grid[key] = row
        return cp

    def enabled(self, column: str, option: str) -> bool:
        return self._grid.get(option, {}).get(column, False)

    def enabled_ids(self, column: str, prefix: str) -> set[str]:
        out = set()
        for key, row in self._grid.items():
            if key.startswith(prefix) and row.get(column, False):
                out.add(key[len(prefix) :])
        return out

    def enabled_rows(self, column: str) -> list[str]:
        return [k for k, r in self._grid.items() if r.get(column, False)]

    def has_column(self, column: str) -> bool:
        return column in self.columns

    def projects(self) -> list[str]:
        return [c for c in self.columns if c != "global"]

    # ---- manifest ------------------------------------------------------

    def field(self, project: str, name: str, default: str = "") -> str:
        return self.manifest.get(project, {}).get(name, default)

    def status(self, project: str) -> str:
        """active, new, inactive, archived, external, or unlisted."""
        return self.field(project, "status") or "unlisted"

    def tier(self, project: str) -> str:
        return self.field(project, "tier") or "normal"

    def rule_tier(self, option: str) -> str:
        """The rules-table tier cell for *option* (e.g. ``rule:repo-scope``).

        Distinct from `tier`, which reads a project's trust tier from the
        manifest table. Empty when the rules table carries no tier column
        (an older document) or the row has no tier recorded.
        """
        return self.rule_tiers.get(option, "")

    def template(self, project: str) -> str:
        return self.field(project, "template") or "base"

    def is_active(self, project: str) -> bool:
        return self.status(project) == "active"

    def active_projects(self) -> list[str]:
        return [p for p in self.manifest if self.is_active(p)]

    def find_checkout(
        self, project: str, root: pathlib.Path | None = None
    ) -> list[pathlib.Path]:
        """Checkout folders named *project* under <home>/projects/.

        The manifest is authoritative for policy; the tree is only where the
        files happen to sit. Only real checkouts are returned, meaning they must
        contain a `.git` directory. More than one hit is drift for --verify to
        report, not something to silently pick a winner from.
        """
        base = root or (home() / "projects")
        if not base.is_dir():
            return []
        # Bounded to the two depths the current layout uses:
        #   <base>/<status>/<project>            inactive, archived, external, new
        #   <base>/<status>/<project>            active, flat layout
        #   <base>/<status>/<tier>/<project>     active, legacy tiered layout
        # A recursive walk would descend into vendored trees and node_modules
        # for no gain, since a checkout never sits deeper than this.
        hits = []
        for pattern in (f"*/{project}", f"*/*/{project}"):
            for p in sorted(base.glob(pattern)):
                if not p.is_dir():
                    continue
                if not (p / ".git").exists():
                    continue
                # Trash, worktree pools and other dot or underscore trees are
                # not checkouts. This exclusion has to match the one in
                # the projects verifier or the two disagree about what exists.
                rel = p.relative_to(base)
                if any(part.startswith((".", "_")) for part in rel.parts):
                    continue
                hits.append(p)
        return hits


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cp = ControlPlane.load()
    col = args[0] if args else "global"
    print(f"{col}: {len(cp.enabled_rows(col))} of {len(cp.rows)} options enabled")
    for r in cp.enabled_rows(col):
        print("  ", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
