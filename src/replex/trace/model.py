"""The provider-neutral trace: ordered model steps and tool steps."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from replex.trace import assertions


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Usage(_Model):
    """Token counts."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ToolRequest(_Model):
    """A tool call requested by the model (not necessarily executed)."""

    id: str | None = None
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ModelStep(_Model):
    """One model call."""

    kind: Literal["model"] = "model"
    index: int
    model: str = "unknown"
    system: str | None = None
    input_messages_digest: str = ""
    input_messages: list[Any] = Field(default_factory=list)
    text: str = ""
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    stop_reason: str = "unknown"
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = 0
    error: str | None = None
    streamed: bool = False
    wire_format: str = "raw"
    #: Cost in US dollars that the provider reported in the response, when it did.
    reported_cost: float | None = None
    #: Cost in US dollars: the reported cost, else tokens times the model's price.
    #: ``None`` when the model has no known price.
    cost: float | None = None


class ToolStep(_Model):
    """One tool execution."""

    kind: Literal["tool"] = "tool"
    index: int
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None
    latency_ms: int = 0


Step = Annotated[ModelStep | ToolStep, Field(discriminator="kind")]


class Trace(_Model):
    """The provider-neutral view of one recorded run.

    Build one with :func:`replex.load_trace`, from ``session.trace``, or with
    :meth:`Trace.from_cassette`. Every ``assert_*`` method raises ``AssertionError`` with a
    message that shows the actual sequence.
    """

    name: str
    provider: str = "unknown"
    wire_format: str = "none"
    model: str = "unknown"
    steps: list[Step] = Field(default_factory=list)
    final_output: str = ""
    usage: Usage = Field(default_factory=Usage)
    #: Total cost in US dollars, or ``None`` when any model step has no known cost.
    cost: float | None = None
    #: Models that have no price, so the total cost is unknown.
    unpriced_models: list[str] = Field(default_factory=list)
    total_latency_ms: int = 0
    notes: list[str] = Field(default_factory=list)

    # ---------------------------------------------------------------- views
    @property
    def model_calls(self) -> list[ModelStep]:
        """The model steps, in order."""
        return [s for s in self.steps if isinstance(s, ModelStep)]

    @property
    def tool_calls(self) -> list[ToolStep]:
        """The executed tool steps, in order."""
        return [s for s in self.steps if isinstance(s, ToolStep)]

    @property
    def tool_names(self) -> list[str]:
        """Names of the tools called, in order.

        Uses executed tool steps (``@replex.tool``). When the run has none, falls back to the
        tools the model requested, so undecorated agents still get tool assertions.
        """
        executed = self.tool_calls
        if executed:
            return [s.name for s in executed]
        return [r.name for m in self.model_calls for r in m.tool_requests]

    @property
    def steps_count(self) -> int:
        """Number of steps (model and tool)."""
        return len(self.steps)

    @classmethod
    def from_cassette(cls, cassette: Any) -> Trace:
        """Build a trace from a :class:`~replex.cassette.Cassette`."""
        from replex.providers import build_trace

        return build_trace(cassette)

    def to_json(self) -> str:
        """Deterministic JSON (sorted keys, two-space indent)."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False
        )

    # ---------------------------------------------------------------- assertions
    def assert_tool_sequence(
        self,
        expected: list[str],
        mode: Literal["exact", "subsequence", "same_set"] = "exact",
    ) -> None:
        """Assert the order of tool calls.

        ``exact``: the same list. ``subsequence``: ``expected`` appears in order, gaps allowed.
        ``same_set``: the same set of names, order and repetition ignored.
        """
        assertions.assert_tool_sequence(self, expected, mode)

    def assert_called(
        self, name: str, times: int | None = None, with_args: dict[str, Any] | None = None
    ) -> None:
        """Assert a tool was called (optionally exactly ``times`` times, with these arguments)."""
        assertions.assert_called(self, name, times, with_args)

    def assert_no_tool(self, pattern: str) -> None:
        """Assert no tool whose name matches the glob ``pattern`` was called."""
        assertions.assert_no_tool(self, pattern)

    def assert_not_called(self, pattern: str) -> None:
        """Alias of :meth:`assert_no_tool`, for those who reach for the mock-style name."""
        assertions.assert_no_tool(self, pattern)

    def assert_max_steps(self, n: int) -> None:
        """Assert the run took at most ``n`` steps."""
        assertions.assert_max_steps(self, n)

    def assert_max_tokens(self, n: int) -> None:
        """Assert the run used at most ``n`` tokens (input plus output)."""
        assertions.assert_max_tokens(self, n)

    def assert_final_output(
        self,
        contains: str | None = None,
        matches: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> None:
        """Assert on the final answer: substring, regex (``re.search``) or JSON schema."""
        assertions.assert_final_output(self, contains, matches, json_schema)
