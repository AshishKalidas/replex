"""Automatic mode: patch the HTTP transports so SDK clients are captured unchanged.

Every captured library (``httpx``, and ``httpx2`` when installed) is patched, and so are
``requests`` and ``aiohttp`` when they are installed. The patch is installed when a session
starts and removed when it ends, including when the session body raises.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from types import ModuleType
from typing import Any, Protocol

from replex import _runtime
from replex.capture.libs import LIBS

SyncHandler = Callable[[Any, Any], Any]
AsyncHandler = Callable[[Any, Any], Awaitable[Any]]

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


class _Patch:
    """The patch for one library.

    The wrapped methods are the ones in place when the patch was installed, captured at install
    time rather than import time, so a patch another library applied in between
    (instrumentation, respx) keeps working underneath ours and is restored afterwards. They are
    kept after uninstall so a request still in flight on another thread finishes normally.
    """

    def __init__(self, lib: ModuleType) -> None:
        self.lib = lib
        self.wrapped_sync: SyncHandler = lib.HTTPTransport.handle_request
        self.wrapped_async: AsyncHandler = lib.AsyncHTTPTransport.handle_async_request
        patch = self

        def patched_sync(transport: Any, request: Any) -> Any:
            inner = patch.wrapped_sync
            ctx = _runtime.active_capture()
            if ctx is None or _bypass.get():
                return inner(transport, request)
            from replex.capture.transport import handle_sync

            return handle_sync(ctx, request, lambda r: inner(transport, r))

        async def patched_async(transport: Any, request: Any) -> Any:
            inner = patch.wrapped_async
            ctx = _runtime.active_capture()
            if ctx is None or _bypass.get():
                return await inner(transport, request)
            from replex.capture.transport import handle_async

            return await handle_async(ctx, request, lambda r: inner(transport, r))

        self.patched_sync = patched_sync
        self.patched_async = patched_async

    def install(self) -> None:
        self.wrapped_sync = self.lib.HTTPTransport.handle_request
        self.wrapped_async = self.lib.AsyncHTTPTransport.handle_async_request
        self.lib.HTTPTransport.handle_request = self.patched_sync
        self.lib.AsyncHTTPTransport.handle_async_request = self.patched_async

    def uninstall(self) -> None:
        self.lib.HTTPTransport.handle_request = self.wrapped_sync
        self.lib.AsyncHTTPTransport.handle_async_request = self.wrapped_async


class ClientPatch(Protocol):
    """The patch for one HTTP client other than ``httpx``."""

    def install(self) -> None: ...
    def uninstall(self) -> None: ...


_patches = [_Patch(lib) for lib in LIBS]

#: The other clients RepleX captures, and the module that patches each. A module is imported
#: only when its client is installed.
CLIENT_MODULES = {
    "requests": "replex.capture.requests_client",
    "aiohttp": "replex.capture.aiohttp_client",
}


@functools.cache
def client_patches() -> tuple[ClientPatch, ...]:
    """The patches for the other clients that are installed and supported."""
    found: list[ClientPatch] = []
    for client, module in CLIENT_MODULES.items():
        if importlib.util.find_spec(client) is None:
            continue
        patch: ClientPatch | None = importlib.import_module(module).make_patch()
        if patch is not None:
            found.append(patch)
    return tuple(found)


def install() -> None:
    """Patch every captured library's ``HTTPTransport`` and ``AsyncHTTPTransport``, and the
    other installed clients. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        for p in (*_patches, *client_patches()):
            p.install()
        _installed = True


def uninstall() -> None:
    """Put back the methods that were in place before :func:`install`. Idempotent."""
    global _installed
    with _lock:
        if not _installed:
            return
        for p in (*_patches, *client_patches()):
            p.uninstall()
        _installed = False


def is_installed() -> bool:
    """Whether the patch is currently active."""
    return _installed
