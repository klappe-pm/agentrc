"""Load and check components.json, the one manifest of external components.

The manifest declares what the source root does not author itself: MCP servers, plugins, foreign hook groups, third-party skills, services, command-line dependencies and per-runtime settings, plus the user-level budget defaults.

Every entry carries a name, the runtimes it applies to, its owner, and wanted (false for a component known and deliberately unwanted), and each section adds its own shape (SECTION_KEYS): an MCP server a command (with args and env) or a url unless hosted, a plugin its marketplace (an OpenCode file plugin its path), a foreign hook group the match that finds it and, optionally, the registration an adapter renders when the source root is the one installing the group rather than only recognizing it, a third-party skill its installer and, where one entry installs several directories, their names, a service its kind and label (and match and remove when unwanted, container, the command a container supervisor runs, environment, non-secret environment variable defaults a launcher applies at start, and image, a Docker image a launcher runs directly, pinned to a tag or digest, never latest), a dependency its command, install line, pinned version and image source. platform_differences declares what legitimately differs between platforms, environments the ref each environment deploys from, and promotion the default soak and the risk tiers a candidate's changed paths are scored against. runtime_settings.<runtime> holds exactly model, subagent_model, aliases, settings, environment_plugins (claude only) and provider (opencode only, a custom OpenAI-compatible provider) (RUNTIME_SETTINGS_KEYS). deploy declares each hosted service's configuration (platform, source, start command, healthcheck, restart, serverless, variables by name); the platform names a validator that an optional private module under the source root registers (DEPLOY_PLATFORMS), and a source root without that module accepts no deploy target. An unknown key is an error. Runtime names are the adapters' registry (agentrc.adapters._common.runtime_registry), not a list kept here. Secrets appear only as secret:// references; a literal the token-shaped detector matches is refused, and the message never repeats the value.

The service and port check and the service installer live here too.

    python3 -m agentrc.components                          validate, exit 1 on any problem
    python3 -m agentrc.components --list                   print what is declared
    python3 -m agentrc.components --services               check each service and its port, exit 1 on a finding
    python3 -m agentrc.components --install-service NAME   copy its plist into LaunchAgents and bootstrap it (macOS only)
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys
from importlib.resources import as_file
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from xml.parsers.expat import ExpatError

from agentrc import budgets
from agentrc.adapters._common import runtime_registry
from agentrc.paths import home, source_root
from agentrc.resources import data_dir

MANIFEST_NAME = "components.json"

SECTIONS = (
    "mcp_servers",
    "plugins",
    "foreign_hooks",
    "third_party_skills",
    "services",
    "dependencies",
)
TOP_LEVEL = ("version", "budgets", "runtime_settings", "platform_differences", "environments", "promotion", "deploy", "autonomy") + SECTIONS

# The dispatcher's limits: key, default, and whether null (no cap) is allowed.
# A dispatcher reads them through autonomy_limits().
AUTONOMY_LIMITS = (
    ("concurrency", 2, False),
    ("per_project", 1, False),
    ("interval_minutes", 10, False),
    ("daily_tokens", None, True),
)


def validate_autonomy(block: Any) -> List[str]:
    """Every problem with the manifest's autonomy block; [] for an absent or valid one."""
    if block is None:
        return []
    if not isinstance(block, dict):
        return ["autonomy must be an object with concurrency, per_project, interval_minutes and daily_tokens"]
    known = {key for key, _default, _nullable in AUTONOMY_LIMITS}
    problems = [f"autonomy: unknown key {key}; use {', '.join(sorted(known))}" for key in block if key not in known]
    for key, _default, nullable in AUTONOMY_LIMITS:
        if key not in block:
            continue
        value = block[key]
        if value is None and nullable:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            allowed = "a whole number of at least 1, or null for no cap" if nullable else "a whole number of at least 1"
            problems.append(f"autonomy: {key} must be {allowed}, got {value!r}")
    return problems


def autonomy_limits(block: Any) -> Dict[str, Any]:
    """The dispatcher's limits with defaults filled; call only on a block validate_autonomy() accepted."""
    block = block if isinstance(block, dict) else {}
    return {key: block.get(key, default) for key, default, _nullable in AUTONOMY_LIMITS}

# environments: the ref each environment deploys from.
# agentrc.deploy_guard reads it for the sync branch guard and the
# post-commit hook; which environment a checkout is in is selected on the
# checkout, never here.
ENVIRONMENT_KEYS = ("ref", "description")

# promotion: the promotion gate. soak_days is how long a candidate runs
# deployed, with no new failure in its telemetry, before a promotion plan
# calls it eligible; 3 when the section is absent. risk_tiers is the declared
# data a promotion plan scores a candidate's changed paths against: name to soak_days and the
# path patterns that carry it, at most one tier marked documentation: true.
# Per project (projects-root/<project>/promotion.json) and per feature
# (a loadout's promotion.soak_days) soaks are the same plain shape,
# SOAK_OVERRIDE_KEYS below, never risk_tiers: that is declared once, here.
PROMOTION_KEYS = ("soak_days", "risk_tiers", "description")
DEFAULT_SOAK_DAYS = 3
RISK_TIER_KEYS = ("soak_days", "paths", "documentation", "description")
SOAK_OVERRIDE_KEYS = ("soak_days", "description")

# Per-section shapes: every section has a checked shape. Every entry carries the
# common keys; each section adds its own. A key in neither list is an error,
# so a typo cannot silently disable a check. Conditional requirements (a
# command or a url, a marketplace or a path, match and remove on an unwanted
# service) are checked in _check_section below.
COMMON_KEYS = ("name", "runtimes", "owner", "wanted", "description")
SECTION_KEYS: Dict[str, Tuple[str, ...]] = {
    "mcp_servers": ("command", "args", "env", "cwd", "url", "transport", "hosted"),
    "plugins": ("marketplace", "path"),
    "foreign_hooks": ("match", "registration"),
    "third_party_skills": ("installer", "skills"),
    "services": ("kind", "label", "port", "plist", "match", "remove", "installed", "container", "environment", "image"),
    "dependencies": ("command", "install", "version", "version_command", "image", "platform"),
}
REQUIRED_STRINGS: Dict[str, Tuple[str, ...]] = {
    "foreign_hooks": ("match",),
    "third_party_skills": ("installer",),
    "services": ("label",),
    "dependencies": ("command", "install", "version"),
}
SERVICE_KINDS = ("launchd", "app")
_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
# A service's own Docker image, run directly by a launchd job or a launcher
# script rather than baked into a container image (dependencies' image is
# for that instead). Pinned to a tag or a digest: ":latest" or an untagged
# reference lets the running version drift with no record of when.
_IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\Z")

# How a dependency reaches a container image. An image builder renders each
# method; the version the entry pins
# is asserted inside the image after install, so a drifted package fails the
# build instead of shipping. base: the image's FROM line, a template holding
# {version}; apt: a Debian package; npm: a global package installed at the
# pinned version; release: an archive downloaded from a url template holding
# {version} and, where the vendor names architectures its own way, {arch}. A
# tar release also declares how deep its archive is: strip 1 (the default) is
# an archive wrapping one directory whose bin/ becomes /usr/local/bin, strip 0
# an archive holding the executables themselves. Stripping a component off a
# flat archive extracts nothing and still exits 0, so the depth is declared
# rather than guessed; a release archive whose executable inside it is not
# named for the command it installs declares binary, the name to rename after
# unpack; a release may pin sha256 as a map from each image architecture to
# its archive's checksum, which the image verifies before unpacking;
# source: a release tarball from a url template holding
# {version}, verified against its pinned sha256 and compiled in the image, for
# a tool whose distribution package is too old.
IMAGE_KEYS: Dict[str, Tuple[str, ...]] = {
    "base": ("from",),
    "apt": ("package",),
    "npm": ("package",),
    "release": ("url", "extract", "arch", "strip", "binary", "sha256"),
    "source": ("url", "sha256"),
}
# tar: an archive holding one directory above the files, unpacked with
# --strip-components=1. tar-flat: an archive holding the bare executable with
# no directory above it, unpacked into /usr/local/bin,
# where --strip-components=1 would strip the only component and extract
# nothing. zip: unpacked into /usr/local/bin with no directory to strip.
# zip-strip: an archive holding one directory above the files (for example
# tool-linux-x64/tool), unpacked with unzip -j, which junks each file's leading
# path the way --strip-components=1 discards the wrapper directory for tar.
IMAGE_EXTRACTS = ("tar", "tar-flat", "zip", "zip-strip")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ARCHES = ("amd64", "arm64")
IMAGE_STRIPS = (0, 1)

# platform_differences: what the suite and the parity check may legitimately
# see differ between two platforms. platform is the one platform the
# difference exists on; tests are the repository paths a test runner skips
# elsewhere, naming the difference as it does; paths are fnmatch patterns
# over environment-parity manifest keys,
# each opening with its scope (PARITY_SCOPES), that the parity comparison
# excuses between two environments on different platforms.
PLATFORM_DIFFERENCE_KEYS = ("name", "description", "platform", "tests", "paths")

# runtime_settings.<runtime>: the default model
# and subagent model, an alias map from the short names agent files use
# (opus, sonnet, haiku) to this runtime's model identifiers, and settings, a
# runtime-native passthrough object (it absorbed runtime-settings/claude.json).
RUNTIME_SETTINGS_KEYS = ("model", "subagent_model", "aliases", "settings", "environment_plugins", "provider")
PLATFORMS = ("darwin", "linux")
PARITY_SCOPES = ("home:", "project:", "runtime:")

# deploy: each hosted service this repository configures, the shape the
# platform's deploy script plans and checks against the live service.
# Variables are declared by name: a sealed one carries only its secret://
# reference and is set by hand, a plain one its literal.
# The shape is checked here; the platform's own vocabulary (its restart
# policy names, for one) is checked by the validator registered for it in
# DEPLOY_PLATFORMS, so this module names no hosting platform.
DEPLOY_KEYS = (
    "name", "platform", "description", "service", "source", "start_command", "healthcheck",
    "restart_policy", "serverless", "replicas", "status_port", "variables",
)  # fmt: skip
DEPLOY_SOURCE_KEYS = ("image", "repo", "dockerfile")
DEPLOY_HEALTHCHECK_KEYS = ("path", "timeout")
DEPLOY_VARIABLE_KEYS = ("name", "sealed", "secret", "value", "description")
_VARIABLE_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# The deploy platform registry: platform name to a validator called with the
# target entry and its error label, returning every problem as a string. It
# is filled from the optional private module at private_platforms_path(),
# loaded by path when the file exists, so the engine carries no platform of
# its own and a source root without the module refuses every deploy target,
# naming the module that would register its platform.
DeployValidator = Callable[[Dict[str, Any], str], List[str]]


def private_platforms_path() -> pathlib.Path:
    """Where the optional deploy platform module lives: under the source root, never beside the engine."""
    return source_root() / "scripts" / "private" / "components_platforms.py"


class PlatformRegistryError(RuntimeError):
    """The private platform module exists but does not register platforms."""


def load_deploy_platforms(path: Optional[pathlib.Path] = None) -> Dict[str, DeployValidator]:
    """The platforms the private module at path registers; {} when there is none.

    path defaults to private_platforms_path().

    A missing file is the absent extension and loads nothing. A file that is
    present but does not declare DEPLOY_PLATFORMS as a map of names to
    callables is a defect in the private module and raises, so it is fixed
    rather than silently leaving every deploy target unrecognized.
    """
    path = path or private_platforms_path()
    if not path.is_file():
        return {}
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise PlatformRegistryError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    platforms = getattr(module, "DEPLOY_PLATFORMS", None)
    if not isinstance(platforms, dict) or not all(
        _nonempty_string(name) and callable(validator) for name, validator in platforms.items()
    ):
        raise PlatformRegistryError(f"{path.name} must declare DEPLOY_PLATFORMS, a map of platform names to validators")
    return dict(platforms)


def known_runtimes(adapters_dir: Optional[pathlib.Path] = None) -> Tuple[str, ...]:
    """The runtime names the adapters' registry declares."""
    return tuple(runtime_registry(adapters_dir))


class ManifestError(ValueError):
    """The manifest exists but cannot be read as JSON."""


def load(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """The manifest (default `<source root>/components.json`), or an empty one when the file does not exist yet."""
    if path is None:
        path = source_root() / MANIFEST_NAME
    if not path.exists():
        return {"version": 1}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ManifestError(f"cannot read {path.name}: {error}") from None
    if not isinstance(document, dict):
        raise ManifestError(f"{path.name} must hold a JSON object")
    return document


def _token_shaped(text: str) -> bool:
    """True when the packaged detector labels the text. Fails closed."""
    try:
        with as_file(data_dir("hooks/lib/secret-scan.sh")) as detector:
            result = subprocess.run(
                ["bash", str(detector), "label-stdin"],
                input=text,
                capture_output=True,
                text=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 1


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def nonempty_string_list(value: Any) -> bool:
    """A non-empty list of non-empty strings: a service's container command, or its match list."""
    return isinstance(value, list) and bool(value) and all(_nonempty_string(v) for v in value)


def deploy_platforms() -> Dict[str, DeployValidator]:
    """The registry of deploy platforms, read from the source root's private module on each call."""
    return load_deploy_platforms()


def _check_registration(entry: Dict[str, Any], label: str, runtimes_known: Sequence[str]) -> List[str]:
    """A foreign hook's registration map, checked for shape when the entry
    carries one.

    A wanted foreign hook is not always rendered: a hook captured from a
    fixture home and rendered by every adapter carries
    a registration naming that captured file per runtime, the way a service
    names its plist. A wanted foreign hook this repository does not render,
    only recognizes when another tool installs it, carries none, and its
    absence is not an error. Only a registration that is present is checked
    here. An unwanted entry is only ever found and removed by its match and
    is refused one entirely, in _check_section below.
    """
    registration = entry.get("registration")
    if not isinstance(registration, dict) or not registration:
        return [f"{label}: registration must be an object mapping each runtime to its captured file"]
    declared = entry.get("runtimes") if isinstance(entry.get("runtimes"), list) else []
    errors: List[str] = []
    for runtime, path in registration.items():
        if runtime not in runtimes_known:
            errors.append(f"{label}: registration: unknown runtime {runtime}")
        elif runtime not in declared:
            errors.append(f"{label}: registration: {runtime} is not in runtimes")
        if not _nonempty_string(path):
            errors.append(f"{label}: registration {runtime} must name the captured file, relative to the repository root")
    return errors


def _check_section(section: str, entry: Dict[str, Any], label: str, runtimes_known: Sequence[str] = ()) -> List[str]:
    """The checks one section adds to the common keys."""
    errors: List[str] = []
    for key in REQUIRED_STRINGS.get(section, ()):
        if not _nonempty_string(entry.get(key)):
            errors.append(f"{label}: needs {key}")
    if section == "foreign_hooks":
        if entry.get("wanted") is True:
            if "registration" in entry:
                errors.extend(_check_registration(entry, label, runtimes_known))
        elif "registration" in entry:
            errors.append(f"{label}: an unwanted foreign hook is found and removed by its match; it renders nothing")
    elif section == "third_party_skills":
        # A tool may install several skill directories under one entry, so the
        # entry may name them; the adapters' inventory matches the entry's own
        # name or any name here.
        skills = entry.get("skills")
        if "skills" in entry and not (
            isinstance(skills, list) and skills and all(_nonempty_string(name) for name in skills)
        ):
            errors.append(f"{label}: skills must be a list of directory names")
    elif section == "mcp_servers":
        # The adapters write the name as a TOML table key ([mcp_servers.<name>])
        # and a JSON key (the adapters' component renderer); a dot, space or
        # bracket would name a different table. A hosted connector is never
        # rendered, so it keeps the provider's name.
        if entry.get("hosted") is not True and not _MCP_NAME.match(str(entry.get("name") or "")):
            errors.append(f"{label}: name must be letters, digits, - or _ (it becomes a configuration key)")
        if "hosted" in entry and not isinstance(entry["hosted"], bool):
            errors.append(f"{label}: hosted must be true or false")
        # An account-level connector (hosted: true) is started by the
        # runtime's provider, not by a command or url this manifest names.
        if not entry.get("command") and not entry.get("url") and entry.get("hosted") is not True:
            errors.append(f"{label}: needs a command or a url")
        args = entry.get("args")
        if args is not None and not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
            errors.append(f"{label}: args must be a list of strings")
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            errors.append(f"{label}: env must be an object")
            env = {}
        for key, value in env.items():
            if not isinstance(value, str):
                errors.append(f"{label}: env {key} must be a string")
            elif not value.startswith("secret://") and _token_shaped(f"{key}={value}"):
                errors.append(f"{label}: env {key} holds a token-shaped literal; use a secret:// reference")
    elif section == "plugins":
        if "path" in entry:
            if not _nonempty_string(entry["path"]):
                errors.append(f"{label}: path must name the plugin file")
            if entry.get("runtimes") != ["opencode"]:
                errors.append(f"{label}: path is only for OpenCode file plugins; runtimes must be [\"opencode\"]")
        elif not _nonempty_string(entry.get("marketplace")):
            errors.append(f"{label}: needs marketplace")
    elif section == "services":
        if entry.get("kind") not in SERVICE_KINDS:
            errors.append(f"{label}: kind must be launchd or app")
        port = entry.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
            errors.append(f"{label}: port must be a port number from 1 to 65535")
        if "plist" in entry and not _nonempty_string(entry["plist"]):
            errors.append(f"{label}: plist must name its source under scripts/launchd/")
        if "installed" in entry and not isinstance(entry["installed"], bool):
            errors.append(f"{label}: installed must be true or false")
        # A container has no launchd: container is the argv, run from the
        # checkout, that a container supervisor runs there.
        if "container" in entry and not nonempty_string_list(entry["container"]):
            errors.append(f"{label}: container must be the command as a non-empty list of non-empty strings")
        # environment: non-secret defaults a launcher applies at start (e.g.
        # a cache-persistence flag). Checked into git, so a
        # secret:// reference or a token-shaped literal here is always wrong:
        # a service's secrets are declared only in the credential profiles
        # and resolved at launch.
        if "environment" in entry:
            environment = entry["environment"]
            if not isinstance(environment, dict):
                errors.append(f"{label}: environment must be an object of string environment variable defaults")
            else:
                for key, value in environment.items():
                    if not isinstance(value, str):
                        errors.append(f"{label}: environment {key} must be a string")
                    elif value.startswith("secret://"):
                        errors.append(f"{label}: environment {key} must not be a secret:// reference; declare it in the credential profiles instead")
                    elif _token_shaped(f"{key}={value}"):
                        errors.append(f"{label}: environment {key} holds a token-shaped literal; environment defaults are non-secret only")
        if "image" in entry:
            image = entry["image"]
            if not isinstance(image, str) or not image:
                errors.append(f"{label}: image must be a string")
            elif _IMAGE_DIGEST.search(image):
                pass
            else:
                tag = image.rsplit("/", 1)[-1]
                if ":" not in tag:
                    errors.append(f"{label}: image must be pinned to a version tag or digest, not {image!r}")
                elif tag.rsplit(":", 1)[-1] == "latest":
                    errors.append(f"{label}: image must be pinned to a version tag or digest, not latest")
        if entry.get("wanted") is False:
            matches = entry.get("match")
            if not (_nonempty_string(matches) or isinstance(matches, list) and matches and all(_nonempty_string(value) for value in matches)):
                errors.append(f"{label}: needs match, since an unwanted service is found and removed by it")
            if not _nonempty_string(entry.get("remove")):
                errors.append(f"{label}: needs remove, since an unwanted service is found and removed by it")
    elif section == "dependencies":
        if "version_command" in entry and not _nonempty_string(entry["version_command"]):
            errors.append(f"{label}: version_command must be a command string")
        # platform: this dependency's command exists only on one platform
        # (for example a keyring daemon that exists only in a Linux image).
        # Absent means every platform. The adapters' inventory skips a
        # mismatched entry so a Mac's own inventory does not report a
        # container-only tool missing.
        if "platform" in entry and entry["platform"] not in PLATFORMS:
            errors.append(f"{label}: platform must be darwin or linux")
        if "image" not in entry:
            errors.append(f"{label}: needs image, the source the container installs it from")
        else:
            errors.extend(_check_image(entry["image"], label))
    return errors


def _check_image(image: Any, label: str) -> List[str]:
    """The shape of one dependency's image source (IMAGE_KEYS)."""
    if not isinstance(image, dict):
        return [f"{label}: image must be an object with a method"]
    method = image.get("method")
    if method not in IMAGE_KEYS:
        return [f"{label}: image method must be one of {', '.join(IMAGE_KEYS)}"]
    errors: List[str] = []
    for key in image:
        if key != "method" and key not in IMAGE_KEYS[method]:
            errors.append(f"{label}: image unknown key {key}")
    if method == "base" and not _nonempty_string(image.get("from")):
        errors.append(f"{label}: image needs from")
    if method in ("apt", "npm") and not _nonempty_string(image.get("package")):
        errors.append(f"{label}: image needs package")
    if method in ("release", "source"):
        url = image.get("url")
        if not _nonempty_string(url) or "{version}" not in url:
            errors.append(f"{label}: image url must carry {{version}}")
    if method == "source":
        sha256 = image.get("sha256")
        if sha256 is None:
            errors.append(f"{label}: image source needs sha256")
        elif not (isinstance(sha256, str) and _SHA256.match(sha256)):
            errors.append(f"{label}: image sha256 must be 64 lowercase hex digits")
    if method == "release":
        if image.get("extract") not in IMAGE_EXTRACTS:
            errors.append(f"{label}: image extract must be tar, tar-flat, zip or zip-strip")
        strip = image.get("strip")
        if strip is not None:
            # isinstance before membership: Python reads True as equal to 1.
            if isinstance(strip, bool) or strip not in IMAGE_STRIPS:
                errors.append(f"{label}: image strip must be 0 or 1")
            elif image.get("extract") != "tar":
                errors.append(f"{label}: image strip is for a tar archive; a zip always unpacks into the bin directory")
        arch = image.get("arch")
        if arch is not None and not (
            isinstance(arch, dict)
            and sorted(arch) == sorted(IMAGE_ARCHES)
            and all(_nonempty_string(value) for value in arch.values())
        ):
            errors.append(f"{label}: image arch must map amd64 and arm64 to the vendor's names")
        if "binary" in image and not _nonempty_string(image["binary"]):
            errors.append(f"{label}: image binary must name the extracted executable")
        # Each architecture downloads its own archive, so each has its own sum.
        sums = image.get("sha256")
        if sums is not None and not (
            isinstance(sums, dict)
            and sorted(sums) == sorted(IMAGE_ARCHES)
            and all(isinstance(value, str) and _SHA256.match(value) for value in sums.values())
        ):
            errors.append(f"{label}: image release sha256 must map amd64 and arm64 to 64 lowercase hex digits")
    return errors


def _check_platform_differences(entries: Any) -> List[str]:
    """The shape of platform_differences (PLATFORM_DIFFERENCE_KEYS)."""
    if not isinstance(entries, list):
        return ["platform_differences must be a list"]
    errors: List[str] = []
    seen: set = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("platform_differences: every entry must be an object")
            continue
        name = entry.get("name")
        if not _nonempty_string(name):
            errors.append("platform_differences: an entry has no name")
            continue
        label = f"platform_differences {name}"
        if name in seen:
            errors.append(f"{label}: declared twice")
        seen.add(name)
        for key in entry:
            if key not in PLATFORM_DIFFERENCE_KEYS:
                errors.append(f"{label}: unknown key {key}")
        if not _nonempty_string(entry.get("description")):
            errors.append(f"{label}: needs description")
        if entry.get("platform") not in PLATFORMS:
            errors.append(f"{label}: platform must be darwin or linux")
        tests = entry.get("tests", [])
        if not (isinstance(tests, list) and all(_nonempty_string(path) for path in tests)):
            errors.append(f"{label}: tests must be a list of paths")
        paths = entry.get("paths", [])
        if not (isinstance(paths, list) and all(_nonempty_string(path) for path in paths)):
            errors.append(f"{label}: paths must be a list of manifest key patterns")
        else:
            for path in paths:
                if not path.startswith(PARITY_SCOPES):
                    errors.append(f"{label}: path {path} must start with home:, project: or runtime:")
    return errors


def _check_runtime_settings(runtime: str, block: Dict[str, Any]) -> List[str]:
    """The shape of runtime_settings.<runtime> (RUNTIME_SETTINGS_KEYS, C-28)."""
    label = f"runtime_settings {runtime}"
    errors: List[str] = []
    for key in block:
        if key not in RUNTIME_SETTINGS_KEYS:
            errors.append(f"{label}: unknown key {key}")
    for key in ("model", "subagent_model"):
        if key in block and (not _nonempty_string(block[key]) or (runtime != "claude" and block[key].strip().lower() in ("inherit", "default"))):
            errors.append(f"{label}: {key} must be a model name")
    aliases = block.get("aliases")
    if "aliases" in block and not (
        isinstance(aliases, dict) and all(_nonempty_string(k) and _nonempty_string(v) for k, v in aliases.items())
    ):
        errors.append(f"{label}: aliases must map each short name to a model name")
    if "settings" in block and not isinstance(block["settings"], dict):
        errors.append(f"{label}: settings must be an object")
    if "environment_plugins" in block:
        plugins = block["environment_plugins"]
        if runtime != "claude" or not isinstance(plugins, dict) or any(
            not _nonempty_string(environment)
            or not isinstance(keys, list)
            or any(not _nonempty_string(key) for key in keys)
            for environment, keys in plugins.items()
        ):
            errors.append(f"{label}: environment_plugins must map environments to plugin key lists for claude")
    if "provider" in block:
        provider = block["provider"]
        # A custom OpenAI-compatible provider is OpenCode's own config
        # concept (provider.<id>.npm/options/models, rendered by the opencode
        # adapter); no other runtime reads this key.
        if runtime != "opencode":
            errors.append(f"{label}: unknown key provider")
        elif not isinstance(provider, dict) or not all(isinstance(value, dict) for value in provider.values()):
            errors.append(f"{label}: provider must map each provider id to an object")
    return errors


def _check_deploy_variables(variables: Any, label: str) -> List[str]:
    """The variables of one deploy target: names, sealed references, plain literals."""
    if not isinstance(variables, list):
        return [f"{label}: variables must be a list"]
    errors: List[str] = []
    seen: set = set()
    for variable in variables:
        if not isinstance(variable, dict):
            errors.append(f"{label}: every variable must be an object")
            continue
        name = variable.get("name")
        if not isinstance(name, str) or not _VARIABLE_NAME.match(name):
            errors.append(f"{label}: variable name must be an environment variable name")
            continue
        where = f"{label}: variable {name}"
        if name in seen:
            errors.append(f"{where}: declared twice")
        seen.add(name)
        for key in variable:
            if key not in DEPLOY_VARIABLE_KEYS:
                errors.append(f"{where}: unknown key {key}")
        sealed = variable.get("sealed")
        if not isinstance(sealed, bool):
            errors.append(f"{where}: sealed must be true or false")
            continue
        secret = variable.get("secret")
        if sealed:
            if not (isinstance(secret, str) and secret.startswith("secret://") and len(secret) > len("secret://")):
                errors.append(f"{where}: a sealed variable needs a secret:// reference")
            if "value" in variable:
                errors.append(f"{where}: a sealed variable carries no value; it is set by hand")
        else:
            value = variable.get("value")
            if not isinstance(value, str):
                errors.append(f"{where}: a plain variable needs a string value")
            elif _token_shaped(f"{name}={value}"):
                errors.append(f"{label}: variable {name} holds a token-shaped literal; declare it sealed with a secret:// reference")
            if secret is not None:
                errors.append(f"{where}: only a sealed variable carries a secret reference")
    return errors


def _check_platform(entry: Dict[str, Any], label: str, platforms: Dict[str, DeployValidator]) -> List[str]:
    """The target's platform is registered, and its validator's own findings."""
    platform = entry.get("platform")
    if _nonempty_string(platform) and platform in platforms:
        return list(platforms[platform](entry, label))
    named = platform if _nonempty_string(platform) else "none"
    if platforms:
        return [f"{label}: platform must be one of {', '.join(sorted(platforms))}, not {named}"]
    module = "/".join(private_platforms_path().parts[-3:])
    return [f"{label}: platform {named} has no registered validator; none is registered, since {module}, which registers each platform, is absent"]


def _check_deploy(entries: Any, platforms: Optional[Dict[str, DeployValidator]] = None) -> List[str]:
    """The shape of the deploy section (DEPLOY_KEYS), each target's platform
    checked against deploy_platforms() or the registry passed in."""
    if not isinstance(entries, list):
        return ["deploy must be a list"]
    if platforms is None:
        platforms = deploy_platforms()
    errors: List[str] = []
    seen: set = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("deploy: every target must be an object")
            continue
        name = entry.get("name")
        if not _nonempty_string(name):
            errors.append("deploy: a target has no name")
            continue
        label = f"deploy {name}"
        if name in seen:
            errors.append(f"{label}: declared twice")
        seen.add(name)
        for key in entry:
            if key not in DEPLOY_KEYS:
                errors.append(f"{label}: unknown key {key}")
        errors.extend(_check_platform(entry, label, platforms))
        for key in ("service", "start_command", "restart_policy"):
            if not _nonempty_string(entry.get(key)):
                errors.append(f"{label}: needs {key}")
        source = entry.get("source")
        if not isinstance(source, dict) or any(key not in DEPLOY_SOURCE_KEYS for key in source):
            errors.append(f"{label}: source may hold only {', '.join(DEPLOY_SOURCE_KEYS)}")
        else:
            image, repo = source.get("image"), source.get("repo")
            if image and repo:
                errors.append(f"{label}: source must name an image or a repo, not both")
            elif not (_nonempty_string(image) or _nonempty_string(repo)):
                errors.append(f"{label}: source must name an image or a repo")
            elif image and "dockerfile" in source:
                errors.append(f"{label}: an image source has no dockerfile")
        healthcheck = entry.get("healthcheck")
        if healthcheck is not None and not (
            isinstance(healthcheck, dict)
            and all(key in DEPLOY_HEALTHCHECK_KEYS for key in healthcheck)
            and _nonempty_string(healthcheck.get("path"))
            and str(healthcheck["path"]).startswith("/")
        ):
            errors.append(f"{label}: healthcheck must be null or an object with a path")
        if not isinstance(entry.get("serverless"), bool):
            errors.append(f"{label}: serverless must be true or false")
        replicas = entry.get("replicas")
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
            errors.append(f"{label}: replicas must be a positive integer")
        port = entry.get("status_port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
            errors.append(f"{label}: status_port must be a port number from 1 to 65535")
        errors.extend(_check_deploy_variables(entry.get("variables", []), label))
    return errors


def _check_environments(environments: Any) -> List[str]:
    """The shape of environments (ENVIRONMENT_KEYS): name to {ref, description}."""
    if not isinstance(environments, dict):
        return ["environments must be an object keyed by environment"]
    errors: List[str] = []
    for name, entry in environments.items():
        label = f"environments {name}"
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be an object")
            continue
        for key in entry:
            if key not in ENVIRONMENT_KEYS:
                errors.append(f"{label}: unknown key {key}")
        if not _nonempty_string(entry.get("ref")):
            errors.append(f"{label}: needs ref, the branch it deploys from")
        if "description" in entry and not isinstance(entry["description"], str):
            errors.append(f"{label}: description must be a string")
    return errors


def _soak_days_valid(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _check_risk_tiers(risk_tiers: Any) -> List[str]:
    """The shape of promotion.risk_tiers (RISK_TIER_KEYS): tier name to
    {soak_days, paths, documentation, description} . paths is matched
    against a candidate's changed paths by a promotion plan: a trailing /
    is a directory prefix, a name holding a glob character is matched with
    fnmatch, anything else matched exactly; a path matching more than one
    tier takes the longer soak. At most one tier may carry
    documentation: true, the tier whose paths, covering every changed path,
    exempt a candidate from the 1-day floor."""
    if not isinstance(risk_tiers, dict):
        return ["promotion: risk_tiers must be an object keyed by tier name"]
    errors: List[str] = []
    documented = 0
    for name, tier in risk_tiers.items():
        if not _nonempty_string(name):
            errors.append("promotion: risk_tiers has an entry with no name")
            continue
        label = f"promotion: risk_tiers {name}"
        if not isinstance(tier, dict):
            errors.append(f"{label}: must be an object")
            continue
        for key in tier:
            if key not in RISK_TIER_KEYS:
                errors.append(f"{label}: unknown key {key}")
        if not _soak_days_valid(tier.get("soak_days")):
            errors.append(f"{label}: soak_days must be a number of days, 0 or more")
        paths = tier.get("paths")
        if not (isinstance(paths, list) and paths and all(_nonempty_string(path) for path in paths)):
            errors.append(f"{label}: paths must be a non-empty list of strings")
        if "documentation" in tier:
            if not isinstance(tier["documentation"], bool):
                errors.append(f"{label}: documentation must be true or false")
            elif tier["documentation"]:
                documented += 1
        if "description" in tier and not isinstance(tier["description"], str):
            errors.append(f"{label}: description must be a string")
    if documented > 1:
        errors.append("promotion: risk_tiers: only one tier may set documentation: true")
    return errors


def _check_promotion(promotion: Any) -> List[str]:
    """The shape of promotion (PROMOTION_KEYS): {soak_days, risk_tiers, description}."""
    if not isinstance(promotion, dict):
        return ["promotion must be an object"]
    errors = [f"promotion: unknown key {key}" for key in promotion if key not in PROMOTION_KEYS]
    if not _soak_days_valid(promotion.get("soak_days")):
        errors.append("promotion: soak_days must be a number of days, 0 or more")
    if "description" in promotion and not isinstance(promotion["description"], str):
        errors.append("promotion: description must be a string")
    if "risk_tiers" in promotion:
        errors.extend(_check_risk_tiers(promotion["risk_tiers"]))
    return errors


def _check_soak_override(value: Any, label: str) -> List[str]:
    """The shape of a plain soak override (SOAK_OVERRIDE_KEYS): {soak_days,
    description}, no risk_tiers. Used for a project's promotion.json (the
    per-project soak) and a loadout's promotion key (the per-feature soak,
    S-05); risk_tiers is declared once, in components.json's promotion block,
    never per project or per feature."""
    if not isinstance(value, dict):
        return [f"{label} must be an object"]
    errors = [f"{label}: unknown key {key}" for key in value if key not in SOAK_OVERRIDE_KEYS]
    if not _soak_days_valid(value.get("soak_days")):
        errors.append(f"{label}: soak_days must be a number of days, 0 or more")
    if "description" in value and not isinstance(value["description"], str):
        errors.append(f"{label}: description must be a string")
    return errors


def soak_days(document: Dict[str, Any]) -> float:
    """The promotion soak the manifest declares, DEFAULT_SOAK_DAYS when it declares none."""
    promotion = document.get("promotion")
    value = promotion.get("soak_days") if isinstance(promotion, dict) else None
    return value if _soak_days_valid(value) else DEFAULT_SOAK_DAYS


def _check_entry(section: str, entry: Any, seen: set, runtimes_known: Sequence[str]) -> List[str]:
    if not isinstance(entry, dict):
        return [f"{section}: every entry must be an object"]
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        return [f"{section}: an entry has no name"]
    label = f"{section} {name}"
    errors: List[str] = []
    if name in seen:
        errors.append(f"{label}: declared twice")
    seen.add(name)
    allowed = set(COMMON_KEYS) | set(SECTION_KEYS[section])
    for key in entry:
        if key not in allowed:
            errors.append(f"{label}: unknown key {key}")
    runtimes = entry.get("runtimes")
    if not isinstance(runtimes, list) or not runtimes:
        errors.append(f"{label}: runtimes must be a non-empty list")
    else:
        for runtime in runtimes:
            if runtime not in runtimes_known:
                errors.append(f"{label}: unknown runtime {runtime}")
    if not isinstance(entry.get("owner"), str) or not entry.get("owner"):
        errors.append(f"{label}: owner must name this repository or the tool that installs it")
    if not isinstance(entry.get("wanted"), bool):
        errors.append(f"{label}: wanted must be true or false")
    if "description" in entry and not isinstance(entry["description"], str):
        errors.append(f"{label}: description must be a string")
    errors.extend(_check_section(section, entry, label, runtimes_known))
    return errors


def validate(document: Dict[str, Any], adapters_dir: Optional[pathlib.Path] = None) -> List[str]:
    """Every problem in the manifest; empty when it is well formed.

    Runtime names are checked against the adapters' registry; adapters_dir
    points it at another adapter directory (a test fixture).
    """
    runtimes_known = known_runtimes(adapters_dir)
    errors: List[str] = []
    if document.get("version") != 1:
        errors.append("version must be 1")
    for key in document:
        if key not in TOP_LEVEL:
            errors.append(f"unknown section {key}")
    errors.extend(f"budgets: {problem}" for problem in budgets.validate(document.get("budgets")))
    errors.extend(validate_autonomy(document.get("autonomy")))
    settings = document.get("runtime_settings")
    if settings is not None and not isinstance(settings, dict):
        errors.append("runtime_settings must be an object keyed by runtime")
    elif settings:
        for runtime, value in settings.items():
            if runtime not in runtimes_known:
                errors.append(f"runtime_settings: unknown runtime {runtime}")
            elif not isinstance(value, dict):
                errors.append(f"runtime_settings: {runtime} must be an object")
            else:
                errors.extend(_check_runtime_settings(runtime, value))
    if "platform_differences" in document:
        errors.extend(_check_platform_differences(document["platform_differences"]))
    if "environments" in document:
        errors.extend(_check_environments(document["environments"]))
    if "promotion" in document:
        errors.extend(_check_promotion(document["promotion"]))
    if "deploy" in document:
        errors.extend(_check_deploy(document["deploy"]))
    for section in SECTIONS:
        entries = document.get(section)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"{section} must be a list")
            continue
        seen: set = set()
        for entry in entries:
            errors.extend(_check_entry(section, entry, seen, runtimes_known))
    return errors


def declared(
    document: Dict[str, Any], section: str, runtime: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """Name to entry for each wanted entry in a section, optionally for one runtime."""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in document.get(section) or []:
        if not isinstance(entry, dict) or entry.get("wanted") is not True:
            continue
        if runtime and runtime not in (entry.get("runtimes") or []):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            out[name] = entry
    return out


# ---------------------------------------------------------------------------
# The service and port check, and the service installer . A wanted launchd service
# is checked loaded and, when it declares a port, holding it; any other
# process on that port is a port conflict naming its executable, and the
# removal line of the wanted: false entry it matches. A wanted: false entry
# found present on its own is returned. The installer copies a declared plist
# into LaunchAgents and bootstraps it; it never stops a competitor, it prints
# its removal line.
#
# launchctl print lists a job's environment, so its output is parsed for the
# top-level state and pid lines only and never printed.
# ---------------------------------------------------------------------------

def launch_agents_dir() -> pathlib.Path:
    """The user's LaunchAgents directory under the agentrc home."""
    return home() / "Library" / "LaunchAgents"


PROBE_TIMEOUT = 10
MISSING_COMMAND = 127
LAUNCHCTL_NOT_FOUND = 113
PROBE_FAILED = -1
_TOP_LEVEL_PID = re.compile(r"^\tpid = (\d+)$", re.MULTILINE)
_TOP_LEVEL_STATE = re.compile(r"^\tstate = (.+)$", re.MULTILINE)


class ProbeUnavailable(RuntimeError):
    """A command the check reads from is missing or failed, so nothing is inferred."""


@dataclasses.dataclass(frozen=True)
class Job:
    state: str
    pid: Optional[int]


@dataclasses.dataclass(frozen=True)
class ServiceFinding:
    service: str
    kind: str
    message: str
    failing: bool

    def render(self) -> str:
        return f"{self.service}: {self.kind}: {self.message}"


def _run(argv: Sequence[str]) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except FileNotFoundError:
        return MISSING_COMMAND, "", ""
    except (OSError, subprocess.SubprocessError) as error:
        # Never 1: lsof exits 1 for "nothing listens", and a timed-out probe
        # must read as unreadable, not as a free port.
        return PROBE_FAILED, "", type(error).__name__
    return result.returncode, result.stdout, result.stderr


class Probe:
    """launchctl, lsof and ps, each a command a test can replace, or one
    runner answering every argv."""

    def __init__(
        self,
        *,
        launchctl: str = "launchctl",
        lsof: str = "lsof",
        ps: str = "ps",
        uid: Optional[int] = None,
        runner=None,
    ) -> None:
        self.launchctl_command = launchctl
        self.lsof_command = lsof
        self.ps_command = ps
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner or _run
        self._processes: Optional[List[Tuple[int, str]]] = None

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def run(self, argv: Sequence[str]) -> Tuple[int, str, str]:
        code, out, err = self.runner(list(argv))
        if code == MISSING_COMMAND:
            raise ProbeUnavailable(f"{argv[0]} is not available on this machine")
        return code, out, err

    def launchctl(self, *args: str) -> Tuple[int, str, str]:
        return self.run([self.launchctl_command, *args])

    def job(self, label: str) -> Optional[Job]:
        """The loaded job's state and pid, or None when it is not loaded.
        launchctl print exits 113 for a label it does not know; any other
        failure is unreadable, never taken as not loaded."""
        code, out, _ = self.launchctl("print", f"{self.domain}/{label}")
        if code == LAUNCHCTL_NOT_FOUND:
            return None
        if code != 0:
            raise ProbeUnavailable(f"launchctl print {self.domain}/{label} failed with exit {code}")
        state = _TOP_LEVEL_STATE.search(out)
        pid = _TOP_LEVEL_PID.search(out)
        return Job(state.group(1).strip() if state else "unknown", int(pid.group(1)) if pid else None)

    def listeners(self, port: int) -> List[int]:
        """The pids listening on a TCP port. lsof exits 1 when there are
        none; any other failure is unreadable, never taken as a free port."""
        code, out, _ = self.run([self.lsof_command, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"])
        if code not in (0, 1):
            raise ProbeUnavailable(f"lsof for port {port} failed with exit {code}")
        return sorted({int(line[1:]) for line in out.splitlines() if line[:1] == "p" and line[1:].isdigit()})

    def executable(self, pid: int) -> str:
        code, out, _ = self.run([self.ps_command, "-o", "comm=", "-p", str(pid)])
        return out.strip() if code == 0 and out.strip() else "an unknown process"

    def processes(self) -> List[Tuple[int, str]]:
        """(pid, executable) for every process, read once per check."""
        if self._processes is None:
            code, out, _ = self.run([self.ps_command, "-axo", "pid=,comm="])
            if code != 0:
                raise ProbeUnavailable(f"ps -axo failed with exit {code}")
            rows: List[Tuple[int, str]] = []
            for line in out.splitlines():
                pid, _, command = line.strip().partition(" ")
                if pid.isdigit():
                    rows.append((int(pid), command.strip()))
            self._processes = rows
        return self._processes


def _install_hint(name: str) -> str:
    return f"install with: python3 -m agentrc.components --install-service {name}"


def _competitor_for(pid: int, exe: str, unwanted: List[Dict[str, Any]], jobs: Dict[str, Optional[Job]]) -> Optional[Dict[str, Any]]:
    """The wanted: false entry a port holder is: its launchd job, or its executable."""
    for entry in unwanted:
        job = jobs.get(entry["name"])
        if entry.get("kind") == "launchd" and job is not None and job.pid == pid:
            return entry
        if any(match in exe for match in _service_matches(entry)):
            return entry
    return None


def _service_matches(entry: Dict[str, Any]) -> List[str]:
    match = entry.get("match")
    if _nonempty_string(match):
        return [match]
    if isinstance(match, list):
        return [value for value in match if _nonempty_string(value)]
    return []


def _unwanted_present(entry: Dict[str, Any], probe: Probe, jobs: Dict[str, Optional[Job]]) -> bool:
    if entry.get("kind") == "launchd":
        return jobs.get(entry["name"]) is not None
    return any(match in command for _, command in probe.processes() for match in _service_matches(entry))


def _port_findings(
    entry: Dict[str, Any],
    job: Optional[Job],
    port: int,
    probe: Probe,
    unwanted: List[Dict[str, Any]],
    jobs: Dict[str, Optional[Job]],
    named: set,
) -> List[ServiceFinding]:
    """Whether the owner holds its declared port, and who does instead.
    Adds each competitor it names to named, so it is not reported twice."""
    name, label = entry["name"], entry.get("label")
    owner_pid = job.pid if job is not None else None
    holders = probe.listeners(port)
    others = [pid for pid in holders if pid != owner_pid]
    findings: List[ServiceFinding] = []
    if owner_pid is not None and owner_pid in holders:
        findings.append(ServiceFinding(name, "loaded", f"loaded as {label} (pid {owner_pid}), holds port {port}", False))
    elif owner_pid is not None and not others:
        findings.append(ServiceFinding(name, "not listening", f"{label} (pid {owner_pid}) is running but nothing listens on port {port}", True))
    owner = f"{label} (pid {owner_pid})" if owner_pid is not None else label
    for pid in others:
        exe = probe.executable(pid)
        message = f"port {port} is held by {exe} (pid {pid}), not {owner}"
        competitor = _competitor_for(pid, exe, unwanted, jobs)
        if competitor is not None:
            named.add(competitor["name"])
            message += f"; it is {competitor['name']}, declared unwanted; remove it with: {competitor['remove']}"
        findings.append(ServiceFinding(name, "port conflict", message, True))
    return findings


def check_services(document: Dict[str, Any], probe: Probe, *, launch_agents: Optional[pathlib.Path] = None) -> List[ServiceFinding]:
    """Every service finding, in manifest order. A missing probe command
    makes the whole check one unreadable finding, never an empty one."""
    try:
        return _check_services(document, probe, launch_agents or launch_agents_dir())
    except ProbeUnavailable as error:
        return [ServiceFinding("launchd", "unreadable", f"the service check cannot run: {error}", True)]


def _check_services(document: Dict[str, Any], probe: Probe, launch_agents: pathlib.Path) -> List[ServiceFinding]:
    entries = [e for e in document.get("services") or [] if isinstance(e, dict) and _nonempty_string(e.get("name"))]
    wanted = [e for e in entries if e.get("wanted") is True]
    unwanted = [e for e in entries if e.get("wanted") is False and _service_matches(e)]
    jobs: Dict[str, Optional[Job]] = {
        e["name"]: probe.job(e["label"])
        for e in wanted + unwanted
        if e.get("kind") == "launchd" and _nonempty_string(e.get("label"))
    }
    findings: List[ServiceFinding] = []
    named: set = set()
    for entry in wanted:
        if entry.get("kind") != "launchd":
            continue
        name, label, port = entry["name"], entry.get("label"), entry.get("port")
        job = jobs.get(name)
        deferred = (
            entry.get("installed") is False
            and not (launch_agents / f"{label}.plist").is_file()
            and not (launch_agents / f".{label}.installed").is_file()
        )
        if job is None:
            if deferred:
                findings.append(ServiceFinding(name, "not installed", f"declared installed: false; {_install_hint(name)}", False))
            else:
                findings.append(ServiceFinding(name, "not loaded", f"{label} is not loaded in {probe.domain}; {_install_hint(name)}", True))
        elif job.pid is None and isinstance(port, int):
            findings.append(ServiceFinding(name, "not running", f"{label} is loaded but not running (state {job.state})", True))
        if not isinstance(port, int):
            if job is not None:
                detail = f" (pid {job.pid})" if job.pid is not None else f" (state {job.state})"
                findings.append(ServiceFinding(name, "loaded", f"loaded as {label}{detail}", False))
        elif not (job is None and deferred):
            findings.extend(_port_findings(entry, job, port, probe, unwanted, jobs, named))
    for entry in unwanted:
        if entry["name"] not in named and _unwanted_present(entry, probe, jobs):
            findings.append(
                ServiceFinding(
                    entry["name"],
                    "returned",
                    f"declared unwanted (installed by {entry.get('owner')}) and present; remove it with: {entry.get('remove')}",
                    True,
                )
            )
    return findings


def service_report_lines(findings: Sequence[ServiceFinding]) -> List[str]:
    """The findings as Markdown for the weekly audit."""
    if not findings:
        return ["No services are declared in components.json."]
    services = len({f.service for f in findings})
    action = sum(1 for f in findings if f.failing)
    lines = [
        f"Services reported: {services}. Findings that need action: {action}.",
        "",
        "| service | state | detail |",
        "|---|---|---|",
    ]
    for finding in findings:
        lines.append(f"| {finding.service} | {finding.kind} | {finding.message.replace('|', '&#124;')} |")
    return lines


def install_service(
    document: Dict[str, Any],
    name: str,
    *,
    root: Optional[pathlib.Path] = None,
    launch_agents: Optional[pathlib.Path] = None,
    probe: Optional[Probe] = None,
    platform: Optional[str] = None,
) -> int:
    """Copy a declared service's plist into launch_agents and bootstrap it.

    launchd exists only on the Mac. Elsewhere this refuses before writing
    anything: a container runs a service with a container command through the
    deploy that names it. root defaults to the source root and launch_agents
    to launch_agents_dir()."""
    root = source_root() if root is None else root
    launch_agents = launch_agents_dir() if launch_agents is None else launch_agents
    if (platform or sys.platform) != "darwin":
        print(
            "components: install-service: launchd services install only on macOS; "
            "a service with a container command runs through the deploy that names it",
            file=sys.stderr,
        )
        return 1
    probe = probe or Probe()
    entry = next((e for e in document.get("services") or [] if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None:
        print(f"components: install-service: no service named {name} in components.json", file=sys.stderr)
        return 1
    if entry.get("wanted") is not True or entry.get("kind") != "launchd" or not _nonempty_string(entry.get("plist")):
        print(f"components: install-service: {name} is not a wanted launchd service with a plist", file=sys.stderr)
        return 1
    label = entry.get("label")
    source = root / entry["plist"]
    try:
        with source.open("rb") as handle:
            plist_label = plistlib.load(handle).get("Label")
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
        print(f"components: install-service: cannot read {entry['plist']}: {type(error).__name__}", file=sys.stderr)
        return 1
    if plist_label != label:
        print(f"components: install-service: {entry['plist']} carries Label {plist_label}, not the declared {label}", file=sys.stderr)
        return 1
    destination = launch_agents / f"{label}.plist"
    # A backup of whatever the destination held before this call, and the
    # label's job at that moment, so a failed replacement can be undone.
    try:
        previous_plist = destination.read_bytes()
    except FileNotFoundError:
        previous_plist = None
    except OSError as error:
        print(f"components: install-service: cannot read {destination}: {type(error).__name__}", file=sys.stderr)
        return 1
    try:
        previous_job = probe.job(label)
    except ProbeUnavailable as error:
        print(f"components: install-service: {error}", file=sys.stderr)
        return 1
    if previous_job is not None and previous_plist is None:
        print(
            f"components: install-service: cannot replace loaded {label} without its previous plist at {destination}",
            file=sys.stderr,
        )
        return 1

    def restore_previous(restart: bool) -> None:
        """Put the previous plist back (or remove this call's fresh copy
        when there was none), and reload the previous job if one was
        running before this call started."""
        try:
            if previous_plist is None:
                destination.unlink(missing_ok=True)
            else:
                destination.write_bytes(previous_plist)
        except OSError as error:
            print(f"components: install-service: cannot restore {destination}: {type(error).__name__}", file=sys.stderr)
            return
        if restart:
            try:
                code, _, err = probe.launchctl("bootstrap", probe.domain, str(destination))
                if code != 0:
                    print(f"components: install-service: cannot restart {label}: {err.strip()}", file=sys.stderr)
            except ProbeUnavailable as error:
                print(f"components: install-service: cannot restart {label}: {error}", file=sys.stderr)

    # Copy first, so a destination that cannot hold the plist fails before
    # a loaded copy of the job is booted out.
    try:
        launch_agents.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    except OSError as error:
        restore_previous(False)
        print(f"components: install-service: cannot write {destination}: {type(error).__name__}", file=sys.stderr)
        return 1
    bootstrapped = False
    try:
        if previous_job is not None:
            code, _, err = probe.launchctl("bootout", f"{probe.domain}/{label}")
            if code != 0:
                restore_previous(False)
                print(f"components: install-service: launchctl bootout {label} failed: {err.strip()}", file=sys.stderr)
                return 1
        code, _, err = probe.launchctl("bootstrap", probe.domain, str(destination))
        if code != 0:
            restore_previous(previous_job is not None)
            print(f"components: install-service: launchctl bootstrap {destination} failed: {err.strip()}", file=sys.stderr)
            return 1
        bootstrapped = True
        job = probe.job(label)
    except ProbeUnavailable as error:
        if bootstrapped:
            # The replacement may or may not actually be loaded; stop it
            # defensively before restoring, whether or not this was a
            # first-time install (there was no previous job to boot out).
            try:
                code, _, err = probe.launchctl("bootout", f"{probe.domain}/{label}")
                if code != 0:
                    print(f"components: install-service: cannot stop failed replacement {label}: {err.strip()}", file=sys.stderr)
            except ProbeUnavailable as stop_error:
                print(f"components: install-service: cannot stop failed replacement {label}: {stop_error}", file=sys.stderr)
        # Restore even when the exception came from the bootout or bootstrap
        # call itself (bootstrapped still False): the plist on disk was
        # already overwritten by the copy above, and a previous job that
        # never actually got booted out is unaffected by reloading it again.
        restore_previous(previous_job is not None)
        print(f"components: install-service: {error}", file=sys.stderr)
        return 1
    if job is None:
        restore_previous(previous_job is not None)
        print(f"components: install-service: {label} is not loaded after bootstrap", file=sys.stderr)
        return 1
    try:
        (launch_agents / f".{label}.installed").touch(exist_ok=True)
    except OSError as error:
        print(f"components: install-service: cannot record installation of {label}: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"components: install-service: copied {entry['plist']} to {destination} and bootstrapped it in {probe.domain}")
    # The installed service and the declared competitors for its port, whose
    # removal lines are printed and never run.
    port = entry.get("port")
    competitors = [
        e
        for e in document.get("services") or []
        if isinstance(e, dict) and e.get("wanted") is False and port is not None and e.get("port") == port
    ]
    findings = check_services({"services": [entry] + competitors}, probe, launch_agents=launch_agents)
    for finding in findings:
        print(f"components: services: {finding.render()}")
    return 1 if any(finding.failing for finding in findings) else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentrc components", description="validate components.json")
    parser.add_argument("--manifest", type=pathlib.Path, default=None, help="manifest to read (default: <source root>/components.json)")
    parser.add_argument("--list", action="store_true", help="print what is declared")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--services", action="store_true", help="check each declared service and its port")
    action.add_argument("--install-service", metavar="NAME", help="copy a declared plist into LaunchAgents and bootstrap it")
    parser.add_argument("--launch-agents-dir", type=pathlib.Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=pathlib.Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--launchctl", default="launchctl", help=argparse.SUPPRESS)
    parser.add_argument("--lsof", default="lsof", help=argparse.SUPPRESS)
    parser.add_argument("--ps", default="ps", help=argparse.SUPPRESS)
    parser.add_argument("--platform", default=sys.platform, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    manifest_path = args.manifest or source_root(args.root) / MANIFEST_NAME
    try:
        document = load(manifest_path)
    except ManifestError as error:
        print(f"components: {error}", file=sys.stderr)
        return 1
    errors = validate(document)
    for error in errors:
        print(f"components: {error}", file=sys.stderr)
    if errors and (args.services or args.install_service):
        return 1
    probe = Probe(launchctl=args.launchctl, lsof=args.lsof, ps=args.ps)
    if args.install_service:
        return install_service(
            document, args.install_service, root=args.root, launch_agents=args.launch_agents_dir, probe=probe, platform=args.platform
        )
    if args.services:
        findings = check_services(document, probe, launch_agents=args.launch_agents_dir)
        if not findings:
            print("components: services: none declared")
        for finding in findings:
            print(f"components: services: {finding.render()}")
        return 1 if any(f.failing for f in findings) else 0
    if args.list:
        for section in SECTIONS:
            for name, entry in sorted(declared(document, section).items()):
                print(f"{section}\t{name}\t{','.join(entry.get('runtimes') or [])}")
    if not errors:
        print(f"components: {manifest_path.name} is valid")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
