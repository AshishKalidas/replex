"""M6: trace assertions."""

from __future__ import annotations

import pytest
from tracekit import make_trace

from replex._jsonschema import _errors, parse_json_output, validate
from replex.trace.assertions import describe

T = make_trace(
    "refund",
    "model>get_order",
    ("get_order", {"order_id": "1042"}, {"status": "delivered"}),
    "model>check_refund_policy",
    ("check_refund_policy", {"category": "shoes", "days": 12}),
    "model>create_refund_request",
    ("create_refund_request", {"order_id": "1042", "amount": 49.99}),
    'model:{"refund_id": "R-1", "status": "requested"}',
)


def test_properties() -> None:
    assert T.tool_names == ["get_order", "check_refund_policy", "create_refund_request"]
    assert T.steps_count == 7
    assert len(T.model_calls) == 4 and len(T.tool_calls) == 3


@pytest.mark.parametrize(
    ("expected", "mode"),
    [
        (["get_order", "check_refund_policy", "create_refund_request"], "exact"),
        (["get_order", "create_refund_request"], "subsequence"),
        (["create_refund_request", "get_order", "check_refund_policy"], "same_set"),
    ],
)
def test_tool_sequence_passes(expected: list[str], mode: str) -> None:
    T.assert_tool_sequence(expected, mode=mode)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("expected", "mode"),
    [
        (["get_order", "create_refund_request"], "exact"),
        (["create_refund_request", "get_order"], "subsequence"),
        (["get_order"], "same_set"),
    ],
)
def test_tool_sequence_fails_with_actual_sequence(expected: list[str], mode: str) -> None:
    with pytest.raises(AssertionError) as info:
        T.assert_tool_sequence(expected, mode=mode)  # type: ignore[arg-type]
    msg = str(info.value)
    assert f"tool sequence ({mode}) does not match" in msg
    assert "actual:   ['get_order', 'check_refund_policy', 'create_refund_request']" in msg
    assert "tool   create_refund_request" in msg


def test_tool_sequence_bad_mode() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        T.assert_tool_sequence([], mode="fuzzy")  # type: ignore[arg-type]


def test_assert_called() -> None:
    T.assert_called("get_order")
    T.assert_called("get_order", times=1)
    T.assert_called("create_refund_request", with_args={"amount": 49.99})
    with pytest.raises(AssertionError, match="'search_docs' was never called"):
        T.assert_called("search_docs")
    with pytest.raises(AssertionError, match=r"called 1 time.*expected 2"):
        T.assert_called("get_order", times=2)
    with pytest.raises(AssertionError) as info:
        T.assert_called("get_order", with_args={"order_id": "9"})
    assert '{"order_id": "1042"}' in str(info.value)


def test_assert_called_uses_model_requests_without_tool_steps() -> None:
    t = make_trace("m", "model>get_order,get_order", "model:done")
    t.assert_called("get_order", times=2)


def test_assert_not_called_is_assert_no_tool() -> None:
    T.assert_not_called("delete_*")
    with pytest.raises(AssertionError, match="expected no tool matching"):
        T.assert_not_called("create_*")


def test_assert_no_tool() -> None:
    T.assert_no_tool("delete_*")
    with pytest.raises(
        AssertionError, match=r"no tool matching 'create_\*'.*\['create_refund_request'\]"
    ):
        T.assert_no_tool("create_*")


def test_max_steps_and_tokens() -> None:
    T.assert_max_steps(7)
    T.assert_max_tokens(80)
    with pytest.raises(AssertionError, match="7 steps, more than the maximum of 6"):
        T.assert_max_steps(6)
    with pytest.raises(
        AssertionError, match=r"(?s)80 tokens \(40 in, 40 out\).*per model call: \[20, 20, 20, 20\]"
    ):
        T.assert_max_tokens(79)


def test_final_output() -> None:
    schema = {
        "type": "object",
        "required": ["refund_id", "status"],
        "properties": {"status": {"enum": ["requested", "approved"]}},
    }
    T.assert_final_output(contains="R-1", matches=r'"refund_id": "R-\d+"', json_schema=schema)
    with pytest.raises(AssertionError, match="does not contain 'approved'"):
        T.assert_final_output(contains="approved")
    with pytest.raises(AssertionError, match="does not match /\\^Sorry/"):
        T.assert_final_output(matches="^Sorry")
    with pytest.raises(AssertionError, match=r"(?s)JSON schema.*'amount'"):
        T.assert_final_output(json_schema={"type": "object", "required": ["amount"]})
    with pytest.raises(AssertionError, match="not JSON"):
        make_trace("x", "model:plain words").assert_final_output(json_schema={"type": "object"})


def test_describe_truncates() -> None:
    long = make_trace("l", *[("t", {"blob": "x" * 200}) for _ in range(45)], "model:" + "y" * 100)
    text = describe(long)
    assert "more steps" in text and "…" in text
    assert describe(make_trace("e")) == "  (no steps)"
    err = make_trace("e", ("t", {}))
    err.steps[0].error = "ValueError: boom"  # type: ignore[union-attr]
    assert "-> error: ValueError: boom" in describe(err)


# ------------------------------------------------------------------ JSON schema subset


@pytest.mark.parametrize(
    ("value", "schema", "ok"),
    [
        (1, {"type": "integer"}, True),
        (1.0, {"type": "integer"}, True),
        (True, {"type": "integer"}, False),
        ("a", {"type": ["string", "null"]}, True),
        (None, {"type": "string"}, False),
        (5, {"minimum": 1, "maximum": 4}, False),
        (5, {"exclusiveMinimum": 5}, False),
        (5, {"exclusiveMaximum": 5}, False),
        ("abc", {"minLength": 4}, False),
        ("abcdef", {"maxLength": 4}, False),
        ("R-12", {"pattern": r"^R-\d+$"}, True),
        ("X", {"pattern": r"^R"}, False),
        ([1, 1], {"uniqueItems": True}, False),
        ([1], {"minItems": 2}, False),
        ([1, 2, 3], {"maxItems": 2}, False),
        ([1, "a"], {"items": {"type": "integer"}}, False),
        ({"a": 1, "b": 2}, {"properties": {"a": {}}, "additionalProperties": False}, False),
        ({"a": 1, "b": "x"}, {"additionalProperties": {"type": "integer"}}, False),
        (3, {"const": 3}, True),
        (3, {"const": 4}, False),
        (3, {"enum": [1, 2]}, False),
        (3, {"anyOf": [{"type": "string"}, {"minimum": 1}]}, True),
        (3, {"anyOf": [{"type": "string"}, {"minimum": 5}]}, False),
        (3, {"oneOf": [{"type": "integer"}, {"minimum": 1}]}, False),
        (3, {"allOf": [{"type": "integer"}, {"minimum": 4}]}, False),
        (3, {"not": {"type": "integer"}}, False),
        (3, True, True),
        (3, False, False),
    ],
)
def test_builtin_json_schema(value: object, schema: object, ok: bool) -> None:
    assert (not _errors(value, schema, "$")) is ok


def test_validate_and_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "jsonschema", None)  # force the built-in validator
    assert validate({"a": 1}, {"type": "object", "required": ["b"]}) == [
        "$: missing required property 'b'"
    ]
    assert parse_json_output('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_output(" [1] ") == [1]


def test_validate_uses_jsonschema_when_available() -> None:
    pytest.importorskip("jsonschema")
    errors = validate({"a": "x"}, {"type": "object", "properties": {"a": {"type": "integer"}}})
    assert errors and errors[0].startswith("$.a:")
