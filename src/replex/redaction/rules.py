"""Redaction rules: which headers, query parameters and text patterns are secret."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from replex.errors import ConfigError

HEADER_PLACEHOLDER = "[REDACTED]"
TOKEN_RE = re.compile(r"\[REDACTED_([A-Z][A-Z0-9_]*?)_(\d+)\]")

SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie", "set-cookie"}
)
SENSITIVE_NAME_GLOBS: tuple[str, ...] = ("*key*", "*token*", "*secret*")


@dataclass(frozen=True)
class PatternRule:
    """A text pattern. The matched ``group`` is replaced with a stable ``[REDACTED_<KIND>_n]``."""

    kind: str
    regex: re.Pattern[str]
    group: int = 0


DEFAULT_PATTERNS: tuple[PatternRule, ...] = (
    PatternRule("BEARER", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{8,}=*)"), group=1),
    PatternRule(
        "API_KEY",
        re.compile(
            r"(?<![A-Za-z0-9])(?:"
            r"sk-ant-[A-Za-z0-9_\-]{8,}"
            r"|sk-(?:proj-|live-|test-|svcacct-)?[A-Za-z0-9_\-]{20,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|gh[pousr]_[A-Za-z0-9]{30,}"
            r"|xox[abprs]-[A-Za-z0-9\-]{10,}"
            r"|AIza[0-9A-Za-z_\-]{35}"
            r")"
        ),
    ),
    PatternRule(
        "EMAIL",
        re.compile(r"(?<![\w.+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"),
    ),
    PatternRule(
        "PHONE",
        re.compile(
            r"(?<![\w+])(?:"
            r"\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?(?:[\s.\-]?\d{2,4}){2,3}"
            r"|\(\d{3}\)\s?\d{3}[\s.\-]\d{4}"
            r"|\d{3}[\s.\-]\d{3}[\s.\-]\d{4}"
            r")(?![\w])"
        ),
    ),
)


def is_sensitive_name(name: str) -> bool:
    """True for header or query parameter names that always carry secrets."""
    lowered = name.lower()
    if lowered in SENSITIVE_HEADERS:
        return True
    return any(fnmatch.fnmatchcase(lowered, glob) for glob in SENSITIVE_NAME_GLOBS)


def _kind_from(name: str) -> str:
    kind = re.sub(r"[^A-Z0-9_]", "_", name.upper()).strip("_")
    if not kind or not kind[0].isalpha():
        kind = f"C_{kind}" if kind else "CUSTOM"
    return kind


@dataclass(frozen=True)
class RedactionRules:
    """The full rule set used by a :class:`~replex.redaction.engine.Redactor`.

    Args:
        patterns: Text patterns applied to every string value.
        paths: Dotted JSON paths (``*`` matches any key or index) whose whole value is replaced.
    """

    patterns: tuple[PatternRule, ...] = DEFAULT_PATTERNS
    paths: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    @classmethod
    def from_config(
        cls,
        redact_patterns: Sequence[str | Mapping[str, Any]] = (),
        redact_paths: Iterable[str] = (),
    ) -> RedactionRules:
        """Build rules from the ``redact_patterns`` and ``redact_paths`` settings.

        A pattern is either a regex string (kind ``CUSTOM``) or a mapping with ``pattern``
        and an optional ``name`` used as the token kind (``{name = "order_ref"}`` gives
        ``[REDACTED_ORDER_REF_1]``).
        """
        extra: list[PatternRule] = []
        for item in redact_patterns:
            if isinstance(item, str):
                pattern, kind = item, "CUSTOM"
            elif isinstance(item, Mapping) and isinstance(item.get("pattern"), str):
                pattern, kind = item["pattern"], _kind_from(str(item.get("name", "CUSTOM")))
            else:
                raise ConfigError(
                    f"redact_patterns entries must be a regex string or a table with a "
                    f"'pattern' key, got {item!r}"
                )
            try:
                extra.append(PatternRule(kind, re.compile(pattern)))
            except re.error as exc:
                raise ConfigError(f"invalid redact pattern {pattern!r}: {exc}") from exc
        paths = tuple(tuple(p.strip("$.").split(".")) for p in redact_paths if p.strip("$."))
        return cls(patterns=DEFAULT_PATTERNS + tuple(extra), paths=paths)
