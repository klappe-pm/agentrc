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
        return self.message.problem.format(**self.values)

    @property
    def recovery(self) -> str:
        return self.message.recovery.format(**self.values)


def from_code(code: str, detail: object, *, param: str | None = None) -> CliError | None:
    """The catalog error for a module's string code, with the module's own text as the detail, or None when the code has no entry."""
    message_id = CODE_MESSAGES.get(code)
    if message_id is None:
        return None
    return CliError(message_id, param=param, detail=detail)
