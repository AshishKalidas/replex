"""M6: alignment golden tests and properties."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tracekit import make_trace

from replex import diff
from replex.drift.diff import align, json_changes

M = "model>x"


def statuses(d: object) -> list[tuple[str, str, int | None, int | None]]:
    return [(e.status, e.name, e.a_index, e.b_index) for e in d.entries]  # type: ignore[attr-defined]


def test_golden_removed_check() -> None:
    a = make_trace(
        "a",
        "model>get_order",
        ("get_order", {"id": 1}),
        "model>check",
        ("check", {}),
        "model>refund",
        ("refund", {"id": 1}),
        "model:done",
    )
    b = make_trace(
        "b",
        "model>get_order",
        ("get_order", {"id": 1}),
        "model>refund",
        ("refund", {"id": 1}),
        "model:done",
    )
    assert statuses(diff(a, b)) == [
        ("same", "model", 0, 0),
        ("same", "get_order", 1, 1),
        ("changed", "model", 2, 2),
        ("removed", "check", 3, None),
        ("removed", "model", 4, None),
        ("same", "refund", 5, 3),
        ("same", "model", 6, 4),
    ]
    changed = diff(a, b).entries[2]
    assert changed.changes[0].path == "tool_requests"
    assert (changed.changes[0].before, changed.changes[0].after) == (["check"], ["refund"])


def test_golden_added_tool_and_changed_args() -> None:
    a = make_trace("a", "model>get_order", ("get_order", {"id": 1, "v": [1, 2]}), "model:Shipped.")
    b = make_trace(
        "b",
        "model>get_order",
        ("get_order", {"id": 2, "v": [1, 3]}),
        "model>search",
        ("search", {"q": "x"}),
        "model:Shipped!",
    )
    d = diff(a, b)
    assert statuses(d) == [
        ("same", "model", 0, 0),
        ("changed", "get_order", 1, 1),
        ("changed", "model", 2, 2),
        ("added", "search", None, 3),
        ("added", "model", None, 4),
    ]
    assert [(c.path, c.before, c.after) for c in d.entries[1].changes] == [
        ("args.id", 1, 2),
        ("args.v[1]", 2, 3),
    ]
    assert d.summary == {"same": 1, "changed": 2, "added": 2, "removed": 0}
    assert not d.is_identical


def test_golden_reorder() -> None:
    a = make_trace("a", ("get_order", {}), ("check", {}))
    b = make_trace("b", ("check", {}), ("get_order", {}))
    assert statuses(diff(a, b)) == [
        ("removed", "get_order", 0, None),
        ("same", "check", 1, 0),
        ("added", "get_order", None, 1),
    ]


def test_golden_empty_and_model_fields() -> None:
    assert diff(make_trace("a"), make_trace("b")).entries == []
    a = make_trace("a", "model:hi")
    b = make_trace("b", "model:hi", model="other")
    b.steps[0].stop_reason = "max_tokens"  # type: ignore[union-attr]
    paths = [c.path for c in diff(a, b).entries[0].changes]
    assert paths == ["stop_reason", "model"]
    c = make_trace("c", ("t", {"a": 1}, {"r": 1}))
    e = make_trace("e", ("t", {"a": 1}, {"r": 2}))
    e.steps[0].error = "ValueError: x"  # type: ignore[union-attr]
    assert [x.path for x in diff(c, e).entries[0].changes] == ["result.r", "error"]


def test_tool_request_arg_changes_on_model_steps() -> None:
    a = make_trace("a", "model>t")
    b = make_trace("b", "model>t")
    b.steps[0].tool_requests[0].args = {"k": 1}  # type: ignore[union-attr]
    assert [c.path for c in diff(a, b).entries[0].changes] == ["tool_requests[0].t.k"]


def test_json_changes() -> None:
    assert [
        c.path for c in json_changes({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2, 3], "c": 0})
    ] == ["b", "c"]
    assert json_changes({"x": 1}, {})[0].after is None
    assert json_changes(1, 1.0)[0].path == "$"
    big = json_changes({str(i): i for i in range(80)}, {str(i): -i - 1 for i in range(80)})
    assert len(big) == 50


# ------------------------------------------------------------------ properties

step = st.one_of(
    st.just("model:a"),
    st.just("model:b"),
    st.just("model>t1"),
    st.tuples(st.sampled_from(["t1", "t2", "t3"]), st.fixed_dictionaries({"x": st.integers(0, 2)})),
)


@settings(max_examples=200, deadline=None)
@given(spec=st.lists(step, max_size=12))
def test_diff_with_itself_is_all_same(spec: list[object]) -> None:
    t = make_trace("t", *spec)
    d = diff(t, t)
    assert all(e.status == "same" for e in d.entries)
    assert len(d.entries) == len(spec)
    assert d.is_identical


@settings(max_examples=200, deadline=None)
@given(
    a=st.lists(st.sampled_from("ABCD"), max_size=10),
    b=st.lists(st.sampled_from("ABCD"), max_size=10),
)
def test_alignment_is_a_valid_lcs(a: list[str], b: list[str]) -> None:
    pairs = align(a, b)
    left = [i for i, _ in pairs if i is not None]
    right = [j for _, j in pairs if j is not None]
    assert sorted(left) == list(range(len(a)))
    assert sorted(right) == list(range(len(b)))
    matched = [(i, j) for i, j in pairs if i is not None and j is not None]
    assert all(a[i] == b[j] for i, j in matched)
    assert [i for i, _ in matched] == sorted(i for i, _ in matched)
    assert [j for _, j in matched] == sorted(j for _, j in matched)

    # optimal: matched length equals the LCS length
    def lcs(x: list[str], y: list[str]) -> int:
        dp = [[0] * (len(y) + 1) for _ in range(len(x) + 1)]
        for i in range(len(x)):
            for j in range(len(y)):
                dp[i + 1][j + 1] = dp[i][j] + 1 if x[i] == y[j] else max(dp[i][j + 1], dp[i + 1][j])
        return dp[-1][-1]

    assert len(matched) == lcs(a, b)


@pytest.mark.parametrize("n", [0, 1, 5])
def test_diff_is_symmetric_in_counts(n: int) -> None:
    a = make_trace("a", *(("t1", {"x": i}) for i in range(n)))
    b = make_trace("b", *(("t1", {"x": i}) for i in range(n)), ("t2", {}))
    assert diff(a, b).count("added") == 1
    assert diff(b, a).count("removed") == 1
