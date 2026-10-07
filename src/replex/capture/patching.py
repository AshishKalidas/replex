"""Automatic mode: patch httpx's default transports so SDK clients are captured unchanged.

The patch is installed when a session starts and removed when it ends, including when the
session body raises.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from contextvars import ContextVar

import httpx

from replex import _runtime

_ORIGINAL_SYNC = httpx.HTTPTransport.handle_request
_ORIGINAL_ASYNC = httpx.AsyncHTTPTransport.handle_async_request
_bypass: ContextVar[bool] = ContextVar("replex_bypass_patch", default=False)
_lock = threading.Lock()
_installed = False


@contextlib.contextmanager
def bypass_patch() -> Iterator[None]:
    """Within this block the patched transports behave like the originals.

    Used by the explicit ``ReplexTransport`` so a request is never captured twice.
    """
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)


def _patched_sync(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    ctx = _runtime.active_capture()
    if ctx is None or _bypass.get():
        return _ORIGINAL_SYNC(self, request)
    from replex.capture.transport import handle_sync

    return handle_sync(ctx, request, lambda r: _ORIGINAL_SYNC(self, r))


async def _patched_async(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
    ctx = _runtime.active_capture()
    if ctx is None or _bypass.get():
        return await _ORIGINAL_ASYNC(self, request)
    from replex.capture.transport import handle_async

    return await handle_async(ctx, request, lambda r: _ORIGINAL_ASYNC(self, r))


def install() -> None:
    """Patch ``httpx.HTTPTransport`` and ``httpx.AsyncHTTPTransport``. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        httpx.HTTPTransport.handle_request = _patched_sync  # type: ignore[method-assign]
        httpx.AsyncHTTPTransport.handle_async_request = _patched_async  # type: ignore[method-assign]
        _installed = True


def uninstall() -> None:
    """Restore the original httpx transports. Idempotent."""
    global _installed
    with _lock:
        httpx.HTTPTransport.handle_request = _ORIGINAL_SYNC  # type: ignore[method-assign]
        httpx.AsyncHTTPTransport.handle_async_request = _ORIGINAL_ASYNC  # type: ignore[method-assign]
        _installed = False


def is_installed() -> bool:
    """Whether the httpx patch is currently active."""
    return _installed
