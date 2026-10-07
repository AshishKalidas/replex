"""Exceptions raised by RepleX.

Every exception derives from :class:`ReplexError`, so ``except replex.ReplexError``
catches anything the library raises on purpose.
"""

from __future__ import annotations


class ReplexError(Exception):
    """Base class for every error raised by RepleX."""


class ConfigError(ReplexError):
    """The ``[tool.replex]`` configuration, an environment variable or an option is invalid."""


class CassetteError(ReplexError):
    """A cassette file could not be read, validated or written."""


class CassetteNotFound(CassetteError):
    """Replay or drift was requested for a cassette that does not exist."""


class UnsupportedSchemaVersion(CassetteError):
    """The cassette was written with a schema version this release cannot read."""


class CassetteExists(CassetteError):
    """``record`` mode refuses to overwrite an existing cassette without ``overwrite=True``."""


class RecordingRefused(ReplexError):
    """``record`` or ``rewrite`` was requested in CI without ``REPLEX_ALLOW_RECORD_IN_CI=1``."""


class SessionActive(ReplexError):
    """A session was started while another one is active. Sessions do not nest."""


class NoActiveSession(ReplexError):
    """An operation that needs an active session was called outside one."""


class NetworkBlocked(ReplexError):
    """A request to a host that is neither intercepted nor allowed to pass through during replay."""


class ReplayMismatch(ReplexError):
    """A request made during replay does not match any recorded exchange."""


class UnusedRecording(ReplexError):
    """Recorded exchanges were never requested during replay (``strict_unused=true``)."""


class UnrecordedToolCall(ReplexError):
    """A tool was called with arguments that have no recording and the tool may not run live."""


class PolicyError(ReplexError):
    """A drift policy file is missing, malformed or contains unknown checks."""


class ReplayedToolError(ReplexError):
    """Re-raised in replay for a recorded tool exception whose original class cannot be rebuilt.

    Attributes:
        type_name: The fully qualified name of the original exception class.
        message: The original exception message (after redaction).
    """

    def __init__(self, type_name: str, message: str) -> None:
        super().__init__(f"{type_name}: {message}")
        self.type_name = type_name
        self.message = message


class DriftDetected(ReplexError, AssertionError):
    """Raised by the pytest plugin when a drift run has at least one ``fail`` finding."""
