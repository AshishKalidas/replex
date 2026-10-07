"""Behavioural assertions on a trace. Each failure message shows what actually happened."""

from __future__ import annotations

import fnmatch
import json
import re
from typing import TYPE_CHECKING, Any

from replex._jsonschema import parse_json_output, validate

if TYPE_CHECKING:
    from replex.trace.model import Trace


def describe(trace: Trace, limit: int = 40) -> str:
    """A compact, numbered listing of the trace's steps."""
    lines: list[str] = []
    for step in trace.steps[:limit]:
        if step.kind == "tool":
            args = json.dumps(step.args, sort_keys=True, ensure_ascii=False)
            if len(args) > 80:
                args = args[:79] + "…"
            status = f" -> error: {step.error}" if step.error else ""
            lines.append(f"  {step.index:>3}  tool   {step.name}({args}){status}")
        else:
            wants = ", ".join(r.name for r in step.tool_requests)
            text = step.text.replace("\n", " ")
            if len(text) > 60:
                text = text[:59] + "…"
            detail = f"requests [{wants}]" if wants else repr(text)
            lines.append(f"  {step.index:>3}  model  {step.stop_reason}: {detail}")
    if len(trace.steps) > limit:
        lines.append(f"  ... {len(trace.steps) - limit} more steps")
    return "\n".join(lines) if lines else "  (no steps)"


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


def assert_tool_sequence(trace: Trace, expected: list[str], mode: str = "exact") -> None:
    actual = trace.tool_names
    if mode == "exact":
        ok = actual == list(expected)
    elif mode == "subsequence":
        ok = _is_subsequence(list(expected), actual)
    elif mode == "same_set":
        ok = set(actual) == set(expected)
    else:
        raise ValueError(f"unknown mode {mode!r}; use 'exact', 'subsequence' or 'same_set'")
    if not ok:
        raise AssertionError(
            f"tool sequence ({mode}) does not match\n"
            f"  expected: {list(expected)}\n"
            f"  actual:   {actual}\n"
            f"steps:\n{describe(trace)}"
        )


def _args_match(args: dict[str, Any], wanted: dict[str, Any]) -> bool:
    return all(k in args and args[k] == v for k, v in wanted.items())


def _calls(trace: Trace, name: str) -> list[dict[str, Any]]:
    if trace.tool_calls:
        return [s.args for s in trace.tool_calls if s.name == name]
    return [r.args for m in trace.model_calls for r in m.tool_requests if r.name == name]


def assert_called(
    trace: Trace, name: str, times: int | None = None, with_args: dict[str, Any] | None = None
) -> None:
    calls = _calls(trace, name)
    matching = [c for c in calls if with_args is None or _args_match(c, with_args)]
    problem = None
    if not calls:
        problem = f"tool {name!r} was never called"
    elif with_args is not None and not matching:
        shown = "\n".join(f"    {json.dumps(c, sort_keys=True)}" for c in calls[:10])
        problem = (
            f"tool {name!r} was called {len(calls)} time(s) but never with {with_args}\n"
            f"  actual arguments:\n{shown}"
        )
    elif times is not None and len(matching) != times:
        problem = f"tool {name!r} was called {len(matching)} time(s), expected {times}"
    if problem:
        raise AssertionError(
            f"{problem}\n  tools called: {trace.tool_names}\nsteps:\n{describe(trace)}"
        )


def assert_no_tool(trace: Trace, pattern: str) -> None:
    hits = [n for n in trace.tool_names if fnmatch.fnmatchcase(n, pattern)]
    if hits:
        raise AssertionError(
            f"expected no tool matching {pattern!r}, but found {hits}\n"
            f"  tools called: {trace.tool_names}\nsteps:\n{describe(trace)}"
        )


def assert_max_steps(trace: Trace, n: int) -> None:
    if trace.steps_count > n:
        raise AssertionError(
            f"run took {trace.steps_count} steps, more than the maximum of {n}\n"
            f"steps:\n{describe(trace)}"
        )


def assert_max_tokens(trace: Trace, n: int) -> None:
    total = trace.usage.total
    if total > n:
        per_call = ", ".join(str(m.usage.total) for m in trace.model_calls)
        raise AssertionError(
            f"run used {total} tokens ({trace.usage.input_tokens} in, "
            f"{trace.usage.output_tokens} out), more than the maximum of {n}\n"
            f"  per model call: [{per_call}]"
        )


def assert_final_output(
    trace: Trace,
    contains: str | None = None,
    matches: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> None:
    out = trace.final_output
    shown = out if len(out) <= 500 else out[:499] + "…"
    if contains is not None and contains not in out:
        raise AssertionError(
            f"final output does not contain {contains!r}\n  final output: {shown!r}"
        )
    if matches is not None and not re.search(matches, out):
        raise AssertionError(f"final output does not match /{matches}/\n  final output: {shown!r}")
    if json_schema is not None:
        try:
            value = parse_json_output(out)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"final output is not JSON ({exc})\n  final output: {shown!r}"
            ) from exc
        errors = validate(value, json_schema)
        if errors:
            listing = "\n".join(f"    {e}" for e in errors[:10])
            raise AssertionError(f"final output does not match the JSON schema:\n{listing}")
