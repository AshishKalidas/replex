"""Capture layer: HTTP transports, httpx patching and tool capture."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from replex import _runtime
from replex.capture import patching
from replex.capture.context import CaptureContext, HttpSettings
from replex.errors import SessionActive


@contextlib.contextmanager
def capturing(
    ctx: CaptureContext, owner: object | None = None, *, patch: bool = True
) -> Iterator[CaptureContext]:
    """Activate ``ctx`` (and the httpx patch) for the duration of the block.

    The patch is removed and the context deactivated even if the block raises.
    """
    owner = owner if owner is not None else ctx
    if not _runtime.activate(owner, ctx):
        raise SessionActive(
            "a RepleX session is already active; sessions do not nest. "
            "Finish the current session before starting another."
        )
    try:
        if patch:
            patching.install()
        yield ctx
    finally:
        try:
            ctx.finalize_streams()
        finally:
            if patch:
                patching.uninstall()
            _runtime.deactivate(owner)


__all__ = ["CaptureContext", "HttpSettings", "capturing"]
