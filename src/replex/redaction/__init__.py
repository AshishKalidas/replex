"""Redaction of secrets and personal data before anything reaches disk."""

from __future__ import annotations

import json
import os
from pathlib import Path

from replex.redaction.engine import LeakFinding, Redactor, scan_value
from replex.redaction.rules import (
    DEFAULT_PATTERNS,
    HEADER_PLACEHOLDER,
    PatternRule,
    RedactionRules,
    is_sensitive_name,
)


def check_directory(
    directory: str | os.PathLike[str], rules: RedactionRules | None = None
) -> list[LeakFinding]:
    """Scan every ``*.replex.json`` under ``directory`` (recursively) for unredacted data."""
    findings: list[LeakFinding] = []
    root = Path(directory)
    files = [root] if root.is_file() else sorted(root.rglob("*.replex.json"))
    for path in files:
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(LeakFinding(str(path), "", "UNREADABLE", str(exc)[:80]))
            continue
        findings.extend(scan_value(data, rules, file=str(path)))
    return findings


__all__ = [
    "DEFAULT_PATTERNS",
    "HEADER_PLACEHOLDER",
    "LeakFinding",
    "PatternRule",
    "RedactionRules",
    "Redactor",
    "check_directory",
    "is_sensitive_name",
    "scan_value",
]
