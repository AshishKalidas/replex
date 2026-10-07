"""Pydantic models for the cassette file format (schema version 1)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

BodyEncoding = Literal["json", "text", "base64", "empty"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StreamEvent(_Model):
    """One server-sent event. ``data`` is parsed JSON when it is an object or array."""

    event: str | None = None
    data: Any = None
    id: str | None = None


class RecordedRequest(_Model):
    """An HTTP request as written to the cassette (already redacted)."""

    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    body_encoding: BodyEncoding = "json"
    match_key: str


class RecordedResponse(_Model):
    """An HTTP response. Streamed responses keep their events in ``stream`` and leave ``body`` null."""

    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    body_encoding: BodyEncoding = "json"
    stream: list[StreamEvent] | None = None


class ExchangeEvent(_Model):
    """One HTTP request and its response."""

    index: int
    type: Literal["exchange"] = "exchange"
    request: RecordedRequest
    response: RecordedResponse
    duration_ms: int = 0


class ToolError(_Model):
    """A recorded tool exception."""

    type: str
    message: str


class ToolCallEvent(_Model):
    """One execution of a function decorated with ``@replex.tool``.

    ``lossy`` is true when an argument or the result was not JSON-serialisable and was
    stored through the ``repr`` fallback (see docs/LIMITATIONS.md).
    """

    index: int
    type: Literal["tool_call"] = "tool_call"
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: ToolError | None = None
    duration_ms: int = 0
    lossy: bool = False


Event = Annotated[ExchangeEvent | ToolCallEvent, Field(discriminator="type")]


class CassetteMeta(BaseModel):
    """Descriptive metadata. Unknown keys are kept so users can add their own."""

    model_config = ConfigDict(extra="allow")

    provider: str = "unknown"
    model: str = "unknown"
    git_sha: str | None = None
    tags: list[str] = Field(default_factory=list)


class CassetteUsage(_Model):
    """Token totals across every model call in the cassette."""

    input_tokens: int = 0
    output_tokens: int = 0


class Cassette(_Model):
    """One recorded agent run."""

    schema_version: Literal[1] = SCHEMA_VERSION
    name: str
    created_at: str
    replex_version: str
    meta: CassetteMeta = Field(default_factory=CassetteMeta)
    events: list[Event] = Field(default_factory=list)
    usage: CassetteUsage = Field(default_factory=CassetteUsage)

    @property
    def exchanges(self) -> list[ExchangeEvent]:
        """The HTTP exchanges, in recorded order."""
        return [e for e in self.events if isinstance(e, ExchangeEvent)]

    @property
    def tool_calls(self) -> list[ToolCallEvent]:
        """The tool calls, in recorded order."""
        return [e for e in self.events if isinstance(e, ToolCallEvent)]
