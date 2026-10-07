"""``compare(baseline, candidate, policy) -> DriftReport``."""

from __future__ import annotations

import difflib
import fnmatch
import json
import math
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from replex._jsonschema import parse_json_output, validate
from replex.capture.matching import drop_fields
from replex.cassette.model import Cassette
from replex.cassette.storage import load
from replex.drift.diff import TraceDiff, diff
from replex.drift.policy import Checks, FinalOutputCheck, Policy, ToolArgsCheck
from replex.errors import PolicyError
from replex.providers import build_trace
from replex.trace.model import ModelStep, ToolStep, Trace

FindingSeverity = Literal["fail", "warn", "info"]


class Finding(BaseModel):
    """One policy-relevant difference between baseline and candidate."""

    check: str
    severity: FindingSeverity
    message: str
    step_index: int | None = None
    before: Any = None
    after: Any = None


class TraceSummary(BaseModel):
    """Headline numbers for one side of a comparison."""

    name: str
    provider: str
    model: str
    steps: int
    tool_names: list[str]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    final_output: str

    @classmethod
    def of(cls, trace: Trace) -> TraceSummary:
        """Summarise a trace."""
        return cls(
            name=trace.name,
            provider=trace.provider,
            model=trace.model,
            steps=trace.steps_count,
            tool_names=trace.tool_names,
            input_tokens=trace.usage.input_tokens,
            output_tokens=trace.usage.output_tokens,
            latency_ms=trace.total_latency_ms,
            final_output=trace.final_output,
        )


class DriftReport(BaseModel):
    """The result of comparing a candidate run with its baseline under a policy."""

    name: str
    passed: bool
    findings: list[Finding] = Field(default_factory=list)
    baseline: TraceSummary
    candidate: TraceSummary
    diff: TraceDiff
    checks: dict[str, Any] = Field(default_factory=dict)
    _baseline_trace: Trace | None = PrivateAttr(default=None)
    _candidate_trace: Trace | None = PrivateAttr(default=None)

    @property
    def failures(self) -> list[Finding]:
        """Findings with severity ``fail``."""
        return [f for f in self.findings if f.severity == "fail"]

    @property
    def warnings(self) -> list[Finding]:
        """Findings with severity ``warn``."""
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def baseline_trace(self) -> Trace | None:
        """The full baseline trace (not serialised to JSON)."""
        return self._baseline_trace

    @property
    def candidate_trace(self) -> Trace | None:
        """The full candidate trace (not serialised to JSON)."""
        return self._candidate_trace

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible data."""
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Deterministic JSON text."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    def to_markdown(self) -> str:
        """A Markdown summary (for pull requests and GitHub job summaries)."""
        from replex.drift.report import report_markdown

        return report_markdown(self)

    def to_html(self) -> str:
        """A single-file HTML report (Compare and Drift views, data inlined)."""
        from replex.drift.report import html_report

        return html_report([self])


TraceLike = Trace | Cassette | str | os.PathLike[str]


def as_trace(value: TraceLike) -> Trace:
    """Accept a trace, a cassette or a cassette path."""
    if isinstance(value, Trace):
        return value
    if isinstance(value, Cassette):
        return build_trace(value)
    return build_trace(load(Path(value)))


# ---------------------------------------------------------------------- checks


def _first_divergence(a: list[str], b: list[str]) -> int:
    for k, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return k
    return min(len(a), len(b))


def _tool_step_index(trace: Trace, k: int) -> int | None:
    tools = trace.tool_calls
    if tools:
        return tools[k].index if k < len(tools) else None
    seen = 0
    for m in trace.model_calls:
        for _ in m.tool_requests:
            if seen == k:
                return m.index
            seen += 1
    return None


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


def _check_sequence(base: Trace, cand: Trace, checks: Checks) -> list[Finding]:
    c = checks.tool_sequence
    if c.mode == "ignore" or c.severity == "ignore":
        return []
    a, b = base.tool_names, cand.tool_names
    if c.mode == "exact":
        ok = a == b
    elif c.mode == "subsequence":
        ok = _is_subsequence(a, b)
    else:
        ok = set(a) == set(b)
    if ok:
        return []
    k = _first_divergence(a, b)
    return [
        Finding(
            check="tool_sequence",
            severity=c.severity,
            message=f"tool sequence changed ({c.mode}): {a} -> {b}",
            step_index=_tool_step_index(cand, k),
            before=a,
            after=b,
        )
    ]


def _numbers_close(x: Any, y: Any, tolerance: float) -> bool:
    if isinstance(x, bool) or isinstance(y, bool):
        return bool(x == y)
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return math.isclose(x, y, rel_tol=0.0, abs_tol=tolerance)
    if isinstance(x, dict) and isinstance(y, dict):
        return x.keys() == y.keys() and all(_numbers_close(x[k], y[k], tolerance) for k in x)
    if isinstance(x, list) and isinstance(y, list):
        return len(x) == len(y) and all(
            _numbers_close(u, v, tolerance) for u, v in zip(x, y, strict=True)
        )
    return bool(x == y)


def _args_equal(name: str, a: dict[str, Any], b: dict[str, Any], c: ToolArgsCheck) -> bool:
    override = c.tools.get(name)
    keys = c.ignore_keys + (override.ignore_keys if override else [])
    tol = override.tolerance if override and override.tolerance is not None else c.tolerance
    return _numbers_close(drop_fields(a, keys), drop_fields(b, keys), tol)


def _check_args(base: Trace, cand: Trace, d: TraceDiff, checks: Checks) -> list[Finding]:
    c = checks.tool_args
    if c.mode == "ignore" or c.severity == "ignore":
        return []
    findings: list[Finding] = []
    executed = bool(base.tool_calls or cand.tool_calls)
    for e in d.entries:
        if e.a_index is None or e.b_index is None:
            continue
        sa, sb = base.steps[e.a_index], cand.steps[e.b_index]
        pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        if isinstance(sa, ToolStep) and isinstance(sb, ToolStep):
            pairs.append((sa.name, sa.args, sb.args))
        elif (
            not executed
            and isinstance(sa, ModelStep)
            and isinstance(sb, ModelStep)
            and [r.name for r in sa.tool_requests] == [r.name for r in sb.tool_requests]
        ):
            pairs += [
                (x.name, x.args, y.args)
                for x, y in zip(sa.tool_requests, sb.tool_requests, strict=True)
            ]
        for name, x, y in pairs:
            if not _args_equal(name, x, y, c):
                findings.append(
                    Finding(
                        check="tool_args",
                        severity=c.severity,
                        message=f"arguments of {name!r} changed",
                        step_index=sb.index,
                        before=x,
                        after=y,
                    )
                )
    return findings


def _check_new_tools(base: Trace, cand: Trace, checks: Checks) -> list[Finding]:
    c = checks.new_tools
    if c.severity == "ignore":
        return []
    known = set(base.tool_names)
    new = [n for n in dict.fromkeys(cand.tool_names) if n not in known]
    if not new:
        return []
    first = cand.tool_names.index(new[0])
    return [
        Finding(
            check="new_tools",
            severity=c.severity,
            message=f"candidate calls tool(s) absent from the baseline: {new}",
            step_index=_tool_step_index(cand, first),
            before=sorted(known),
            after=new,
        )
    ]


def _check_forbidden(cand: Trace, checks: Checks) -> list[Finding]:
    c = checks.forbidden_tools
    if c.severity == "ignore" or not c.patterns:
        return []
    findings = []
    for k, name in enumerate(cand.tool_names):
        hit = next((p for p in c.patterns if fnmatch.fnmatchcase(name, p)), None)
        if hit:
            findings.append(
                Finding(
                    check="forbidden_tools",
                    severity=c.severity,
                    message=f"forbidden tool {name!r} was called (matches {hit!r})",
                    step_index=_tool_step_index(cand, k),
                    after=name,
                )
            )
    return findings


def similarity(a: str, b: str, check: FinalOutputCheck | None = None) -> float:
    """Similarity in ``[0, 1]``: ``difflib`` ratio, or embedding cosine if configured."""
    if a == b:
        return 1.0
    if check is not None and check.similarity_backend == "embeddings":
        return _embedding_similarity(a, b, check.embedding_model)
    return difflib.SequenceMatcher(None, a, b).ratio()


_EMBEDDERS: dict[str, Any] = {}


def _embedding_similarity(a: str, b: str, model_name: str) -> float:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise PolicyError(
            "similarity_backend: embeddings needs the 'semantic' extra: "
            "pip install 'replex[semantic]'"
        ) from exc
    model = _EMBEDDERS.get(model_name)
    if model is None:
        model = _EMBEDDERS[model_name] = SentenceTransformer(model_name)
    va, vb = model.encode([a, b])
    dot = float(sum(x * y for x, y in zip(va, vb, strict=True)))
    na = math.sqrt(float(sum(x * x for x in va)))
    nb = math.sqrt(float(sum(x * x for x in vb)))
    return dot / (na * nb) if na and nb else 0.0


def _check_output(base: Trace, cand: Trace, checks: Checks, policy: Policy) -> list[Finding]:
    c = checks.final_output
    if c.mode == "ignore" or c.severity == "ignore":
        return []
    a, b = base.final_output, cand.final_output
    last = cand.model_calls[-1].index if cand.model_calls else None

    def finding(message: str) -> list[Finding]:
        return [
            Finding(
                check="final_output",
                severity=c.severity,
                message=message,
                step_index=last,
                before=a,
                after=b,
            )
        ]

    if c.mode == "exact":
        return [] if a == b else finding("final output changed (exact)")
    if c.mode == "json_equal":
        try:
            ja, jb = parse_json_output(a), parse_json_output(b)
        except json.JSONDecodeError:
            return finding("final output is not valid JSON on both sides (json_equal)")
        return [] if ja == jb else finding("final output JSON changed (json_equal)")
    if c.mode == "json_schema":
        if c.schema_ is None:
            raise PolicyError("final_output mode json_schema needs a 'schema'")
        schema = policy.resolve_schema(c.schema_)
        try:
            value = parse_json_output(b)
        except json.JSONDecodeError:
            return finding("candidate final output is not valid JSON (json_schema)")
        errors = validate(value, schema)
        if errors:
            return finding("candidate final output violates the schema: " + "; ".join(errors[:3]))
        return []
    score = similarity(a, b, c)
    if score < c.threshold:
        return finding(f"final output similarity {score:.2f} is below {c.threshold:.2f}")
    return []


def _check_steps(base: Trace, cand: Trace, checks: Checks) -> list[Finding]:
    c = checks.steps
    if c.severity == "ignore":
        return []
    increase = cand.steps_count - base.steps_count
    if increase <= c.max_increase:
        return []
    return [
        Finding(
            check="steps",
            severity=c.severity,
            message=(
                f"step count rose from {base.steps_count} to {cand.steps_count} "
                f"(+{increase}, allowed +{c.max_increase})"
            ),
            before=base.steps_count,
            after=cand.steps_count,
        )
    ]


def _check_tokens(base: Trace, cand: Trace, checks: Checks) -> list[Finding]:
    c = checks.tokens
    a, b = base.usage.total, cand.usage.total
    if c.severity == "ignore" or a == 0:
        return []
    pct = (b - a) / a * 100
    if pct <= c.max_increase_pct:
        return []
    return [
        Finding(
            check="tokens",
            severity=c.severity,
            message=f"tokens rose from {a} to {b} (+{pct:.0f}%, allowed +{c.max_increase_pct:g}%)",
            before=a,
            after=b,
        )
    ]


def _check_latency(base: Trace, cand: Trace, checks: Checks) -> list[Finding]:
    if checks.latency.severity == "ignore":
        return []
    a, b = base.total_latency_ms, cand.total_latency_ms
    change = f" ({(b - a) / a * 100:+.0f}%)" if a else ""
    return [
        Finding(
            check="latency",
            severity="info",
            message=f"latency {a} ms -> {b} ms{change} (reported only)",
            before=a,
            after=b,
        )
    ]


def compare(
    baseline: TraceLike,
    candidate: TraceLike,
    policy: Policy | str | os.PathLike[str] | None = None,
    *,
    name: str | None = None,
) -> DriftReport:
    """Compare a candidate run with its baseline and evaluate every policy check.

    Args:
        baseline: The committed run (trace, cassette or cassette path).
        candidate: The fresh run.
        policy: A :class:`Policy`, a path to a policy YAML file, or ``None`` for defaults.
        name: Cassette name used to pick per-cassette overrides (defaults to the baseline's).
    """
    from replex.drift.policy import load_policy

    pol = policy if isinstance(policy, Policy) else load_policy(policy)
    base, cand = as_trace(baseline), as_trace(candidate)
    key = name or base.name
    checks = pol.for_cassette(key)
    d = diff(base, cand)
    findings = (
        _check_sequence(base, cand, checks)
        + _check_args(base, cand, d, checks)
        + _check_new_tools(base, cand, checks)
        + _check_forbidden(cand, checks)
        + _check_output(base, cand, checks, pol)
        + _check_steps(base, cand, checks)
        + _check_tokens(base, cand, checks)
        + _check_latency(base, cand, checks)
    )
    report = DriftReport(
        name=key,
        passed=not any(f.severity == "fail" for f in findings),
        findings=findings,
        baseline=TraceSummary.of(base),
        candidate=TraceSummary.of(cand),
        diff=d,
        checks=checks.model_dump(mode="json", by_alias=True),
    )
    report._baseline_trace = base
    report._candidate_trace = cand
    return report
