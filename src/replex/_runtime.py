"""Process-wide state: the active session and its capture context.

Sessions do not nest, so a single global (guarded by a lock) is enough. ``pytest-xdist``
workers are separate processes, each with its own state.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from replex.capture.context import CaptureContext

_lock = threading.Lock()
_capture: CaptureContext | None = None
_owner: object | None = None


def active_capture() -> CaptureContext | None:
    """The capture context of the active session, if any."""
    return _capture


def active_owner() -> object | None:
    """The object (normally a :class:`replex.Session`) that owns the active capture."""
    return _owner


def activate(owner: object, capture: CaptureContext | None) -> bool:
    """Make ``owner`` the active session. Returns False if another one is active."""
    global _capture, _owner
    with _lock:
        if _owner is not None:
            return False
        _owner, _capture = owner, capture
        return True


def deactivate(owner: object) -> None:
    """Clear the active session if ``owner`` holds it."""
    global _capture, _owner
    with _lock:
        if _owner is owner:
            _owner, _capture = None, None
