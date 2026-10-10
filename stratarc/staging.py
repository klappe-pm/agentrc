"""Build a filtered view of the source root for one control-plane column.

The adapters read a source tree and mirror it. The control plane decides what
is IN that tree. So for each target column we materialize a staging directory
that contains only the opted-in items, and hand that to the adapter. Adapters
never learn about the control plane; they just see a smaller source root.

    from stratarc.staging import build_stage
    stage = build_stage(root, cp, "global")     # pathlib.Path to a temp dir
    adapter.sync(stage, target)

Every staged rule (rules/README.md too) and the staged AGENTS.md have their
`${STRATARC_SOURCE}` tokens rendered to the root the stage is built from, so
the published source stays free of this machine's paths while the deployed
copy names them (_stage_rendered).

Guard scripts ship as package data (`stratarc/data/hooks`). The stage overlays
the source root's own `hooks/` on top of them, file by file with the source
winning: a hook script of the same name replaces the packaged one, the
`hooks.json` registries merge with a source group replacing a packaged group
that runs the same hooks, and `hooks/lib/` is copied from the package and then
from the source root. A `private/` directory in the package data is never
staged; the source root's own `hooks/lib/private/` is staged for the global
column only (the user-level runtimes), never from package data.

For the global column the stage mirrors the source root's layout:
  AGENTS.md, rules/{global,common}/<enabled>.md, rules/tiers.json, hooks/<enabled>.sh,
  hooks/lib/**, hooks/hooks.json (filtered to enabled hooks),
  skills/<enabled>/, commands/<enabled>.md, agents/<enabled>.md, permissions.json,
  components.json (only the wanted MCP servers and plugins the column selects,
  plus runtime_settings)

For a project column it mirrors projects-root/<name>/ filtered by the
project:* rows, PLUS the shared rules/hooks that column opted into, so a
project can carry a subset of global rules in its own .claude/rules/. A shared
rule the global column also ships is staged as its binding section only (see
_binding_only), pointing at the full text in the canonical source tree.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
from importlib.resources import as_file

from stratarc import paths
from stratarc.control_plane import ControlPlane
from stratarc.resources import data_dir
from stratarc.rules_digest import SOURCE_TOKEN, _extract_binding_section, render_source, rules_prefix

_STAGES: list[pathlib.Path] = []

# The directories a project may carry under projects-root/<name>/, each
# selected by its project:<dir> row. The single definition: the projects sync
# delivers them and the reconciler writes their rows.
PROJECT_DIRECTORIES = ("rules", "hooks", "agents", "commands", "scripts", "skills")


def _copy(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            src,
            dst,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"),
        )
    else:
        shutil.copy2(src, dst)


# Names under a hooks/lib/ tree that are never staged.
_LIB_IGNORED = (".DS_Store", "__pycache__", "private", "fixtures", "*.test.sh", "*.test.py")


def _group_hook_names(group: dict) -> tuple[str, ...]:
    """The hook script names a registry group runs, or its raw commands when it names none."""
    commands = [h.get("command", "") for h in group.get("hooks", [])]
    names = tuple(c.split("/hooks/")[-1].split(".sh")[0] for c in commands if "/hooks/" in c)
    return names or tuple(commands)


def _merge_registries(base: dict, override: dict) -> dict:
    """Merge two hooks.json registries: a group of *override* replaces the *base* group of the same event that runs the same hooks, and any other group is appended."""
    merged = {event: list(groups) for event, groups in base.items()}
    for event, groups in override.items():
        target = merged.setdefault(event, [])
        index = {_group_hook_names(g): i for i, g in enumerate(target)}
        for group in groups:
            key = _group_hook_names(group)
            if key in index:
                target[index[key]] = group
            else:
                index[key] = len(target)
                target.append(group)
    return merged


def _read_registry(src: pathlib.Path) -> dict:
    if not src.is_file():
        return {}
    return json.loads(src.read_text())


def _hook_file(dirs: tuple[pathlib.Path, ...], relative: str) -> pathlib.Path | None:
    """The file *relative* names under the last of *dirs* that holds it, so the source root wins."""
    for directory in reversed(dirs):
        candidate = directory / relative
        if candidate.is_file():
            return candidate
    return None


def _filter_hooks_json(src: pathlib.Path | dict, enabled: set[str]) -> dict:
    """Keep only hook groups whose every command references an enabled hook.

    *src* is a registry file or an already loaded registry.
    """
    data = src if isinstance(src, dict) else _read_registry(src)
    if not data:
        return {}
    out: dict = {}
    for event, groups in data.items():
        kept = []
        for g in groups:
            cmds = [h.get("command", "") for h in g.get("hooks", [])]
            names = [
                c.split("/hooks/")[-1].split(".sh")[0] for c in cmds if "/hooks/" in c
            ]
            if names and all(n in enabled for n in names):
                kept.append(g)
        if kept:
            out[event] = kept
    return out


# components.json section -> the control-plane row prefix that selects it.
COMPONENT_ROWS = {"mcp_servers": "mcp:", "plugins": "plugin:"}

# Sections carried whole into the global stage, wanted entries only, with no
# control-plane row of their own. A row would
# default them off and the hook would never render, and neither is a per-
# project choice: both describe what the user's own runtime directories hold.
USER_SCOPE_SECTIONS = ("foreign_hooks", "third_party_skills")


def _stage_registrations(root: pathlib.Path, stage: pathlib.Path, entries: list) -> None:
    """Copy each captured registration a wanted foreign hook names into the
    stage at its own repository-relative path, so an adapter reads it from
    the stage exactly as components.json spells it."""
    for entry in entries:
        registration = entry.get("registration")
        if not isinstance(registration, dict):
            continue
        for relative in registration.values():
            if not isinstance(relative, str) or not relative:
                continue
            source = root / relative
            if source.is_file():
                _copy(source, stage / relative)


def _selected_components(root: pathlib.Path, cp: ControlPlane, col: str) -> dict | None:
    """The manifest's wanted MCP servers and plugins that col selects, plus
    the global stage's user-scope sections, or None when root has no readable
    components.json."""
    path = root / "components.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    out: dict = {"version": document.get("version", 1)}
    # runtime_settings is user scope, so only the global stage carries it
    if col == "global" and isinstance(document.get("runtime_settings"), dict):
        out["runtime_settings"] = document["runtime_settings"]
    for section, prefix in COMPONENT_ROWS.items():
        chosen = cp.enabled_ids(col, prefix)
        out[section] = [
            entry
            for entry in document.get(section) or []
            if isinstance(entry, dict) and entry.get("wanted") is True and entry.get("name") in chosen
        ]
    if col == "global":
        for section in USER_SCOPE_SECTIONS:
            wanted = [
                entry
                for entry in document.get(section) or []
                if isinstance(entry, dict) and entry.get("wanted") is True
            ]
            # Absent rather than empty, so a stage with nothing wanted reads
            # exactly as it did before this section existed.
            if wanted:
                out[section] = wanted
    return out


def _binding_only(rule: pathlib.Path, root: pathlib.Path) -> str | None:
    """The rule's H1, binding section and a pointer to the full text under
    root, or None when it has no binding section (the caller then ships it
    whole)."""
    binding = _extract_binding_section(_rendered(rule, root))
    if not binding:
        return None
    return (
        f"# {rule.stem}\n\n## binding\n\n{binding}\n\n"
        f"Full rule: `{rules_prefix(root)}{rule.name}`.\n"
    )


def _rendered(source: pathlib.Path, root: pathlib.Path) -> str:
    """The file's text with every `${STRATARC_SOURCE}` token rendered to root.

    The source rules and AGENTS.md are published and never carry a checkout
    path, and neither does the committed digest; the staged copy is what
    this machine's runtimes read, so it names the tree the stage is built
    from (the same rendering the digest applies for a source_root).
    """
    return render_source(source.read_text(encoding="utf-8"), root)


def _stage_rendered(src: pathlib.Path, dst: pathlib.Path, root: pathlib.Path) -> None:
    """Copy a text file into the stage, rendered. A file without the token
    is copied as is, metadata included, so it stays byte-identical."""
    if SOURCE_TOKEN not in src.read_text(encoding="utf-8"):
        _copy(src, dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(_rendered(src, root), encoding="utf-8")


def _stage_shared(
    root: pathlib.Path,
    cp: ControlPlane,
    col: str,
    stage: pathlib.Path,
    path_prefix: str,
) -> list[str]:
    """Shared items (rules, hooks, skills, commands, agents) into stage."""
    with as_file(data_dir("hooks")) as packaged_hooks:
        return _stage_shared_with_hooks(
            root, cp, col, stage, path_prefix, (pathlib.Path(packaged_hooks), root / "hooks")
        )


def _stage_shared_with_hooks(
    root: pathlib.Path,
    cp: ControlPlane,
    col: str,
    stage: pathlib.Path,
    path_prefix: str,
    hook_dirs: tuple[pathlib.Path, ...],
) -> list[str]:
    """_stage_shared with the hook directories to overlay, packaged first."""
    notes: list[str] = []

    if cp.enabled(col, "AGENTS.md") and (root / "AGENTS.md").exists():
        _stage_rendered(root / "AGENTS.md", stage / "AGENTS.md", root)

    # Permission policy is global-only and is consumed by every adapter. It
    # does not belong in a project-scoped runtime configuration.
    if col == "global" and (root / "permissions.json").is_file():
        _copy(root / "permissions.json", stage / "permissions.json")

    # components.json, filtered to the wanted MCP servers and plugins this
    # column selects through its mcp: and plugin: rows. The global
    # stage also carries runtime_settings, which an adapter reads to render
    # settings keys with no other source, such as statusLine, and the default
    # models. A project stage carries its own column's selection, which
    # the projects sync writes into the project's local, untracked scope.
    selected = _selected_components(root, cp, col)
    if selected is not None:
        (stage / "components.json").write_text(json.dumps(selected, indent=2) + "\n")
        _stage_registrations(root, stage, selected.get("foreign_hooks") or [])

    # A project stage carries a rule the global column also ships as its
    # binding section only. Claude Code loads the user scope's digest and the
    # project's .claude/rules/ together without deduplicating, and full copies
    # pushed every managed project's session past Claude Code's
    # instruction-size limit. The copy stays (binding only) because most
    # projects commit .claude/rules/, and a cloud session has no user scope.
    user_scope = cp.enabled_ids("global", "rule:") if col != "global" else set()
    for rid in cp.enabled_ids(col, "rule:"):
        f = root / "rules" / f"{rid}.md"
        if not f.exists():
            notes.append(f"rule:{rid} not found under rules/")
        elif rid in user_scope and (binding := _binding_only(f, root)) is not None:
            (stage / "rules").mkdir(parents=True, exist_ok=True)
            (stage / "rules" / f.name).write_text(binding, encoding="utf-8")
        else:
            _stage_rendered(f, stage / "rules" / f.name, root)

    if col == "global" and cp.enabled_ids(col, "rule:"):
        if (root / "rules" / "README.md").exists():
            _stage_rendered(root / "rules" / "README.md", stage / "rules" / "README.md", root)
        # The tier manifest travels with the rules it classifies: the
        # OpenCode adapter reads it from the stage to tell the common tier
        # from the global one.
        if (root / "rules" / "tiers.json").is_file():
            _copy(root / "rules" / "tiers.json", stage / "rules" / "tiers.json")

    hooks = cp.enabled_ids(col, "hook:")
    for h in hooks:
        f = _hook_file(hook_dirs, f"{h}.sh")
        if f is not None:
            _copy(f, stage / "hooks" / f.name)
        else:
            notes.append(f"hook:{h} not found under hooks/")
    if hooks:
        for directory in hook_dirs:
            if (directory / "lib").is_dir():
                shutil.copytree(
                    directory / "lib",
                    stage / "hooks" / "lib",
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*_LIB_IGNORED),
                )
        # The source root's private hook files reach the user-level runtimes
        # only. Package data never holds them, and a project column's hooks
        # land in a checkout that may be tracked or public.
        private = root / "hooks" / "lib" / "private"
        if col == "global" and private.is_dir():
            shutil.copytree(
                private,
                stage / "hooks" / "lib" / "private",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"),
            )
        runtime_hooks = _hook_file(hook_dirs, "opencode-runtime-hooks.ts")
        if runtime_hooks is not None:
            _copy(runtime_hooks, stage / "hooks" / runtime_hooks.name)
    worktree_hooks = {"worktree-create", "worktree-remove", "worktree-validate"}
    manifest = _hook_file(hook_dirs, "claude-worktree-hooks.json")
    if col == "global" and hooks.intersection(worktree_hooks) and manifest is not None:
        _copy(manifest, stage / "hooks" / manifest.name)
    agent_graph_manifest = _hook_file(hook_dirs, "claude-agent-graph-hooks.json")
    agent_graph_hooks = (
        _filter_hooks_json(agent_graph_manifest, hooks)
        if col == "global" and agent_graph_manifest is not None
        else {}
    )
    if agent_graph_hooks:
        (stage / "hooks").mkdir(parents=True, exist_ok=True)
        (stage / "hooks" / agent_graph_manifest.name).write_text(
            json.dumps(agent_graph_hooks, indent=2) + "\n"
        )
    registry: dict = {}
    for directory in hook_dirs:
        registry = _merge_registries(registry, _read_registry(directory / "hooks.json"))
    hj = _filter_hooks_json(registry, hooks)
    if hj:
        hj_text = json.dumps(hj, indent=2).replace("$HOME/.claude/hooks/", path_prefix)
        (stage / "hooks").mkdir(parents=True, exist_ok=True)
        (stage / "hooks" / "hooks.json").write_text(hj_text + "\n")

    for s in cp.enabled_ids(col, "skill:"):
        d = root / "skills" / s
        if d.is_dir():
            _copy(d, stage / "skills" / s)
        else:
            notes.append(f"skill:{s} not found under skills/")
    ex = root / "skills" / "sync-exclude.json"
    if ex.exists() and (stage / "skills").is_dir():
        _copy(ex, stage / "skills" / ex.name)

    for c in cp.enabled_ids(col, "command:"):
        f = root / "commands" / f"{c}.md"
        if f.exists():
            _copy(f, stage / "commands" / f.name)
        else:
            notes.append(f"command:{c} not found under commands/")

    for a in cp.enabled_ids(col, "agent:"):
        f = root / "agents" / f"{a}.md"
        if f.exists():
            _copy(f, stage / "agents" / f.name)
        else:
            notes.append(f"agent:{a} not found under agents/")

    return notes


def _drop_shadowed_skills(own: pathlib.Path, staged: pathlib.Path, col: str) -> None:
    """Remove each shared skill the project's own skill of the same name
    replaces, so the two directories are never merged file by file. The
    project's skill wins whole; a note on stderr says so."""
    for skill in sorted(p for p in own.iterdir() if p.is_dir()):
        shared = staged / skill.name
        if shared.is_dir():
            print(
                f"staging: note: {col} skills/{skill.name} replaces the shared skill:{skill.name}",
                file=sys.stderr,
            )
            shutil.rmtree(shared)


def build_stage(
    root: pathlib.Path, cp: ControlPlane, col: str
) -> tuple[pathlib.Path, list[str]]:
    """Return (stage_dir, notes). Caller may delete stage_dir; cleanup_all() also does."""
    stage = pathlib.Path(tempfile.mkdtemp(prefix=f"stratarc-stage-{col}-"))
    _STAGES.append(stage)
    # A stage carries no stratarc.toml, so an adapter asking the stage for the
    # engine name would get the default. Record the source root's name there.
    (stage / paths.CONFIG_NAME).write_text(f'name = "{paths.engine_name(root)}"\n', encoding="utf-8")
    prefix = (
        "$HOME/.claude/hooks/"
        if col == "global"
        else "$CLAUDE_PROJECT_DIR/.claude/hooks/"
    )
    notes = _stage_shared(root, cp, col, stage, prefix)

    if col != "global":
        proj = root / "projects-root" / col
        if proj.is_dir():
            if cp.enabled(col, "project:AGENTS.md") and (proj / "AGENTS.md").exists():
                _stage_rendered(proj / "AGENTS.md", stage / "AGENTS.md", root)
                if (proj / "surfaces.json").exists():
                    _copy(proj / "surfaces.json", stage / "surfaces.json")
            for d in PROJECT_DIRECTORIES:
                if cp.enabled(col, f"project:{d}") and (proj / d).is_dir():
                    if d == "skills":
                        _drop_shadowed_skills(proj / d, stage / d, col)
                    _copy(proj / d, stage / d)
    return stage, notes


def cleanup_all() -> None:
    for s in _STAGES:
        shutil.rmtree(s, ignore_errors=True)
    _STAGES.clear()
