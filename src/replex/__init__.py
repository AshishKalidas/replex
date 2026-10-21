"""RepleX: record-and-replay regression testing for AI agents.

Record an agent run once, replay it offline forever, and fail CI when behaviour drifts.

Everything exported here is the public API; anything else is private.
"""

from __future__ import annotations

import os
from pathlib import Path

from replex._version import __version__
from replex.capture.tools import UnserialisableValue, tool, wrap_tools
from replex.capture.transport import (
    AsyncReplexTransport,
    ReplexTransport,
    async_http_client,
    http_client,
)
from replex.cassette.model import Cassette
from replex.cassette.storage import load as _load
from replex.config import Config, load_config
from replex.drift.compare import DriftReport, Finding, compare
from replex.drift.diff import TraceDiff, diff
from replex.drift.policy import Policy, load_policy
from replex.errors import (
    CassetteError,
    CassetteExists,
    CassetteNotFound,
    ConfigError,
    DriftDetected,
    NetworkBlocked,
    NoActiveSession,
    PolicyError,
    RecordingRefused,
    ReplayedToolError,
    ReplayMismatch,
    ReplexError,
    SessionActive,
    UnrecordedToolCall,
    UnsupportedSchemaVersion,
    UnusedRecording,
)
from replex.pricing import Pricing
from replex.providers import Normaliser, register_normaliser
from replex.session import Session, current_session, session
from replex.trace.model import ModelStep, ToolRequest, ToolStep, Trace, Usage


def load_cassette(path: str | os.PathLike[str]) -> Cassette:
    """Load and validate a cassette file."""
    return _load(Path(path))


def load_trace(source: str | os.PathLike[str] | Cassette) -> Trace:
    """Build the provider-neutral trace of a cassette (object or file path)."""
    cassette = source if isinstance(source, Cassette) else _load(Path(source))
    return Trace.from_cassette(cassette)


__all__ = [
    "AsyncReplexTransport",
    "Cassette",
    "CassetteError",
    "CassetteExists",
    "CassetteNotFound",
    "Config",
    "ConfigError",
    "DriftDetected",
    "DriftReport",
    "Finding",
    "ModelStep",
    "NetworkBlocked",
    "NoActiveSession",
    "Normaliser",
    "Policy",
    "PolicyError",
    "Pricing",
    "RecordingRefused",
    "ReplayMismatch",
    "ReplayedToolError",
    "ReplexError",
    "ReplexTransport",
    "Session",
    "SessionActive",
    "ToolRequest",
    "ToolStep",
    "Trace",
    "TraceDiff",
    "UnrecordedToolCall",
    "UnserialisableValue",
    "UnsupportedSchemaVersion",
    "UnusedRecording",
    "Usage",
    "__version__",
    "async_http_client",
    "compare",
    "current_session",
    "diff",
    "http_client",
    "load_cassette",
    "load_config",
    "load_policy",
    "load_trace",
    "register_normaliser",
    "session",
    "tool",
    "wrap_tools",
]
