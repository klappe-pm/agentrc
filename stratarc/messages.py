"""The error messages the command line raises, in one place so the wording is reviewed as a set.

Each message has an id (`msg-` and a number), the exit status it ends the command with, one sentence that states the problem and one sentence that states the recovery. Placeholders in braces are filled by the caller. A message is raised as `CliError(id, name=value, ...)`; `stratarc.cli` prints it and exits with the catalog's status.
"""

from __future__ import annotations

from dataclasses import dataclass

OK = 0
FAILURE = 1
INVALID_INPUT = 2
DENIED = 3
CONFLICT = 4
UNAVAILABLE = 5
DRIFT = 6
INTERRUPTED = 130


@dataclass(frozen=True)
class Message:
    id: str
    exit: int
    problem: str
    recovery: str


_MESSAGES = (
    Message(
        "msg-1001",
        INVALID_INPUT,
        "The source root {path} does not exist or is not a directory.",
        "Pass an existing directory with --root, or create one with `stratarc init {path}`.",
    ),
    Message(
        "msg-1002",
        INVALID_INPUT,
        "The configuration cannot be used: {detail}",
        "Fix stratarc.toml in the source root, or remove it to use the defaults.",
    ),
    Message(
        "msg-1003",
        DENIED,
        "The home directory {path} is missing or cannot be written to.",
        "Make it writable, or point at another one with --home or STRATARC_HOME.",
    ),
    Message(
        "msg-1004",
        INVALID_INPUT,
        "The runtime {name} is not known.",
        "Use one of: {known}.",
    ),
    Message(
        "msg-1005",
        CONFLICT,
        "{path} exists and is not empty.",
        "Choose a path that does not exist, or empty the directory first.",
    ),
    Message(
        "msg-1006",
        INVALID_INPUT,
        "{path} exists and is not a directory.",
        "Choose a path that does not exist, or an empty directory.",
    ),
    Message(
        "msg-1007",
        UNAVAILABLE,
        "The packaged source-root template is missing from this install.",
        "Reinstall stratarc.",
    ),
    Message(
        "msg-1008",
        FAILURE,
        "The command {command} stopped unexpectedly: {detail}",
        "Run it again with --debug to see the traceback, and include it in an issue report.",
    ),
    # 11xx: the failures the config, log, verify, provider and adapter commands and the deploy gate can hit.
    Message(
        "msg-1101",
        INVALID_INPUT,
        "A list value has no mode: {detail}",
        "Set the mode for that key to replace or extend in the file that holds the list.",
    ),
    Message(
        "msg-1102",
        INVALID_INPUT,
        "A mode is not valid: {detail}",
        "Use replace or extend, and set a mode only on a list.",
    ),
    Message(
        "msg-1103",
        INVALID_INPUT,
        "A key has a different type in two layers: {detail}",
        "Give the key one type in every layer.",
    ),
    Message(
        "msg-1104",
        INVALID_INPUT,
        "A layer file cannot be parsed: {detail}",
        "Fix the syntax in the file named in the message.",
    ),
    Message(
        "msg-1105",
        INVALID_INPUT,
        "No layer sets the key: {detail}",
        "Run `stratarc config list` to see the keys that are set.",
    ),
    Message(
        "msg-1106",
        INVALID_INPUT,
        "The project is not known: {detail}",
        "Run `stratarc projects --check` to list the projects, or check the spelling.",
    ),
    Message(
        "msg-1107",
        INVALID_INPUT,
        "The agent is not known: {detail}",
        "Check the spelling, or pass --project when the agent belongs to a project.",
    ),
    Message(
        "msg-1108",
        INVALID_INPUT,
        "The account is not known: {detail}",
        "Create accounts/<name>.toml in the source root, or check the spelling.",
    ),
    Message(
        "msg-1109",
        INVALID_INPUT,
        "The runtime is not known to the layers: {detail}",
        "Use a runtime named under [runtimes] in stratarc.toml.",
    ),
    Message(
        "msg-1110",
        INVALID_INPUT,
        "The source root cannot be read: {detail}",
        "Pass an existing directory with --root, or create one with `stratarc init`.",
    ),
    Message(
        "msg-1111",
        INVALID_INPUT,
        "The command needs a project: {detail}",
        "Pass --project NAME.",
    ),
    Message(
        "msg-1112",
        UNAVAILABLE,
        "A file was written by a newer stratarc and was left unchanged: {detail}",
        "Upgrade stratarc, then run the command again.",
    ),
    Message(
        "msg-1113",
        UNAVAILABLE,
        "A provider did not answer its test: {detail}",
        "Check the endpoint and the network, or register the provider with --no-test.",
    ),
    Message(
        "msg-1114",
        UNAVAILABLE,
        "An adapter does not support this install: {detail}",
        "Update the adapter with `stratarc adapter register`, or pin the runtime to a supported version.",
    ),
    Message(
        "msg-1115",
        UNAVAILABLE,
        "An adapter is older than the installed runtime: {detail}",
        "Update the adapter with `stratarc adapter register`; the deploy continues.",
    ),
    Message(
        "msg-1116",
        UNAVAILABLE,
        "The change log database is locked or unusable: {detail}",
        "Close the other process that uses the database and run the command again; the event is kept in the human readable log.",
    ),
    Message(
        "msg-1117",
        DRIFT,
        "The deployed files differ from what the source renders: {detail}",
        "Run `stratarc sync` to redeploy, or inspect the report with `stratarc verify show`.",
    ),
    Message(
        "msg-1118",
        DENIED,
        "The home directory cannot be created or written to: {detail}",
        "Make it writable, or point at another one with --home or STRATARC_HOME.",
    ),
    Message(
        "msg-1119",
        UNAVAILABLE,
        "The backup of {path} is missing from the home backups.",
        "Restore the file by hand, or run `stratarc sync` again to redeploy it.",
    ),
    Message(
        "msg-1120",
        INVALID_INPUT,
        "The project {name} was not delivered because {path} is not valid JSON ({detail}).",
        "Fix or remove the file and run the command again; nothing was delivered to this project.",
    ),
    Message(
        "msg-1121",
        INVALID_INPUT,
        "A version range is not valid: {detail}",
        "Use comma separated comparators such as >=0.4,<0.9, or *.",
    ),
    Message(
        "msg-1122",
        INVALID_INPUT,
        "An adapter manifest is not valid: {detail}",
        "Fix the fields the message lists; the adapter manifest schema describes each one.",
    ),
    Message(
        "msg-1123",
        INVALID_INPUT,
        "An adapter manifest cannot be read: {detail}",
        "Fix or replace the file, then register the adapter again.",
    ),
    Message(
        "msg-1124",
        INVALID_INPUT,
        "No adapter manifest was found: {detail}",
        "Pass a manifest.json file, a directory that holds one, or an installed package that ships one.",
    ),
    Message(
        "msg-1125",
        INVALID_INPUT,
        "The adapter is not registered: {detail}",
        "List the registered adapters with `stratarc adapter list`.",
    ),
    Message(
        "msg-1126",
        CONFLICT,
        "The adapter is already registered: {detail}",
        "Pass --replace to register it again.",
    ),
    Message(
        "msg-1127",
        INVALID_INPUT,
        "A deprecation has no reason: {detail}",
        "Pass --reason with the explanation operators should read.",
    ),
    Message(
        "msg-1128",
        INVALID_INPUT,
        "The end date of a deprecation is not a date: {detail}",
        "Pass --end-date as YYYY-MM-DD.",
    ),
    Message(
        "msg-1129",
        INVALID_INPUT,
        "The adapter is not deprecated: {detail}",
        "Check the name, or mark the adapter first with `stratarc adapter deprecate`.",
    ),
    Message(
        "msg-1130",
        INVALID_INPUT,
        "A runtime version is not in the form RUNTIME=VERSION: {detail}",
        "Pass it as, for example, --runtime-version codex=0.9.1.",
    ),
    Message(
        "msg-1131",
        INVALID_INPUT,
        "A provider is not valid: {detail}",
        "Fix the fields the message lists, and reference secrets as secret://NAMESPACE/KEY.",
    ),
    Message(
        "msg-1132",
        INVALID_INPUT,
        "The provider name is not valid: {detail}",
        "Use lowercase letters, digits and hyphens, starting with a letter.",
    ),
    Message(
        "msg-1133",
        INVALID_INPUT,
        "The provider is not registered: {detail}",
        "List the registered providers with `stratarc provider list`.",
    ),
    Message(
        "msg-1134",
        INVALID_INPUT,
        "A provider file cannot be read: {detail}",
        "Fix or remove the file under the home's providers directory.",
    ),
    Message(
        "msg-1135",
        CONFLICT,
        "The provider is already registered: {detail}",
        "Change it with `stratarc provider edit`, or remove it first.",
    ),
    Message(
        "msg-1136",
        INVALID_INPUT,
        "The provider does not serve the model: {detail}",
        "List the models it serves with `stratarc provider show`.",
    ),
    Message(
        "msg-1137",
        INVALID_INPUT,
        "The command was given input it cannot use: {detail}",
        "Check the arguments against `stratarc COMMAND --help`, then run it again.",
    ),
    Message(
        "msg-1138",
        UNAVAILABLE,
        "The data the command needs is not available: {detail}",
        "Run the command that produces it first, or check that the path exists and can be read.",
    ),
    Message(
        "msg-1139",
        INVALID_INPUT,
        "The edit would leave the file invalid: {detail}",
        "Fix the listed problems and run the command again, the file was left unchanged.",
    ),
    Message(
        "msg-1140",
        INVALID_INPUT,
        "No editor is configured to edit the file: {detail}",
        "Set $EDITOR or $VISUAL, then run the command again.",
    ),
    Message(
        "msg-1141",
        INVALID_INPUT,
        "The command deletes files and needs confirmation: {detail}",
        "Pass --yes to confirm, or --dry-run to preview.",
    ),
    Message(
        "msg-1142",
        INVALID_INPUT,
        "The source root is not registered or no longer exists: {detail}",
        "Run `stratarc source list` for the registered ones, or register one with `stratarc source init`.",
    ),
    Message(
        "msg-1143",
        INVALID_INPUT,
        "The name cannot be used: {detail}",
        "Use lowercase letters, digits and hyphens, starting with a letter.",
    ),
    Message(
        "msg-1144",
        INVALID_INPUT,
        "The value cannot be used: {detail}",
        "Check the value against `stratarc COMMAND --help`, then run the command again.",
    ),
    Message(
        "msg-1145",
        INVALID_INPUT,
        "The path cannot be used: {detail}",
        "Choose a path the command allows, which `stratarc COMMAND --help` describes.",
    ),
    Message(
        "msg-1146",
        INVALID_INPUT,
        "The configuration cannot be used: {detail}",
        "Fix stratarc.toml in the source root, then run the command again.",
    ),
    Message(
        "msg-1147",
        CONFLICT,
        "The project already exists: {detail}",
        "Change it with `stratarc project edit`, or pick another name.",
    ),
    Message(
        "msg-1148",
        CONFLICT,
        "The account already exists: {detail}",
        "Change it with `stratarc account edit`, or pick another name.",
    ),
    Message(
        "msg-1149",
        CONFLICT,
        "The source name is already registered: {detail}",
        "Choose another --name, or use `stratarc source use`.",
    ),
    Message(
        "msg-1150",
        CONFLICT,
        "The path already exists and is not empty: {detail}",
        "Choose a new or empty directory.",
    ),
    Message(
        "msg-1151",
        UNAVAILABLE,
        "The control plane has no entry for the project: {detail}",
        "Run `stratarc reconcile` to add it, then run the command again.",
    ),
    Message(
        "msg-1152",
        FAILURE,
        "The editor did not finish: {detail}",
        "Check $EDITOR, then run the command again, nothing was saved.",
    ),
    Message(
        "msg-1153",
        UNAVAILABLE,
        "The terminal interface needs Textual, which is not installed.",
        "Install it with `pip install 'stratarc[ui]'`, then run `stratarc ui` again.",
    ),
    Message(
        "msg-1154",
        CONFLICT,
        "The change conflicts with what already exists: {detail}",
        "Resolve the conflict, or choose another name or path, then run the command again.",
    ),
    Message(
        "msg-1155",
        CONFLICT,
        "The agent already exists: {detail}",
        "Change it with `stratarc agent edit`, or pick another name.",
    ),
    Message(
        "msg-1156",
        INVALID_INPUT,
        "The agents relay to each other in a loop: {detail}",
        "Remove one \"parent\" so the chain ends at an agent with no parent.",
    ),
    Message(
        "msg-1157",
        INVALID_INPUT,
        "The relay chain of the agent is too long: {detail}",
        "Shorten the chain, or give the child the settings directly.",
    ),
    Message(
        "msg-1158",
        INVALID_INPUT,
        "An agent's \"parent\" or \"relay\" is not valid: {detail}",
        "Name an existing agent in \"parent\" and give \"relay\" an \"inherit\" list of key patterns, or remove the key.",
    ),
    Message(
        "msg-1159",
        INVALID_INPUT,
        "A --set, --set-json or --set-mode flag is not valid: {detail}",
        "Write KEY=VALUE, quote JSON for your shell, and pass --set-mode only with the --set or --set-json that sets the key.",
    ),
    Message(
        "msg-1160",
        UNAVAILABLE,
        "The terminal interface needs a terminal, and its input or output is redirected.",
        "Run `stratarc ui` in an interactive terminal, or use the commands that print text, such as `stratarc config list`.",
    ),
)

CATALOG: dict[str, Message] = {message.id: message for message in _MESSAGES}

# The stable string codes the config, provider, adapter, log and verify modules put in their error bodies, and the catalog message each one is shown as. A module keeps raising its own code and wording; the command line resolves the code here and shows the catalog message with the module's text as {detail}. A code absent from this table is shown as the module wrote it.
CODE_MESSAGES: dict[str, str] = {
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


class CliError(Exception):
    """A failure the command line reports by catalog id, then exits with that message's status."""

    def __init__(self, message_id: str, *, param: str | None = None, **values: object) -> None:
        self.message = CATALOG[message_id]
        self.param = param
        self.values = values
        super().__init__(self.problem)

    @property
    def id(self) -> str:
        return self.message.id

    @property
    def exit(self) -> int:
        return self.message.exit

    @property
    def problem(self) -> str:
        return self.message.problem.format_map(_Values(self.values))

    @property
    def recovery(self) -> str:
        return self.message.recovery.format_map(_Values(self.values))


class _Values(dict):
    """Format values where a placeholder the caller did not supply reads as "it" rather than raising."""

    def __missing__(self, key: str) -> str:
        return "it"


def from_code(code: str, detail: object, *, param: str | None = None) -> CliError | None:
    """The catalog error for a module's string code, with the module's own text as the detail, or None when the code has no entry.

    The param, when given, also fills the {path} and {name} placeholders some messages carry, so a code raised with only a detail still renders a complete sentence.
    """
    message_id = CODE_MESSAGES.get(code)
    if message_id is None:
        return None
    values: dict[str, object] = {"detail": detail}
    if param is not None:
        values["path"] = param
        values["name"] = param
    return CliError(message_id, param=param, **values)
