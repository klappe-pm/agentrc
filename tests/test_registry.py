"""The adapter registry: manifests, ranges, status, deprecation and the deploy gate."""

from __future__ import annotations

import json
import shutil
import time
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest

from stratarc import home_layout as layout
from stratarc import registry
from stratarc.adapters._supports import SUPPORTS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
GOOD = FIXTURES / "good"
BAD = FIXTURES / "bad"


def manifest_validator() -> jsonschema.Draft202012Validator:
    text = (files("stratarc") / "data" / "schema" / "adapter-manifest.schema.json").read_text(encoding="utf-8")
    return jsonschema.Draft202012Validator(json.loads(text))


def good() -> dict:
    return json.loads((GOOD / "manifest.json").read_text())


# ranges


@pytest.mark.parametrize(
    "version, spec, expected",
    [
        ("0.8.3", ">=0.4,<0.9", True),
        ("0.9", ">=0.4,<0.9", False),
        ("0.3.9", ">=0.4,<0.9", False),
        ("2.1.273", "*", True),
        ("1.0", "==1", True),
        ("1.2", "==1", False),
        ("0.4", ">0.4", False),
        ("codex-cli 0.9.1", "<1", True),
    ],
)
def test_ranges(version, spec, expected) -> None:
    assert registry.in_range(registry.parse_version(version), spec) is expected


def test_invalid_range_is_rejected() -> None:
    with pytest.raises(layout.InvalidInput):
        registry.in_range((1,), "about one")


def test_parse_version_none() -> None:
    assert registry.parse_version(None) is None
    assert registry.parse_version("no digits") is None


# manifests


def test_good_fixture_is_valid_by_code_and_schema() -> None:
    assert registry.validate_manifest(good()) == []
    assert list(manifest_validator().iter_errors(good())) == []


def test_bad_fixture_is_rejected_by_code_and_schema() -> None:
    doc = json.loads((BAD / "manifest.json").read_text())
    errors = registry.validate_manifest(doc)
    for fragment in ("unknown key", "name must match", "supports must be a range", "distinct kind", "inside it"):
        assert any(fragment in e for e in errors), fragment
    assert list(manifest_validator().iter_errors(doc))


def test_bundled_manifests_come_from_the_adapters_and_validate() -> None:
    manifests = registry.bundled_manifests()
    assert set(manifests) == {"claude", "codex", "cursor", "gemini", "opencode"}
    for name, doc in manifests.items():
        assert registry.validate_manifest(doc) == [], name
        assert list(manifest_validator().iter_errors(doc)) == [], name
        assert doc["runtime"] == name and doc["origin"] == "bundled"
        assert doc["files_written"][-1].endswith("/**")
    assert "hooks" in manifests["claude"]["source_kinds"]
    assert "claude/settings.json" not in manifests["claude"]["files_written"]
    assert ".claude/settings.json" in manifests["claude"]["files_written"]


def test_register_from_directory_file_and_package(stratarc_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stored = registry.register(str(GOOD))
    assert stored["origin"] == "registered" and stored["source"].endswith("manifest.json")
    assert (layout.adapters_dir() / "acme" / "manifest.json").is_file()
    assert registry.get("acme")["supports"] == ">=0.4,<0.9"
    with pytest.raises(layout.Conflict):
        registry.register(str(GOOD / "manifest.json"))
    registry.register(str(GOOD / "manifest.json"), replace=True)

    package = tmp_path / "libs" / "acme_adapter"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    shutil.copy(GOOD / "manifest.json", package / "manifest.json")
    monkeypatch.syspath_prepend(str(tmp_path / "libs"))
    registry.remove("acme")
    assert registry.register("acme_adapter")["source"] == "package:acme_adapter"


def test_register_rejects_invalid_and_missing(stratarc_home: Path, tmp_path: Path) -> None:
    with pytest.raises(layout.InvalidInput) as caught:
        registry.register(str(BAD))
    assert caught.value.code == "manifest-invalid"
    with pytest.raises(layout.InvalidInput) as caught:
        registry.register(str(tmp_path / "nowhere"))
    assert caught.value.code == "manifest-missing"
    assert not (layout.adapters_dir() / "Acme Bad").exists()


def test_registered_manifest_overrides_the_bundled_one(stratarc_home: Path, tmp_path: Path) -> None:
    doc = {**good(), "name": "codex", "runtime": "codex", "supports": ">=0.4,<0.9"}
    directory = tmp_path / "override"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(doc))
    registry.register(str(directory))
    assert registry.list_adapters()["codex"]["supports"] == ">=0.4,<0.9"
    registry.remove("codex")
    assert registry.list_adapters()["codex"]["supports"] == SUPPORTS["codex"]["supports"]


def test_register_bundled_writes_missing_manifests_once(stratarc_home: Path) -> None:
    assert sorted(registry.register_bundled()) == ["claude", "codex", "cursor", "gemini", "opencode"]
    assert registry.register_bundled() == []


def test_newer_schema_manifest_is_refused(stratarc_home: Path, tmp_path: Path) -> None:
    directory = tmp_path / "future"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({**good(), "schema_version": 9}))
    with pytest.raises(layout.Unavailable) as caught:
        registry.register(str(directory))
    assert caught.value.exit == 5


# status


def test_status_states(stratarc_home: Path) -> None:
    registry.register(str(GOOD))
    manifest = registry.get("acme")
    cases = {
        "0.6.1": registry.OK,
        "0.8": registry.OK,
        "0.8.5": registry.OUTDATED,
        "0.3": registry.UNSUPPORTED,
        "0.9": registry.UNSUPPORTED,
        None: registry.UNKNOWN,
    }
    for installed, expected in cases.items():
        assert registry.evaluate(manifest, installed).state == expected, installed


def test_schema_outside_range_is_unsupported() -> None:
    result = registry.evaluate(good(), "0.6", schema=2)
    assert result.state == registry.UNSUPPORTED and "schema" in result.detail


def test_status_covers_every_adapter(stratarc_home: Path) -> None:
    registry.register(str(GOOD))
    rows = {s.name: s for s in registry.status({"acme": "0.9", "claude": "2.1.273"})}
    assert rows["acme"].state == registry.UNSUPPORTED
    assert rows["claude"].state == registry.OK
    assert rows["codex"].state == registry.UNKNOWN


def test_detect_versions_is_injectable() -> None:
    class Result:
        stdout = "codex-cli 0.9.1\n"
        stderr = ""

    def which(command: str):
        return "/bin/" + command if command == "codex" else None

    versions = registry.detect_versions(runner=lambda *a, **k: Result(), which=which)
    assert versions["codex"] == "0.9.1" and versions["claude"] is None


# gate


def test_gate_blocks_unsupported_every_time() -> None:
    status = registry.evaluate(good(), "0.9")
    warned: set[str] = set()
    for _ in range(2):
        decision = registry.sync_gate(status, warned)
        assert not decision.allowed and "acme" in decision.message
    with pytest.raises(layout.Unavailable) as caught:
        registry.require_deployable(status)
    assert caught.value.exit == 5


def test_gate_warns_once_per_run_for_outdated() -> None:
    status = registry.evaluate(good(), "0.8.5")
    warned: set[str] = set()
    first = registry.sync_gate(status, warned)
    second = registry.sync_gate(status, warned)
    assert first.allowed and first.message and "outdated" in first.message
    assert second.allowed and second.message is None
    assert registry.sync_gate(status, set()).message  # a new run warns again


def test_gate_is_silent_for_ok_and_unknown() -> None:
    for installed in ("0.6", None):
        decision = registry.sync_gate(registry.evaluate(good(), installed))
        assert decision.allowed and decision.message is None


# deprecation


def test_deprecate_records_and_validates(stratarc_home: Path) -> None:
    registry.register(str(GOOD))
    with pytest.raises(layout.InvalidInput):
        registry.deprecate("acme", "", "2027-01-01")
    with pytest.raises(layout.InvalidInput):
        registry.deprecate("acme", "old", "next spring")
    with pytest.raises(layout.InvalidInput):
        registry.deprecate("acme", "old", "2027-01-01", replacement="ghost")
    with pytest.raises(layout.InvalidInput):
        registry.deprecate("ghost", "old", "2027-01-01")
    record = registry.deprecate("acme", "The runtime was retired.", "2027-01-01", replacement="codex")
    assert registry.read_deprecation("acme") == record
    notice = registry.deprecation_notice("acme")
    assert 'adapter "acme"' in notice and "settings.json" in notice and "2027-01-01" in notice and "Replacement: codex" in notice


def test_interactive_prompt_asks_once_and_records(stratarc_home: Path) -> None:
    registry.register(str(GOOD))
    registry.deprecate("acme", "retired", "2027-01-01")
    asked, printed = [], []
    shown = registry.check_deprecations(interactive=True, ask=lambda q: asked.append(q) or "y", write=printed.append)
    assert len(shown) == 1 and len(asked) == 1
    assert registry.read_deprecation("acme")["answer"] == "continue"
    assert registry.check_deprecations(interactive=True, ask=lambda q: asked.append(q) or "y", write=printed.append) == []
    assert len(asked) == 1


def test_noninteractive_prints_and_continues_without_recording(stratarc_home: Path) -> None:
    registry.register(str(GOOD))
    registry.deprecate("acme", "retired", "2027-01-01")
    printed = []

    def forbidden(_q):
        raise AssertionError("must not prompt")

    shown = registry.check_deprecations(interactive=False, ask=forbidden, write=printed.append)
    assert shown and printed == shown
    assert registry.read_deprecation("acme")["answer"] is None
    assert len(registry.check_deprecations(interactive=False, ask=forbidden, write=printed.append)) == 1


def test_declined_answer_is_recorded_as_stop(stratarc_home: Path) -> None:
    registry.register(str(GOOD))
    registry.deprecate("acme", "retired", "2027-01-01")
    registry.check_deprecations(interactive=True, ask=lambda q: "n", write=lambda s: None)
    assert registry.read_deprecation("acme")["answer"] == "stop"


# command line


def run(argv, capsys, **kwargs):
    code = registry.main(argv, **kwargs)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_list_show_register_remove(stratarc_home: Path, capsys) -> None:
    code, out, _ = run(["list", "--json"], capsys, interactive=False)
    assert code == 0 and {a["name"] for a in json.loads(out)["data"]["adapters"]} >= {"claude", "codex"}
    assert run(["register", str(GOOD), "--json"], capsys, interactive=False)[0] == 0
    assert run(["register", str(GOOD)], capsys, interactive=False)[0] == 4
    code, out, _ = run(["show", "acme", "--json"], capsys, interactive=False)
    assert json.loads(out)["data"]["deprecation"] is None
    assert run(["remove", "acme"], capsys, interactive=False)[0] == 0
    assert run(["show", "acme"], capsys, interactive=False)[0] == 2


def test_cli_status_uses_injected_versions_and_overrides(stratarc_home: Path, capsys) -> None:
    run(["register", str(GOOD)], capsys, interactive=False)
    code, out, _ = run(["status", "--json", "--runtime-version", "acme=0.9"], capsys, detect=lambda: {"acme": "0.6", "claude": "2.1.0"})
    rows = {r["name"]: r for r in json.loads(out)["data"]["adapters"]}
    assert code == 0
    assert rows["acme"]["state"] == "unsupported" and rows["claude"]["state"] == "ok"
    assert run(["status", "--runtime-version", "broken"], capsys, detect=dict)[0] == 2


def test_cli_deprecate_then_noninteractive_notice(stratarc_home: Path, capsys) -> None:
    run(["register", str(GOOD)], capsys, interactive=False)
    code, _, _ = run(["deprecate", "acme", "--reason", "retired", "--end-date", "2027-01-01", "--replacement", "codex"], capsys, interactive=False)
    assert code == 0
    code, out, _ = run(["list"], capsys, interactive=False)
    assert 'The adapter "acme" is deprecated' in out
    assert run(["deprecate", "acme", "--reason", "x", "--end-date", "soon"], capsys, interactive=False)[0] == 2
    code, out, _ = run(["status", "--json"], capsys, detect=dict, interactive=False)
    assert code == 0 and next(r for r in json.loads(out)["data"]["adapters"] if r["name"] == "acme")["deprecated"] is True


# version detection runs in an isolated home


def fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> tuple[Path, Path]:
    """A `codex` command on a PATH of its own; returns the caller's redirected home and the file the command appends its observations to."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.txt"
    script = bin_dir / "codex"
    script.write_text(f"#!/bin/sh\nRECORD='{record}'\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(real_home / ".config"))
    monkeypatch.setenv("GH_TOKEN", "placeholder-not-a-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-secret")
    monkeypatch.setattr(registry, "_DETECTED", {})
    return real_home, record


WRITES_EVERYWHERE = """
mkdir -p "$HOME/.codex/tmp" "$XDG_CONFIG_HOME/opencode"
echo x > "$HOME/.codex/tmp/arg0"
echo x > "$XDG_CONFIG_HOME/opencode/state"
echo x > "./cwd-file"
echo "cwd=$(pwd) home=$HOME" >> "$RECORD"
env | cut -d= -f1 | sort >> "$RECORD"
echo "codex-cli 0.9.1"
"""


def test_detection_leaves_the_callers_home_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_home, record = fake_runtime(tmp_path, monkeypatch, WRITES_EVERYWHERE)
    versions = registry.detect_versions()
    assert versions["codex"] == "0.9.1"
    assert list(real_home.iterdir()) == []
    seen = record.read_text()
    first = seen.splitlines()[0]
    assert str(real_home) not in first
    assert not Path(first.split("cwd=")[1].split(" ")[0]).exists()
    assert str(Path.cwd()) not in first


def test_detection_environment_is_an_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, record = fake_runtime(tmp_path, monkeypatch, WRITES_EVERYWHERE)
    registry.detect_versions()
    names = set(record.read_text().splitlines()[1:])
    assert "GH_TOKEN" not in names and "OPENAI_API_KEY" not in names
    assert {"PATH", "HOME", "TERM", "NO_COLOR", "CI", "XDG_CONFIG_HOME", "CODEX_HOME"} <= names
    allowed = {"PATH", "LANG", "TERM", "NO_COLOR", "CI", "PWD", "SHLVL", "_", "OLDPWD", *registry._DETECT_HOME_VARIABLES}
    assert {n for n in names if not n.startswith("LC_")} <= allowed


def test_a_hanging_runtime_times_out_to_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runtime(tmp_path, monkeypatch, "exec sleep 30")
    monkeypatch.setattr(registry, "DETECT_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    assert registry.detect_versions()["codex"] is None
    assert time.monotonic() - started < 5, "the configured timeout must bound the wait, not the binary's own sleep"


def test_an_unparsable_runtime_is_unknown_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runtime(tmp_path, monkeypatch, "echo nothing useful")
    versions = registry.detect_versions()
    assert versions["claude"] is None
    assert versions["codex"] is None


def test_a_version_printed_before_a_nonzero_exit_is_still_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Some CLIs print the version and then exit nonzero from a wrapper; the output decides, not the status."""
    fake_runtime(tmp_path, monkeypatch, "echo codex-cli 0.9.1; exit 3")
    assert registry.detect_versions()["codex"] == "0.9.1"


def test_detection_is_cached_for_the_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, record = fake_runtime(tmp_path, monkeypatch, 'echo run >> "$RECORD"\necho "codex-cli 0.9.1"')
    assert registry.detect_versions()["codex"] == "0.9.1"
    assert registry.detect_versions()["codex"] == "0.9.1"
    assert record.read_text().splitlines() == ["run"]
