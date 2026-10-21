"""Normaliser interface, registry and trace building.

The interpretation layer never touches the network: it only reads recorded exchanges.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from replex.capture.matching import canonical_json
from replex.cassette.model import Cassette, ExchangeEvent, StreamEvent, ToolCallEvent
from replex.pricing import DEFAULT_PRICING, Pricing
from replex.trace.model import ModelStep, ToolStep, Trace, Usage

MOCK_HEADER = "x-replex-mock"
#: Gateways that serve many vendors' models over one wire format, by host. A recording made
#: through one names the gateway as its provider; the wire format alone would misattribute it.
GATEWAY_HOSTS = {"openrouter.ai": "openrouter"}


class Normaliser(ABC):
    """Turns one recorded exchange of a known wire format into a :class:`ModelStep`.

    Subclass it and call :func:`replex.register_normaliser` to support another provider.
    """

    name: ClassVar[str] = "base"

    @abstractmethod
    def matches(self, exchange: ExchangeEvent) -> bool:
        """True if this normaliser understands the exchange (by URL and body shape)."""

    @abstractmethod
    def model_step(self, exchange: ExchangeEvent, index: int) -> ModelStep:
        """Build the model step for one exchange."""

    # helpers shared by subclasses ------------------------------------------------
    @staticmethod
    def path(exchange: ExchangeEvent) -> str:
        """The URL path of the request."""
        return httpx.URL(exchange.request.url).path

    @staticmethod
    def request_body(exchange: ExchangeEvent) -> dict[str, Any]:
        """The request body when it is a JSON object, else an empty dict."""
        body = exchange.request.body
        return body if isinstance(body, dict) else {}

    @staticmethod
    def response_body(exchange: ExchangeEvent) -> dict[str, Any]:
        """The response body when it is a JSON object, else an empty dict."""
        body = exchange.response.body
        return body if isinstance(body, dict) else {}

    @staticmethod
    def stream_data(exchange: ExchangeEvent) -> list[dict[str, Any]]:
        """The JSON object payloads of a streamed response, in order."""
        events: list[StreamEvent] = exchange.response.stream or []
        return [e.data for e in events if isinstance(e.data, dict)]

    @staticmethod
    def digest(messages: Any) -> str:
        """Short stable hash of the messages sent."""
        return "sha256:" + hashlib.sha256(canonical_json(messages).encode()).hexdigest()[:16]


class RawNormaliser(Normaliser):
    """Fallback for unknown wire formats: exposes exchanges without tool semantics."""

    name = "raw"

    def matches(self, exchange: ExchangeEvent) -> bool:
        return True

    def model_step(self, exchange: ExchangeEvent, index: int) -> ModelStep:
        rsp = exchange.response
        if rsp.stream is not None:
            text = "".join(
                e.data if isinstance(e.data, str) else canonical_json(e.data) for e in rsp.stream
            )
        elif isinstance(rsp.body, str):
            text = rsp.body
        else:
            text = "" if rsp.body is None else canonical_json(rsp.body)
        body = exchange.request.body
        return ModelStep(
            index=index,
            model=str(self.request_body(exchange).get("model", "unknown")),
            input_messages_digest=self.digest(body),
            input_messages=[body] if body is not None else [],
            text=text[:4000],
            stop_reason="error" if rsp.status >= 400 else "unknown",
            error=f"HTTP {rsp.status}" if rsp.status >= 400 else None,
            latency_ms=exchange.duration_ms,
            streamed=rsp.stream is not None,
            wire_format=self.name,
        )


_registry: list[Normaliser] = []
_raw = RawNormaliser()


def register_normaliser(normaliser: Normaliser, *, first: bool = True) -> None:
    """Add a normaliser. It is tried before the built-in ones unless ``first=False``."""
    if first:
        _registry.insert(0, normaliser)
    else:
        _registry.append(normaliser)


def unregister_normaliser(normaliser: Normaliser) -> None:
    """Remove a previously registered normaliser."""
    if normaliser in _registry:
        _registry.remove(normaliser)


def normaliser_for(exchange: ExchangeEvent) -> Normaliser:
    """The first registered normaliser that matches, or the raw fallback."""
    for n in _registry:
        if n.matches(exchange):
            return n
    return _raw


def detect_provider(cassette_or_events: Cassette | list[Any]) -> tuple[str, str]:
    """``(provider, wire_format)`` for a set of events.

    The provider is ``mock`` for anything served by RepleX's mock provider, the gateway's
    name when every exchange went through a known gateway (``openrouter``), and otherwise the
    wire format's name.
    """
    events = (
        cassette_or_events.events
        if isinstance(cassette_or_events, Cassette)
        else cassette_or_events
    )
    exchanges = [e for e in events if isinstance(e, ExchangeEvent)]
    if not exchanges:
        return "none", "none"
    formats = sorted({normaliser_for(e).name for e in exchanges})
    wire = formats[0] if len(formats) == 1 else "mixed"
    if any(MOCK_HEADER in e.response.headers for e in exchanges):
        return "mock", wire
    gateways = {GATEWAY_HOSTS.get(httpx.URL(e.request.url).host) for e in exchanges}
    gateway = gateways.pop() if len(gateways) == 1 else None
    return gateway or wire, wire


def _final_output(steps: list[ModelStep]) -> str:
    if not steps:
        return ""
    if steps[-1].text:
        return steps[-1].text
    for s in reversed(steps):
        if s.text:
            return s.text
    return ""


def build_trace(cassette: Cassette, *, pricing: Pricing | None = None) -> Trace:
    """Build the provider-neutral trace of a cassette, priced with ``pricing``.

    ``pricing`` defaults to the built-in price table; pass ``Pricing.from_config(config)`` to
    include the prices from ``[tool.replex]``.
    """
    steps: list[ModelStep | ToolStep] = []
    notes: list[str] = []
    raw_count = 0
    for event in cassette.events:
        index = len(steps)
        if isinstance(event, ExchangeEvent):
            normaliser = normaliser_for(event)
            if normaliser is _raw:
                raw_count += 1
            steps.append(normaliser.model_step(event, index))
        elif isinstance(event, ToolCallEvent):
            error = f"{event.error.type}: {event.error.message}" if event.error else None
            steps.append(
                ToolStep(
                    index=index,
                    name=event.name,
                    args=event.args,
                    result=event.result,
                    error=error,
                    latency_ms=event.duration_ms,
                )
            )
    if raw_count:
        notes.append(
            f"RawNormaliser handled {raw_count} exchange(s) in an unrecognised format: "
            f"they are shown without tool semantics."
        )
    models = [s for s in steps if isinstance(s, ModelStep)]
    executed = any(isinstance(s, ToolStep) for s in steps)
    if not executed and any(m.tool_requests for m in models):
        notes.append(
            "No @replex.tool calls were recorded; tool names come from the model's tool requests."
        )
    usage = Usage()
    for m in models:
        usage = usage + m.usage
    provider, wire = detect_provider(cassette)
    if cassette.meta.provider not in ("unknown", ""):
        provider = cassette.meta.provider
    model = next((m.model for m in models if m.model != "unknown"), cassette.meta.model)
    trace = Trace(
        name=cassette.name,
        provider=provider,
        wire_format=wire,
        model=model,
        steps=steps,
        final_output=_final_output(models),
        usage=usage,
        total_latency_ms=sum(s.latency_ms for s in steps),
        notes=notes,
    )
    return (pricing or DEFAULT_PRICING).priced(trace)
