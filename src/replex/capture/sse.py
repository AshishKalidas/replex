"""Server-sent events: parse a recorded stream and render it back to bytes."""

from __future__ import annotations

import json
from typing import Any

from replex.cassette.model import StreamEvent


def _parse_data(data: str) -> Any:
    stripped = data.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data
    return data


def parse_sse(text: str) -> list[StreamEvent]:
    """Parse an ``text/event-stream`` body into ordered events.

    ``data`` is decoded as JSON when it is an object or array, and kept as a string otherwise
    (for example OpenAI's ``[DONE]`` sentinel). Comment lines are dropped.
    """
    events: list[StreamEvent] = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name: str | None = None
        ident: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
            elif field == "id":
                ident = value
        if name is None and not data_lines and ident is None:
            continue
        data = _parse_data("\n".join(data_lines)) if data_lines else None
        events.append(StreamEvent(event=name, data=data, id=ident))
    return events


def render_event(event: StreamEvent) -> bytes:
    """Render one event back to wire format."""
    out: list[str] = []
    if event.event is not None:
        out.append(f"event: {event.event}")
    if event.id is not None:
        out.append(f"id: {event.id}")
    if event.data is not None:
        data = (
            event.data
            if isinstance(event.data, str)
            else json.dumps(event.data, separators=(",", ":"), ensure_ascii=False)
        )
        out.extend(f"data: {line}" for line in data.split("\n"))
    return ("\n".join(out) + "\n\n").encode("utf-8")
