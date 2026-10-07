"""The redaction engine.

Redaction runs on every event *before* it is written and before the replay match key is
computed. Replacement tokens are stable within one cassette: the same secret always becomes
the same ``[REDACTED_EMAIL_1]``, so the agent's conversation stays internally consistent.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from replex.redaction.rules import (
    HEADER_PLACEHOLDER,
    TOKEN_RE,
    RedactionRules,
    is_sensitive_name,
)


class Redactor:
    """Replaces secrets and configured personal data with stable tokens.

    One redactor is used per cassette so numbering is consistent across its events.
    """

    def __init__(self, rules: RedactionRules | None = None) -> None:
        self.rules = rules or RedactionRules()
        self._tokens: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------ tokens
    def token(self, kind: str, original: str) -> str:
        """The stable token for ``original`` within this redactor."""
        key = (kind, original)
        found = self._tokens.get(key)
        if found is None:
            n = self._counters.get(kind, 0) + 1
            self._counters[kind] = n
            found = f"[REDACTED_{kind}_{n}]"
            self._tokens[key] = found
        return found

    def observe(self, value: Any) -> None:
        """Advance counters past any tokens already present in ``value``.

        Used during replay so that new originals receive the same numbers they were given
        when the cassette was recorded.
        """
        for text in _iter_strings(value):
            for m in TOKEN_RE.finditer(text):
                kind, n = m.group(1), int(m.group(2))
                if n > self._counters.get(kind, 0):
                    self._counters[kind] = n

    def fork(self) -> Redactor:
        """An independent copy (same rules, same tokens so far)."""
        other = Redactor(self.rules)
        other._tokens = dict(self._tokens)
        other._counters = dict(self._counters)
        return other

    def adopt(self, other: Redactor) -> None:
        """Take over the state of a fork (after a successful speculative redaction)."""
        self._tokens = dict(other._tokens)
        self._counters = dict(other._counters)

    # ------------------------------------------------------------------ values
    def text(self, value: str) -> str:
        """Apply every text pattern to a string."""
        for rule in self.rules.patterns:
            if not rule.regex.search(value):
                continue
            pieces: list[str] = []
            pos = 0
            for m in rule.regex.finditer(value):
                start, end = m.span(rule.group)
                if start < 0 or start < pos:
                    continue
                pieces.append(value[pos:start])
                pieces.append(self.token(rule.kind, value[start:end]))
                pos = end
            pieces.append(value[pos:])
            value = "".join(pieces)
        return value

    def value(self, obj: Any) -> Any:
        """Redact a JSON-compatible value: configured paths first, then text patterns."""
        if self.rules.paths:
            obj = copy.deepcopy(obj)
            for path in self.rules.paths:
                obj = self._redact_path(obj, path)
        return self._walk(obj)

    def _walk(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self._walk(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self._walk(v) for v in obj)
        if isinstance(obj, dict):
            return {k: self._walk(v) for k, v in obj.items()}
        return obj

    def _redact_path(self, obj: Any, path: tuple[str, ...]) -> Any:
        if not path:
            if isinstance(obj, str) and TOKEN_RE.fullmatch(obj):
                return obj
            original = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True)
            return self.token("FIELD", original)
        head, rest = path[0], path[1:]
        if isinstance(obj, dict):
            for key in list(obj):
                if head in ("*", key):
                    obj[key] = self._redact_path(obj[key], rest)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if head == "*" or head == str(i):
                    obj[i] = self._redact_path(item, rest)
        return obj

    def headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Redact header values. Secret-bearing names are always replaced outright."""
        out: dict[str, str] = {}
        for name, value in headers.items():
            key = name.lower()
            out[key] = HEADER_PLACEHOLDER if is_sensitive_name(key) else self.text(value)
        return out

    def url(self, url: str) -> str:
        """Redact secret query parameters and any pattern found in the URL."""
        parts = urlsplit(url)
        if parts.query:
            pairs = [
                (k, HEADER_PLACEHOLDER if is_sensitive_name(k) else self.text(v))
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
            ]
            parts = parts._replace(query=urlencode(pairs, safe="[]"))
        return self.text(urlunsplit(parts))


def _iter_strings(obj: Any) -> Any:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)


# ---------------------------------------------------------------------- scanning


@dataclass(frozen=True)
class LeakFinding:
    """Something in a cassette that looks like an unredacted secret."""

    file: str
    location: str
    kind: str
    excerpt: str


def _mask(value: str) -> str:
    return value[:4] + "…" if len(value) > 4 else "…"


def scan_value(
    obj: Any, rules: RedactionRules | None = None, *, file: str = ""
) -> list[LeakFinding]:
    """Find unredacted secrets anywhere in a decoded cassette (or any JSON value).

    Header maps are recognised by their key, so a ``headers`` object whose sensitive
    entries are not ``[REDACTED]`` is reported even if the value looks harmless.
    """
    rules = rules or RedactionRules()
    findings: list[LeakFinding] = []

    def visit(node: Any, loc: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                child = f"{loc}.{k}" if loc else str(k)
                if k == "headers" and isinstance(v, dict):
                    for hk, hv in v.items():
                        if is_sensitive_name(str(hk)) and hv != HEADER_PLACEHOLDER:
                            findings.append(
                                LeakFinding(file, f"{child}.{hk}", "HEADER", _mask(str(hv)))
                            )
                visit(v, child)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, f"{loc}[{i}]")
        elif isinstance(node, str):
            stripped = TOKEN_RE.sub("", node)
            for rule in rules.patterns:
                for m in rule.regex.finditer(stripped):
                    findings.append(LeakFinding(file, loc, rule.kind, _mask(m.group(rule.group))))

    visit(obj, "")
    return findings
