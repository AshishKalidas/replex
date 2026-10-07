"""Agent-aware trace diff: LCS alignment keyed on step type and tool name."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from replex.trace.model import ModelStep, Step, ToolStep, Trace

Status = Literal["same", "changed", "added", "removed"]
_MAX_CHANGES = 50


class FieldChange(BaseModel):
    """One changed field inside an aligned pair of steps."""

    path: str
    before: Any = None
    after: Any = None


class DiffEntry(BaseModel):
    """One row of the aligned diff."""

    status: Status
    kind: Literal["model", "tool"]
    name: str
    a_index: int | None = None
    b_index: int | None = None
    changes: list[FieldChange] = Field(default_factory=list)


class TraceDiff(BaseModel):
    """The aligned difference between two traces (``a`` = baseline, ``b`` = candidate)."""

    a_name: str
    b_name: str
    entries: list[DiffEntry] = Field(default_factory=list)

    def count(self, status: Status) -> int:
        """Number of entries with this status."""
        return sum(1 for e in self.entries if e.status == status)

    @property
    def is_identical(self) -> bool:
        """True when every aligned step is ``same``."""
        return all(e.status == "same" for e in self.entries)

    @property
    def summary(self) -> dict[str, int]:
        """Counts per status."""
        return {s: self.count(s) for s in ("same", "changed", "added", "removed")}


def step_key(step: Step) -> tuple[str, ...]:
    """Alignment key: model steps align with model steps, tools with tools of the same name."""
    return ("tool", step.name) if isinstance(step, ToolStep) else ("model",)


def align(a: Sequence[Any], b: Sequence[Any]) -> list[tuple[int | None, int | None]]:
    """Longest-common-subsequence alignment of two key sequences.

    Returns pairs ``(i, j)``; ``(i, None)`` is a removal, ``(None, j)`` an addition. Between
    two matched pairs, removals are listed before additions.
    """
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    pairs: list[tuple[int | None, int | None]] = []
    removed: list[tuple[int | None, int | None]] = []
    added: list[tuple[int | None, int | None]] = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs += removed + added
            removed, added = [], []
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            removed.append((i, None))
            i += 1
        else:
            added.append((None, j))
            j += 1
    removed += [(k, None) for k in range(i, n)]
    added += [(None, k) for k in range(j, m)]
    return pairs + removed + added


def json_changes(a: Any, b: Any, path: str = "") -> list[FieldChange]:
    """Every leaf-level difference between two JSON-like values."""
    out: list[FieldChange] = []

    def walk(x: Any, y: Any, p: str) -> None:
        if len(out) >= _MAX_CHANGES:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y), key=str):
                sub = f"{p}.{k}" if p else str(k)
                if k not in x:
                    out.append(FieldChange(path=sub, before=None, after=y[k]))
                elif k not in y:
                    out.append(FieldChange(path=sub, before=x[k], after=None))
                else:
                    walk(x[k], y[k], sub)
        elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
            for k, (u, v) in enumerate(zip(x, y, strict=True)):
                walk(u, v, f"{p}[{k}]")
        elif x != y or type(x) is not type(y):
            out.append(FieldChange(path=p or "$", before=x, after=y))

    walk(a, b, path)
    return out


def step_changes(a: Step, b: Step) -> list[FieldChange]:
    """Behavioural differences between two aligned steps.

    For model steps the inputs (messages sent) are not compared: a prompt change alters every
    input, and what matters is whether the model's *behaviour* changed.
    """
    if isinstance(a, ToolStep) and isinstance(b, ToolStep):
        changes = json_changes(a.args, b.args, "args")
        changes += json_changes(a.result, b.result, "result")
        if a.error != b.error:
            changes.append(FieldChange(path="error", before=a.error, after=b.error))
        return changes
    assert isinstance(a, ModelStep)
    assert isinstance(b, ModelStep)
    changes = []
    if a.text != b.text:
        changes.append(FieldChange(path="text", before=a.text, after=b.text))
    a_req = [(r.name, r.args) for r in a.tool_requests]
    b_req = [(r.name, r.args) for r in b.tool_requests]
    if [n for n, _ in a_req] != [n for n, _ in b_req]:
        changes.append(
            FieldChange(
                path="tool_requests",
                before=[n for n, _ in a_req],
                after=[n for n, _ in b_req],
            )
        )
    else:
        for k, ((name, x), (_, y)) in enumerate(zip(a_req, b_req, strict=True)):
            changes += json_changes(x, y, f"tool_requests[{k}].{name}")
    for attr in ("stop_reason", "model", "error"):
        if getattr(a, attr) != getattr(b, attr):
            changes.append(FieldChange(path=attr, before=getattr(a, attr), after=getattr(b, attr)))
    return changes


def diff(a: Trace, b: Trace) -> TraceDiff:
    """Align two traces and report each step as ``same``, ``changed``, ``added`` or ``removed``."""
    entries: list[DiffEntry] = []
    for i, j in align([step_key(s) for s in a.steps], [step_key(s) for s in b.steps]):
        sa = a.steps[i] if i is not None else None
        sb = b.steps[j] if j is not None else None
        ref = sa if sa is not None else sb
        assert ref is not None
        kind: Literal["model", "tool"] = "tool" if isinstance(ref, ToolStep) else "model"
        name = ref.name if isinstance(ref, ToolStep) else "model"
        if sa is not None and sb is not None:
            changes = step_changes(sa, sb)
            entries.append(
                DiffEntry(
                    status="changed" if changes else "same",
                    kind=kind,
                    name=name,
                    a_index=i,
                    b_index=j,
                    changes=changes,
                )
            )
        else:
            entries.append(
                DiffEntry(
                    status="removed" if sb is None else "added",
                    kind=kind,
                    name=name,
                    a_index=i,
                    b_index=j,
                )
            )
    return TraceDiff(a_name=a.name, b_name=b.name, entries=entries)
