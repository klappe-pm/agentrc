"""Provider files: validation, secret handling, add, edit, remove and an injected probe."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest

from stratarc import home_layout as layout
from stratarc import providers

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "registry" / "provider-good.json"


def good() -> dict:
    return json.loads(FIXTURE.read_text())


def fake_token() -> str:
    # Assembled at run time so the source holds no token-shaped literal.
    return "ghp_" + "a1B2c3D4e5" * 4


def no_probe_calls(*_args):
    raise AssertionError("the network probe must not run")


def schema_validator() -> jsonschema.Draft202012Validator:
    text = (files("stratarc") / "data" / "schema" / "provider.schema.json").read_text(encoding="utf-8")
    return jsonschema.Draft202012Validator(json.loads(text))


def test_fixture_is_valid_by_code_and_schema() -> None:
    assert providers.validate(good()) == []
    assert list(schema_validator().iter_errors(good())) == []


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d.update(name="Bad Name"), "name must match"),
        (lambda d: d.update(endpoint="ftp://example.com"), "http or https"),
        (lambda d: d.update(endpoint="https://user:pw@example.com/v1"), "credentials"),
        (lambda d: d.update(endpoint="https://example.com/v1?k=1"), "query string"),
        (lambda d: d.update(models=[]), "non-empty list"),
        (lambda d: d.update(models=[{"id": "a"}, {"id": "a"}]), "duplicate"),
        (lambda d: d.update(secret="not-a-reference"), "secret://"),
        (lambda d: d.update(unknown=1), "unknown key"),
        (lambda d: d.pop("endpoint"), "endpoint must be a string"),
    ],
)
def test_invalid_documents_are_reported(mutate, fragment) -> None:
    doc = good()
    mutate(doc)
    assert any(fragment in e for e in providers.validate(doc))
    if fragment not in ("duplicate", "http or https") and "credentials" not in fragment:
        assert list(schema_validator().iter_errors(doc)) or fragment == "name must match"


def test_literal_token_in_secret_field_is_rejected_without_repeating_it() -> None:
    doc = good()
    doc["secret"] = fake_token()
    errors = providers.validate(doc)
    assert errors and all(fake_token() not in e for e in errors)


def test_literal_token_in_any_other_field_is_rejected() -> None:
    doc = good()
    doc["description"] = "key=" + fake_token()
    errors = providers.validate(doc)
    assert any("description holds a token-shaped literal" in e for e in errors)
    assert all(fake_token() not in e for e in errors)


def test_secret_reference_is_not_flagged_as_a_token() -> None:
    assert providers.validate(good()) == []


def test_add_writes_and_list_show_read_it_back(stratarc_home: Path) -> None:
    path = providers.add(good())
    assert path == stratarc_home / ".stratarc" / "providers" / "example-cloud.json"
    assert providers.show("example-cloud")["endpoint"] == "https://api.example.com/v1"
    assert [d["name"] for d in providers.list_providers()] == ["example-cloud"]


def test_add_refuses_a_duplicate_unless_replace(stratarc_home: Path) -> None:
    providers.add(good())
    with pytest.raises(layout.Conflict):
        providers.add(good())
    providers.add({**good(), "description": "new"}, replace=True)
    assert providers.show("example-cloud")["description"] == "new"


def test_add_with_a_failing_probe_writes_nothing(stratarc_home: Path) -> None:
    with pytest.raises(layout.Unavailable) as caught:
        providers.add(good(), probe=lambda *_: providers.ProbeResult(False, "connection refused"))
    assert caught.value.exit == 5
    assert providers.list_providers() == []


def test_probe_receives_the_reference_and_never_a_value(stratarc_home: Path) -> None:
    seen = []

    def probe(endpoint, secret, models):
        seen.append((endpoint, secret, list(models)))
        return True

    providers.add(good(), probe=probe)
    assert seen == [("https://api.example.com/v1", "secret://example/api-key", ["example-large", "example-small"])]
    assert providers.test("example-cloud", probe) == providers.ProbeResult(True, "")


def test_edit_changes_fields_and_revalidates(stratarc_home: Path) -> None:
    providers.add(good())
    doc = providers.edit("example-cloud", endpoint="https://api.example.org/v2", add_models=["example-new"], remove_models=["example-small"])
    assert doc["endpoint"] == "https://api.example.org/v2"
    assert [m["id"] for m in doc["models"]] == ["example-large", "example-new"]
    with pytest.raises(layout.InvalidInput):
        providers.edit("example-cloud", secret=fake_token())
    assert providers.show("example-cloud")["secret"] == "secret://example/api-key"
    with pytest.raises(layout.InvalidInput):
        providers.edit("example-cloud", remove_models=["missing"])
    assert "secret" not in providers.edit("example-cloud", clear_secret=True)


def test_remove_keeps_a_backup(stratarc_home: Path) -> None:
    providers.add(good())
    backup = providers.remove("example-cloud")
    assert backup is not None and backup.is_file()
    with pytest.raises(layout.InvalidInput):
        providers.show("example-cloud")


def test_a_newer_schema_provider_is_not_rewritten(stratarc_home: Path) -> None:
    providers.add(good())
    path = layout.providers_dir() / "example-cloud.json"
    path.write_text(json.dumps({**good(), "schema_version": 7}))
    before = path.read_bytes()
    with pytest.raises(layout.NewerSchemaError):
        providers.edit("example-cloud", description="x")
    with pytest.raises(layout.NewerSchemaError):
        providers.add(good(), replace=True)
    assert path.read_bytes() == before


def test_provider_name_cannot_escape_the_directory(stratarc_home: Path) -> None:
    with pytest.raises(layout.InvalidInput):
        providers.path_for("../evil")


# command line


def run(argv, capsys, probe=no_probe_calls):
    code = providers.main(argv, probe=probe)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_add_list_show_json(stratarc_home: Path, capsys) -> None:
    code, out, _ = run(["add", "example-cloud", "--endpoint", "https://api.example.com/v1", "--model", "m1", "--secret", "secret://example/key", "--json"], capsys, probe=lambda *_: True)
    assert code == 0 and json.loads(out)["ok"] is True
    code, out, _ = run(["list", "--json"], capsys)
    assert [p["name"] for p in json.loads(out)["data"]["providers"]] == ["example-cloud"]
    code, out, _ = run(["show", "example-cloud"], capsys)
    assert code == 0 and "secret://example/key" in out


def test_cli_add_no_test_never_calls_the_probe(stratarc_home: Path, capsys) -> None:
    code, _, _ = run(["add", "p1", "--endpoint", "http://127.0.0.1:1/v1", "--model", "m", "--no-test"], capsys)
    assert code == 0


def test_cli_unreachable_provider_exits_5(stratarc_home: Path, capsys) -> None:
    run(["add", "p1", "--endpoint", "http://127.0.0.1:1/v1", "--model", "m", "--no-test"], capsys)
    code, out, _ = run(["test", "p1", "--json"], capsys, probe=lambda *_: providers.ProbeResult(False, "refused"))
    body = json.loads(out)
    assert code == 5 and body["ok"] is False and body["error"]["code"] == "provider-unreachable"


def test_cli_literal_secret_exits_2_and_writes_nothing(stratarc_home: Path, capsys) -> None:
    code, out, _ = run(["add", "p1", "--endpoint", "https://api.example.com", "--model", "m", "--secret", fake_token(), "--no-test", "--json"], capsys)
    assert code == 2
    assert fake_token() not in out
    assert providers.list_providers() == []


def test_cli_duplicate_exits_4_and_unknown_exits_2(stratarc_home: Path, capsys) -> None:
    args = ["add", "p1", "--endpoint", "https://api.example.com", "--model", "m", "--no-test"]
    assert run(args, capsys)[0] == 0
    assert run(args, capsys)[0] == 4
    assert run(["show", "nope"], capsys)[0] == 2
    assert run(["remove", "p1"], capsys)[0] == 0


def test_cli_bad_arguments_return_2(stratarc_home: Path, capsys) -> None:
    assert run(["add"], capsys)[0] == 2
