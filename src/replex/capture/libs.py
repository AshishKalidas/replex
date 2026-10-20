"""The HTTP client libraries RepleX captures.

``httpx`` is always captured. ``httpx2`` (pydantic's successor to httpx, a separate package
with its own classes) is captured too when it is installed: current releases of the official
Anthropic and OpenAI SDKs send their requests through it. Both expose the same API, so the
capture code works on either; only the classes it constructs or subclasses differ.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

import httpx


def _available() -> tuple[ModuleType, ...]:
    libs: list[ModuleType] = [httpx]
    try:
        libs.append(importlib.import_module("httpx2"))
    except ImportError:
        pass
    return tuple(libs)


LIBS: tuple[ModuleType, ...] = _available()

#: Stream base classes of every captured library. Replayed and recorded streams subclass all
#: of them so each library's ``Response`` accepts them.
SYNC_STREAM_BASES: tuple[type, ...] = tuple(lib.SyncByteStream for lib in LIBS)
ASYNC_STREAM_BASES: tuple[type, ...] = tuple(lib.AsyncByteStream for lib in LIBS)


def lib_for(obj: Any) -> ModuleType:
    """The library an ``httpx``-style object (request, client, transport) belongs to.

    The class hierarchy is searched, so subclasses such as the SDKs' own client wrappers
    resolve to the library they build on.
    """
    for cls in type(obj).__mro__:
        module = sys.modules.get(cls.__module__.partition(".")[0])
        if module in LIBS:
            return module
    return httpx


def lib_named(name: str) -> ModuleType:
    """A captured library by import name, with a clear error when it is not installed."""
    for lib in LIBS:
        if lib.__name__ == name:
            return lib
    raise ImportError(
        f"{name!r} is not installed; RepleX captures {', '.join(lib.__name__ for lib in LIBS)}"
    )
