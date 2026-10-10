"""Pins the message catalog as a table, so a dropped id, a changed exit status or a code mapped to the wrong message fails here."""

from __future__ import annotations

import re
from pathlib import Path

import stratarc
from stratarc import messages

DENIED = {"msg-1003", "msg-1118"}
CONFLICT = {"msg-1005", "msg-1126", "msg-1135", "msg-1147", "msg-1148", "msg-1149", "msg-1150", "msg-1154", "msg-1155"}
UNAVAILABLE = {"msg-1007", "msg-1112", "msg-1113", "msg-1114", "msg-1115", "msg-1116", "msg-1119", "msg-1138", "msg-1151", "msg-1153", "msg-1160"}
DRIFT = {"msg-1117"}
FAILURE = {"msg-1008", "msg-1152"}

EXPECTED_IDS = {f"msg-{n}" for n in (*range(1001, 1009), *range(1101, 1161))}

# Codes a module raises that the catalog deliberately does not map. None today: a code a module raises is shown as a catalog message.
INTENTIONALLY_UNMAPPED: set[str] = set()

SCANNED_MODULES = ("layers.py", "config_cmd.py", "resources_cmd.py")

EXPECTED_CODES = {
    "list-mode-missing": "msg-1101",
    "mode-invalid": "msg-1102",
    "type-mismatch": "msg-1103",
    "parse-error": "msg-1104",
    "unknown-key": "msg-1105",
    "unknown-project": "msg-1106",
    "unknown-agent": "msg-1107",
    "unknown-account": "msg-1108",
    "unknown-runtime": "msg-1109",
    "source-root-missing": "msg-1110",
    "project-required": "msg-1111",
    "newer-schema": "msg-1112",
    "provider-unreachable": "msg-1113",
    "adapter-unsupported": "msg-1114",
    "adapter-outdated": "msg-1115",
    "database-locked": "msg-1116",
    "drift": "msg-1117",
    "home-unwritable": "msg-1118",
    "backup-missing": "msg-1119",
    "settings-malformed": "msg-1120",
    "range-invalid": "msg-1121",
    "manifest-invalid": "msg-1122",
    "manifest-unreadable": "msg-1123",
    "manifest-missing": "msg-1124",
    "adapter-unknown": "msg-1125",
    "adapter-exists": "msg-1126",
    "deprecation-reason": "msg-1127",
    "deprecation-date": "msg-1128",
    "deprecation-unknown": "msg-1129",
    "runtime-version": "msg-1130",
    "provider-invalid": "msg-1131",
    "provider-name": "msg-1132",
    "provider-unknown": "msg-1133",
    "provider-unreadable": "msg-1134",
    "provider-exists": "msg-1135",
    "model-unknown": "msg-1136",
    "invalid-input": "msg-1137",
    "unavailable": "msg-1138",
    "invalid-edit": "msg-1139",
    "no-editor": "msg-1140",
    "needs-yes": "msg-1141",
    "source-not-found": "msg-1142",
    "invalid-name": "msg-1143",
    "invalid-value": "msg-1144",
    "invalid-path": "msg-1145",
    "invalid-config": "msg-1146",
    "project-exists": "msg-1147",
    "account-exists": "msg-1148",
    "source-exists": "msg-1149",
    "path-exists": "msg-1150",
    "not-reconciled": "msg-1151",
    "editor-failed": "msg-1152",
    "ui-extra-missing": "msg-1153",
    "conflict": "msg-1154",
    "agent-exists": "msg-1155",
    "relay-cycle": "msg-1156",
    "relay-depth": "msg-1157",
    "relay-invalid": "msg-1158",
    "flag-invalid": "msg-1159",
}

EXPECTED_EXITS_OF_NEW = {"msg-1155": messages.CONFLICT, "msg-1156": messages.INVALID_INPUT, "msg-1157": messages.INVALID_INPUT, "msg-1158": messages.INVALID_INPUT, "msg-1159": messages.INVALID_INPUT}


def _raised_codes() -> dict[str, set[str]]:
    package = Path(stratarc.__file__).parent
    pattern = re.compile(r'(?:LayerError|ResourceError)\(\s*"([a-z]+(?:-[a-z]+)+)"')
    return {name: set(pattern.findall((package / name).read_text(encoding="utf-8"))) for name in SCANNED_MODULES}


def test_every_code_the_command_modules_raise_is_mapped_or_listed_as_unmapped():
    raised = _raised_codes()
    # The scan must find the codes, or it proves nothing.
    assert all(raised.values()), raised
    for module, codes in raised.items():
        for code in sorted(codes):
            assert code in messages.CODE_MESSAGES or code in INTENTIONALLY_UNMAPPED, f"{module} raises {code}, which has no catalog message"


def test_the_unmapped_list_holds_only_codes_that_are_raised_and_not_mapped():
    raised = set().union(*_raised_codes().values())
    for code in INTENTIONALLY_UNMAPPED:
        assert code in raised and code not in messages.CODE_MESSAGES, code


def test_the_new_relay_flag_and_agent_messages_end_with_their_status():
    for message_id, status in EXPECTED_EXITS_OF_NEW.items():
        assert messages.CATALOG[message_id].exit == status, message_id


def test_the_catalog_holds_exactly_the_published_ids():
    assert set(messages.CATALOG) == EXPECTED_IDS


def test_every_message_ends_with_its_published_exit_status():
    expected = {}
    for message_id in EXPECTED_IDS:
        for status, group in ((messages.DENIED, DENIED), (messages.CONFLICT, CONFLICT), (messages.UNAVAILABLE, UNAVAILABLE), (messages.DRIFT, DRIFT), (messages.FAILURE, FAILURE)):
            if message_id in group:
                expected[message_id] = status
                break
        else:
            expected[message_id] = messages.INVALID_INPUT
    assert {message_id: message.exit for message_id, message in messages.CATALOG.items()} == expected


def test_every_module_code_maps_to_its_published_message():
    assert messages.CODE_MESSAGES == EXPECTED_CODES


def test_from_code_returns_the_message_each_code_maps_to():
    for code, message_id in EXPECTED_CODES.items():
        error = messages.from_code(code, "x")
        assert error is not None and error.id == message_id, code
        # Rendering must never raise, even when the code's text names placeholders the caller did not supply.
        assert error.problem and error.recovery, code


def test_a_message_with_unsupplied_placeholders_renders_without_raising():
    error = messages.from_code("backup-missing", "x")
    assert error is not None
    assert "{" not in error.problem and "{" not in error.recovery


def test_the_param_fills_the_path_and_name_placeholders():
    error = messages.from_code("settings-malformed", "bad json", param="notes-cli")
    assert error is not None
    assert "notes-cli" in error.problem
