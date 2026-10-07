"""Request normalisation, match keys and the replay mismatch message."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

BodyNormaliser = Callable[[Any], Any]

DEFAULT_IGNORE_FIELDS: tuple[str, ...] = (
    "metadata.request_id",
    "metadata.timestamp",
    "request_id",
    "timestamp",
)

_MAX_DIFF_LINES = 40
_MAX_VALUE_CHARS = 160


def canonical_json(value: Any) -> str:
    """Compact JSON with sorted keys: the form that match keys hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def drop_fields(body: Any, paths: Sequence[str]) -> Any:
    """Remove dotted paths (``*`` matches any key or index) from a JSON value. Returns a copy."""
    if not paths or not isinstance(body, (dict, list)):
        return body
    body = copy.deepcopy(body)
    for path in paths:
        _drop(body, path.strip("$.").split("."))
    return body


def _drop(node: Any, parts: list[str]) -> None:
    if not parts:
        return
    head, rest = parts[0], parts[1:]
    if isinstance(node, dict):
        keys = list(node) if head == "*" else ([head] if head in node else [])
        for key in keys:
            if rest:
                _drop(node[key], rest)
            else:
                del node[key]
    elif isinstance(node, list):
        indexes = range(len(node)) if head == "*" else ([int(head)] if head.isdigit() else [])
        for i in sorted(indexes, reverse=True):
            if i >= len(node):
                continue
            if rest:
                _drop(node[i], rest)
            else:
                del node[i]


def canonicalise_embedded_json(value: Any) -> Any:
    """Rewrite strings that hold a JSON object or array into canonical (sorted-key) form.

    Agents usually send tool results back to the model as ``json.dumps(result)``. Cassettes
    store results with sorted keys, so the replayed result may serialise in a different key
    order than the live one did; comparing embedded JSON canonically makes that irrelevant.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "[") and stripped[-1:] in ("}", "]"):
            try:
                return canonical_json(json.loads(stripped))
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, list):
        return [canonicalise_embedded_json(v) for v in value]
    if isinstance(value, dict):
        return {k: canonicalise_embedded_json(v) for k, v in value.items()}
    return value


def normalise_body(
    body: Any,
    ignore_fields: Sequence[str] = DEFAULT_IGNORE_FIELDS,
    normalisers: Sequence[BodyNormaliser] = (),
) -> Any:
    """Drop volatile fields, canonicalise embedded JSON, then apply user normalisers."""
    body = canonicalise_embedded_json(drop_fields(body, ignore_fields))
    for fn in normalisers:
        body = fn(copy.deepcopy(body))
    return body


def match_key(method: str, url: str, body: Any) -> str:
    """``sha256:`` digest of the method, URL path and canonical normalised body."""
    path = urlsplit(url).path or "/"
    payload = f"{method.upper()} {path}\n{canonical_json(body)}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def args_key(name: str, args: Any) -> str:
    """Identity of a tool call: name plus canonical arguments."""
    return f"{name}:{canonical_json(args)}"


# ----------------------------------------------------------------- diagnostics


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    text = json.dumps(text, ensure_ascii=False) if isinstance(value, str) else text
    return text if len(text) <= _MAX_VALUE_CHARS else text[: _MAX_VALUE_CHARS - 1] + "…"


def first_difference(a: Any, b: Any, path: str = "$") -> tuple[str, Any, Any] | None:
    """The first JSON path at which two values differ, with both values, or ``None``."""
    if type(a) is not type(b):
        return path, a, b
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                return f"{path}.{key}", _MISSING, b[key]
            if key not in b:
                return f"{path}.{key}", a[key], _MISSING
            found = first_difference(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            found = first_difference(x, y, f"{path}[{i}]")
            if found:
                return found
        if len(a) != len(b):
            i = min(len(a), len(b))
            return (
                f"{path}[{i}]",
                a[i] if i < len(a) else _MISSING,
                b[i] if i < len(b) else _MISSING,
            )
        return None
    return None if a == b else (path, a, b)


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _fmt(value: Any) -> str:
    return "<missing>" if value is _MISSING else _short(value)


def body_diff(expected: Any, actual: Any, max_lines: int = _MAX_DIFF_LINES) -> str:
    """A unified diff of two pretty-printed bodies, truncated to ``max_lines``."""

    def pretty(v: Any) -> list[str]:
        text = (
            v if isinstance(v, str) else json.dumps(v, sort_keys=True, indent=2, ensure_ascii=False)
        )
        return text.splitlines()

    lines = list(
        difflib.unified_diff(
            pretty(expected), pretty(actual), "recorded", "actual", n=2, lineterm=""
        )
    )
    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = [*lines[:max_lines], f"... ({hidden} more diff lines)"]
    return "\n".join(lines)


def format_mismatch(
    *,
    request_index: int,
    method: str,
    url: str,
    actual_body: Any,
    expected: tuple[int, str, str, Any] | None,
    cassette: str,
    match_mode: str,
    remaining: int,
) -> str:
    """The message of :class:`~replex.errors.ReplayMismatch`.

    ``expected`` is ``(recorded_index, method, url, body)`` of the closest recorded request.
    """
    path = urlsplit(url).path
    head = f"request #{request_index} ({method} {path}) does not match the recording."
    lines = [head, "", f"  cassette:   {cassette}", f"  match mode: {match_mode}"]
    if expected is None:
        lines += [
            f"  The cassette has no unused recorded exchanges left ({remaining} remaining).",
            "  The agent made more model calls than when it was recorded.",
        ]
    else:
        rec_index, rec_method, rec_url, rec_body = expected
        rec_path = urlsplit(rec_url).path
        lines.append(f"  closest recorded request: event {rec_index} ({rec_method} {rec_path})")
        if (rec_method.upper(), rec_path) != (method.upper(), path):
            lines += ["", f"  The method or path differs: recorded {rec_method} {rec_path}."]
        diff = first_difference(rec_body, actual_body)
        if diff is not None:
            where, before, after = diff
            lines += [
                "",
                f"  First difference at {where}:",
                f"    recorded: {_fmt(before)}",
                f"    actual:   {_fmt(after)}",
            ]
        text = body_diff(rec_body, actual_body)
        if text:
            lines += ["", "  Body diff (recorded -> actual):"]
            lines += [f"    {line}" for line in text.splitlines()]
    lines += [
        "",
        "  If this change is intentional, re-record the baseline:",
        "    pytest --replex-mode=rewrite      (or: replex approve <name> after a drift run)",
        "  To test downstream code against the old responses, use match='sequence'.",
    ]
    return "\n".join(lines)
