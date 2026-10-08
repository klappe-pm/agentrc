"""The JSON Schemas for hooks.json, permissions.json and components.json."""

from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest

SCHEMAS = ("hooks", "permissions", "components")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "schema"
REFERENCE_ENV = "AGENTRC_SCHEMA_REFERENCE_DIR"
REFERENCE_FILES = {
    "hooks": Path("hooks") / "hooks.json",
    "permissions": Path("permissions.json"),
    "components": Path("components.json"),
}


def load_schema(name: str) -> dict:
    resource = files("agentrc") / "data" / "schema" / f"{name}.schema.json"
    return json.loads(resource.read_text(encoding="utf-8"))


def validator(name: str) -> "jsonschema.Draft202012Validator":
    return jsonschema.Draft202012Validator(load_schema(name))


def fixtures(kind: str) -> list:
    cases = []
    for name in SCHEMAS:
        for path in sorted((FIXTURES / name / kind).glob("*.json")):
            cases.append(pytest.param(name, path, id=f"{name}/{path.stem}"))
    return cases


def describe(errors: list) -> str:
    return "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(load_schema(name))


@pytest.mark.parametrize("name", SCHEMAS)
def test_every_schema_has_fixtures(name: str) -> None:
    assert list((FIXTURES / name / "valid").glob("*.json"))
    assert list((FIXTURES / name / "invalid").glob("*.json"))


@pytest.mark.parametrize(("name", "path"), fixtures("valid"))
def test_valid_fixture_passes(name: str, path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    errors = list(validator(name).iter_errors(document))
    assert not errors, describe(errors)


@pytest.mark.parametrize(("name", "path"), fixtures("invalid"))
def test_invalid_fixture_fails(name: str, path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert not validator(name).is_valid(document)


@pytest.mark.skipif(
    not os.environ.get(REFERENCE_ENV),
    reason=f"{REFERENCE_ENV} is not set",
)
@pytest.mark.parametrize("name", SCHEMAS)
def test_reference_file_passes(name: str) -> None:
    path = Path(os.environ[REFERENCE_ENV]) / REFERENCE_FILES[name]
    assert path.is_file(), f"{REFERENCE_FILES[name]} not found under {REFERENCE_ENV}"
    document = json.loads(path.read_text(encoding="utf-8"))
    errors = list(validator(name).iter_errors(document))
    assert not errors, describe(errors)
