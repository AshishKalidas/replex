"""Automatic mode: patch httpx's default transports so SDK clients are captured unchanged.

The patch is installed when a session starts and removed when it ends, including when the
session body raises.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar

import httpx

from replex import _runtime

SyncHandler = Callable[[httpx.HTTPTransport, httpx.Request], httpx.Response]
AsyncHandler = Callable[[httpx.AsyncHTTPTransport, httpx.Request], Awaitable[httpx.Response]]

_bypass: ContextVar[bool] = ContextVar("replex_bypass_patch", default=False)
_lock = threading.Lock()
# The methods in place when the patch was last installed. Captured at install time, not import
# time, so a patch another library applied in between (instrumentation, respx) keeps working
# underneath ours and is restored afterwards. Kept after uninstall so a request still in
# flight on another thread finishes normally.
_wrapped_sync: SyncHandler = httpx.HTTPTransport.handle_request
_wrapped_async: AsyncHandler = httpx.AsyncHTTPTransport.handle_async_request
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
    inner = _wrapped_sync
    ctx = _runtime.active_capture()
    if ctx is None or _bypass.get():
        return inner(self, request)
    from replex.capture.transport import handle_sync

    return handle_sync(ctx, request, lambda r: inner(self, r))


async def _patched_async(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
    inner = _wrapped_async
    ctx = _runtime.active_capture()
    if ctx is None or _bypass.get():
        return await inner(self, request)
    from replex.capture.transport import handle_async

    return await handle_async(ctx, request, lambda r: inner(self, r))


def install() -> None:
    """Patch ``httpx.HTTPTransport`` and ``httpx.AsyncHTTPTransport``. Idempotent."""
    global _wrapped_sync, _wrapped_async, _installed
    with _lock:
        if _installed:
            return
        _wrapped_sync = httpx.HTTPTransport.handle_request
        _wrapped_async = httpx.AsyncHTTPTransport.handle_async_request
        httpx.HTTPTransport.handle_request = _patched_sync  # type: ignore[method-assign]
        httpx.AsyncHTTPTransport.handle_async_request = _patched_async  # type: ignore[method-assign]
        _installed = True


def uninstall() -> None:
    """Put back the methods that were in place before :func:`install`. Idempotent."""
    global _installed
    with _lock:
        if not _installed:
            return
        httpx.HTTPTransport.handle_request = _wrapped_sync  # type: ignore[method-assign,assignment]
        httpx.AsyncHTTPTransport.handle_async_request = _wrapped_async  # type: ignore[method-assign,assignment]
        _installed = False


def is_installed() -> bool:
    """Whether the httpx patch is currently active."""
    return _installed
