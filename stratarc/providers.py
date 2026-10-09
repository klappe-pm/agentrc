"""Inference providers as data: one JSON file per provider under `<home>/.stratarc/providers/`.

A provider file names an endpoint, the models it serves and, optionally, a `secret://` reference. It never holds a secret value: `validate` rejects any string a token-shaped detector matches and any secret field that is not a `secret://` reference. The bundled schema is `data/schema/provider.schema.json`; `validate` applies the same rules without needing a schema library at run time.

`test` takes a probe callable so tests never touch the network. The probe sees the endpoint, the secret reference (never a value) and the model ids.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from stratarc import home_layout as layout
from stratarc.home_layout import Conflict, InvalidInput, ToolError, Unavailable

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
SECRET_PATTERN = re.compile(r"^secret://[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$")
ALLOWED_KEYS = {"schema_version", "name", "endpoint", "models", "secret", "description"}
MODEL_KEYS = {"id", "display_name"}

Detector = Callable[[str], bool]


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str = ""


Probe = Callable[[str, str | None, Sequence[str]], "ProbeResult | bool"]


def _default_detector(text: str) -> bool:
    from stratarc.components import _token_shaped

    return _token_shaped(text)


def _strings(value: Any, path: str = ""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def validate(doc: Any, *, detector: Detector | None = None) -> list[str]:
    """Every problem in a provider document; an empty list means it is valid. Messages never repeat a value."""
    if not isinstance(doc, dict):
        return ["a provider must be a JSON object"]
    detect = detector or _default_detector
    errors: list[str] = []
    for key in sorted(set(doc) - ALLOWED_KEYS):
        errors.append(f"unknown key {key!r}")
    version = doc.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("schema_version must be a positive integer")
    name = doc.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        errors.append("name must match ^[a-z][a-z0-9-]*$")
    endpoint = doc.get("endpoint")
    if not isinstance(endpoint, str):
        errors.append("endpoint must be a string")
    else:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            errors.append("endpoint must be an http or https URL")
        elif parsed.username or parsed.password:
            errors.append("endpoint must not carry credentials; use a secret:// reference")
        elif parsed.query:
            errors.append("endpoint must not carry a query string; credentials belong in a secret:// reference")
    models = doc.get("models")
    if not isinstance(models, list) or not models:
        errors.append("models must be a non-empty list")
    else:
        seen: set[str] = set()
        for index, model in enumerate(models):
            label = f"models[{index}]"
            if not isinstance(model, dict):
                errors.append(f"{label} must be an object with an id")
                continue
            for key in sorted(set(model) - MODEL_KEYS):
                errors.append(f"{label}: unknown key {key!r}")
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                errors.append(f"{label}.id must be a non-empty string")
            elif model_id in seen:
                errors.append(f"{label}.id is a duplicate")
            else:
                seen.add(model_id)
            if "display_name" in model and not isinstance(model["display_name"], str):
                errors.append(f"{label}.display_name must be a string")
    if "description" in doc and not isinstance(doc["description"], str):
        errors.append("description must be a string")
    secret = doc.get("secret")
    if "secret" in doc and secret is not None:
        if not isinstance(secret, str) or not SECRET_PATTERN.match(secret):
            errors.append("secret must be a secret://<namespace>/<key> reference, never a value")
    # The detector runs over every string except a well-formed reference.
    lines = [f"{path}={value}" for path, value in _strings(doc) if not (path == "secret" and SECRET_PATTERN.match(value))]
    if lines and detect("\n".join(lines)):
        for path, value in _strings(doc):
            if path == "secret" and SECRET_PATTERN.match(value):
                continue
            if detect(f"{path}={value}"):
                errors.append(f"{path} holds a token-shaped literal; use a secret:// reference")
    return errors


def _require_valid(doc: Any, detector: Detector | None) -> None:
    errors = validate(doc, detector=detector)
    if errors:
        raise InvalidInput(
            "The provider is not valid: " + "; ".join(errors) + ".",
            hint="Fix the listed fields. Secrets are referenced as secret://<namespace>/<key>.",
            code="provider-invalid",
        )


def path_for(name: str) -> Path:
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise InvalidInput(f"The provider name {name!r} is not valid.", hint="Use lowercase letters, digits and hyphens, starting with a letter.", param="name", code="provider-name")
    return layout.providers_dir() / f"{name}.json"


def build(name: str, endpoint: str, models: Sequence[str | Mapping[str, Any]], *, secret: str | None = None, description: str | None = None) -> dict[str, Any]:
    """A provider document from loose arguments; model ids may be strings."""
    doc: dict[str, Any] = {
        "schema_version": layout.SCHEMA_VERSION,
        "name": name,
        "endpoint": endpoint,
        "models": [{"id": m} if isinstance(m, str) else dict(m) for m in models],
    }
    if secret is not None:
        doc["secret"] = secret
    if description is not None:
        doc["description"] = description
    return doc


def _render(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def show(name: str) -> dict[str, Any]:
    path = path_for(name)
    layout.check_not_newer(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InvalidInput(f"The provider {name} is not registered.", hint="List them with `stratarc provider list`.", param="name", code="provider-unknown") from None
    except (OSError, ValueError) as error:
        raise InvalidInput(f"The provider file {path.name} cannot be read: {error}", hint="Fix or remove the file.", param="name", code="provider-unreadable") from None
    return doc


def list_providers() -> list[dict[str, Any]]:
    """Every registered provider, by name. A file that cannot be read is skipped rather than hiding the rest."""
    directory = layout.providers_dir()
    found: list[dict[str, Any]] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            found.append(doc)
    return found


def add(doc: dict[str, Any], *, probe: Probe | None = None, replace: bool = False, detector: Detector | None = None) -> Path:
    """Validate, optionally probe, and write a new provider file. Refuses an existing name unless `replace`."""
    _require_valid(doc, detector)
    path = path_for(doc["name"])
    if path.exists() and not replace:
        raise Conflict(f"The provider {doc['name']} is already registered.", hint="Change it with `stratarc provider edit`, or remove it first.", param="name", code="provider-exists")
    if probe is not None:
        result = run_probe(doc, probe)
        if not result.ok:
            raise Unavailable(f"The provider {doc['name']} did not answer its test: {result.detail or 'no detail'}.", hint="Check the endpoint, or add it without the test.", param="endpoint", code="provider-unreachable")
    layout.ensure_layout()
    layout.safe_write(path, _render(doc))
    return path


def edit(name: str, *, endpoint: str | None = None, secret: str | None = None, clear_secret: bool = False, description: str | None = None, add_models: Sequence[str] = (), remove_models: Sequence[str] = (), detector: Detector | None = None) -> dict[str, Any]:
    """Change fields of a registered provider; the result is validated before it replaces the file."""
    doc = show(name)
    if endpoint is not None:
        doc["endpoint"] = endpoint
    if secret is not None:
        doc["secret"] = secret
    if clear_secret:
        doc.pop("secret", None)
    if description is not None:
        doc["description"] = description
    models = [dict(m) for m in doc.get("models", []) if isinstance(m, dict)]
    for model_id in remove_models:
        if not any(m.get("id") == model_id for m in models):
            raise InvalidInput(f"The provider {name} does not serve the model {model_id}.", hint="List its models with `stratarc provider show`.", param="model", code="model-unknown")
        models = [m for m in models if m.get("id") != model_id]
    for model_id in add_models:
        models.append({"id": model_id})
    doc["models"] = models
    _require_valid(doc, detector)
    layout.safe_write(path_for(name), _render(doc))
    return doc


def remove(name: str) -> Path | None:
    """Delete a provider file after keeping a backup; returns the backup path."""
    path = path_for(name)
    if not path.exists():
        raise InvalidInput(f"The provider {name} is not registered.", hint="List them with `stratarc provider list`.", param="name", code="provider-unknown")
    layout.check_not_newer(path)
    backup = layout._backup(path, None)
    path.unlink()
    return backup


def run_probe(doc: Mapping[str, Any], probe: Probe) -> ProbeResult:
    outcome = probe(doc["endpoint"], doc.get("secret"), [m["id"] for m in doc.get("models", [])])
    if isinstance(outcome, ProbeResult):
        return outcome
    return ProbeResult(bool(outcome), "" if outcome else "the probe reported failure")


def test(name: str, probe: Probe) -> ProbeResult:
    """Run `probe` against a registered provider. The probe is injected so callers and tests choose whether anything touches the network."""
    return run_probe(show(name), probe)


test.__test__ = False  # not a pytest test when imported into a test module


def http_probe(endpoint: str, secret: str | None, models: Sequence[str]) -> ProbeResult:
    """Default probe: one GET of the endpoint. Any HTTP answer counts as reachable; the secret is not resolved or sent."""
    request = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 (scheme checked by validate)
            return ProbeResult(True, f"HTTP {response.status}")
    except urllib.error.HTTPError as error:
        return ProbeResult(True, f"HTTP {error.code}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        return ProbeResult(False, str(getattr(error, "reason", error)))


# ---------------------------------------------------------------------------
# command line: `provider <verb>`
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stratarc provider", description="Manage inference providers.")
    verbs = parser.add_subparsers(dest="verb", required=True)

    def common(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--json", action="store_true", help="print one JSON envelope instead of text")
        return sub

    common(verbs.add_parser("list", help="list registered providers"))
    for verb in ("show", "remove", "test"):
        sub = common(verbs.add_parser(verb))
        sub.add_argument("name")
    add_parser = common(verbs.add_parser("add", help="register a provider"))
    add_parser.add_argument("name")
    add_parser.add_argument("--endpoint", required=True)
    add_parser.add_argument("--model", action="append", default=[], dest="models", metavar="ID")
    add_parser.add_argument("--secret", help="a secret://<namespace>/<key> reference")
    add_parser.add_argument("--description")
    add_parser.add_argument("--no-test", action="store_true", help="write without probing the endpoint")
    add_parser.add_argument("--replace", action="store_true")
    edit_parser = common(verbs.add_parser("edit", help="change a registered provider"))
    edit_parser.add_argument("name")
    edit_parser.add_argument("--endpoint")
    edit_parser.add_argument("--secret")
    edit_parser.add_argument("--clear-secret", action="store_true")
    edit_parser.add_argument("--description")
    edit_parser.add_argument("--add-model", action="append", default=[], dest="add_models", metavar="ID")
    edit_parser.add_argument("--remove-model", action="append", default=[], dest="remove_models", metavar="ID")
    return parser


def _describe(doc: Mapping[str, Any]) -> str:
    lines = [f"{doc.get('name')}  {doc.get('endpoint')}"]
    lines.append("  models: " + ", ".join(m.get("id", "?") for m in doc.get("models", []) if isinstance(m, dict)))
    lines.append(f"  secret: {doc.get('secret', '(none)')}")
    if doc.get("description"):
        lines.append(f"  {doc['description']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, probe: Probe | None = None) -> int:
    """Run a `provider` verb. Returns the exit status; `probe` replaces the HTTP probe for `add` and `test`."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    active_probe = probe or http_probe
    try:
        if args.verb == "list":
            docs = list_providers()
            text = "\n".join(_describe(d) for d in docs) if docs else "No providers are registered."
            return layout.emit(args.json, data={"providers": docs}, text=text)
        if args.verb == "show":
            doc = show(args.name)
            return layout.emit(args.json, data=doc, text=_describe(doc))
        if args.verb == "add":
            doc = build(args.name, args.endpoint, args.models, secret=args.secret, description=args.description)
            path = add(doc, probe=None if args.no_test else active_probe, replace=args.replace)
            return layout.emit(args.json, data={"name": args.name, "path": str(path)}, text=f"Registered provider {args.name} in {path}.")
        if args.verb == "edit":
            doc = edit(args.name, endpoint=args.endpoint, secret=args.secret, clear_secret=args.clear_secret, description=args.description, add_models=args.add_models, remove_models=args.remove_models)
            return layout.emit(args.json, data=doc, text=f"Updated provider {args.name}.\n{_describe(doc)}")
        if args.verb == "remove":
            backup = remove(args.name)
            return layout.emit(args.json, data={"name": args.name, "backup": str(backup) if backup else None}, text=f"Removed provider {args.name}; a backup was kept.")
        result = test(args.name, active_probe)
        if not result.ok:
            raise Unavailable(f"The provider {args.name} did not answer its test: {result.detail or 'no detail'}.", hint="Check the endpoint.", param="endpoint", code="provider-unreachable")
        return layout.emit(args.json, data={"name": args.name, "ok": True, "detail": result.detail}, text=f"Provider {args.name} answered: {result.detail or 'ok'}.")
    except ToolError as error:
        return layout.emit(args.json, error=error)
