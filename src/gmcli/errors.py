"""Error hierarchy for gmcli.

Every error carries the process exit code it should produce, so ``cli.main``
can translate an exception into a status without a lookup table. Codes:

    0  success
    1  general failure
    2  usage error (bad arguments)
    3  authentication / authorization failure
    4  requested resource not found
    5  API or network failure
"""

from __future__ import annotations


class GmcliError(Exception):
    """Base for every error gmcli raises deliberately.

    ``hint`` is optional follow-up text shown under the error message — use it
    for the "here is the command that fixes this" line.
    """

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(GmcliError):
    exit_code = 2


class AuthError(GmcliError):
    exit_code = 3


class NotFoundError(GmcliError):
    exit_code = 4


class ApiError(GmcliError):
    exit_code = 5


class ConfigError(GmcliError):
    exit_code = 1
