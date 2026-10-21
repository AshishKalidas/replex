"""Group repeated runs of one scenario by behaviour and measure how consistent it is."""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel

from replex._jsonschema import parse_json_output
from replex.trace.model import Trace

#: Puts a run's outcome into a category (for example the decision in a JSON answer).
Categoriser = Callable[[Trace], str | None]


class Spread(BaseModel):
    """Lowest, median and highest value of a measure across runs."""

    min: int
    median: float
    max: int

    @classmethod
    def of(cls, values: Sequence[int]) -> Spread:
        """The spread of a non-empty sequence of values."""
        return cls(min=min(values), median=float(statistics.median(values)), max=max(values))


class PathVariant(BaseModel):
    """One distinct behaviour: the runs that called the same tools in the same order."""

    tools: list[str]
    category: str | None = None
    runs: list[str]
    share: float
    matches_baseline: bool = False

    @property
    def count(self) -> int:
        """How many runs took this path."""
        return len(self.runs)


class ConsistencyReport(BaseModel):
    """How consistently repeated runs of one scenario behave.

    ``variants`` are ordered most common first; ties keep the order in which the paths first
    appeared. ``baseline_*`` fields are ``None`` when there is no recorded run to compare with.
    """

    name: str
    runs: int
    distinct_paths: int
    variants: list[PathVariant]
    most_common: list[str]
    baseline_tools: list[str] | None = None
    baseline_category: str | None = None
    baseline_share: float | None = None
    baseline_is_majority: bool | None = None
    tokens: Spread
    latency_ms: Spread
    steps: Spread
    categorised: bool = False

    @property
    def consistent(self) -> bool:
        """True when every run took the same path."""
        return self.distinct_paths == 1

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible data."""
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Deterministic JSON text."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def json_field(field: str) -> Categoriser:
    """A categoriser that reads a top-level field of a JSON final answer.

    ``json_field("decision")`` puts a run answering ``{"decision": "refund_denied", ...}`` in
    the category ``refund_denied``. Answers that are not a JSON object, or lack the field, get
    no category (``None``).
    """

    def categorise(trace: Trace) -> str | None:
        try:
            value = parse_json_output(trace.final_output)
        except ValueError:
            return None
        if not isinstance(value, dict) or field not in value:
            return None
        found = value[field]
        return found if isinstance(found, str) else json.dumps(found, sort_keys=True)

    return categorise


def analyse(
    runs: Sequence[Trace],
    *,
    baseline: Trace | None = None,
    labels: Sequence[str] | None = None,
    categorise: Categoriser | None = None,
    name: str | None = None,
) -> ConsistencyReport:
    """Group ``runs`` by behaviour and summarise how consistent they are.

    Args:
        runs: Repeated runs of the same scenario (at least one).
        baseline: The recorded run, to tell whether its path is what most runs do.
        labels: A label per run (default ``run-01``, ``run-02``, ...).
        categorise: Optionally split paths further by outcome, e.g. :func:`json_field`.
        name: Scenario name (default: the first run's name).
    """
    if not runs:
        raise ValueError("no runs to analyse")
    names = (
        list(labels) if labels is not None else [f"run-{i:02d}" for i in range(1, len(runs) + 1)]
    )
    if len(names) != len(runs):
        raise ValueError(f"{len(names)} labels for {len(runs)} runs")

    def key(trace: Trace) -> tuple[tuple[str, ...], str | None]:
        return tuple(trace.tool_names), categorise(trace) if categorise else None

    groups: dict[tuple[tuple[str, ...], str | None], list[str]] = {}
    for label, trace in zip(names, runs, strict=True):
        groups.setdefault(key(trace), []).append(label)
    base_key = key(baseline) if baseline is not None else None
    ordered = sorted(groups.items(), key=lambda item: -len(item[1]))
    variants = [
        PathVariant(
            tools=list(tools),
            category=category,
            runs=members,
            share=len(members) / len(runs),
            matches_baseline=(tools, category) == base_key,
        )
        for (tools, category), members in ordered
    ]
    baseline_share = None
    if base_key is not None:
        baseline_share = len(groups.get(base_key, [])) / len(runs)
    return ConsistencyReport(
        name=name or runs[0].name,
        runs=len(runs),
        distinct_paths=len(variants),
        variants=variants,
        most_common=variants[0].tools,
        baseline_tools=list(base_key[0]) if base_key is not None else None,
        baseline_category=base_key[1] if base_key is not None else None,
        baseline_share=baseline_share,
        baseline_is_majority=None if baseline_share is None else baseline_share > 0.5,
        tokens=Spread.of([t.usage.total for t in runs]),
        latency_ms=Spread.of([t.total_latency_ms for t in runs]),
        steps=Spread.of([t.steps_count for t in runs]),
        categorised=categorise is not None,
    )
