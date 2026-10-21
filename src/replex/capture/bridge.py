"""Shared pieces for capturing clients other than ``httpx``.

Their requests are converted to ``httpx.Request`` objects and handled by the same transport
code as ``httpx`` traffic, so every client records, matches, redacts and replays alike and
writes cassettes of the same shape.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import httpx

# Headers httpx adds to a request it builds; the original request already carries what it
# needs, so they are not copied back.
_ADDED_BY_HTTPX = frozenset({"host", "content-length"})


def copy_changed_headers(target: MutableMapping[str, Any], request: httpx.Request) -> None:
    """Copy the headers the capture layer changed on ``request`` back onto the real request."""
    for name, value in request.headers.items():
        if name in _ADDED_BY_HTTPX and name not in target:
            continue
        if target.get(name) != value:
            target[name] = value


def decoded_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    """``headers`` without the ones that describe an encoded body, for one delivered decoded."""
    if "content-encoding" not in headers:
        return headers.multi_items()
    return [
        (k, v) for k, v in headers.multi_items() if k not in ("content-encoding", "content-length")
    ]
