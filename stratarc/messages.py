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
)

CATALOG: dict[str, Message] = {message.id: message for message in _MESSAGES}


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
