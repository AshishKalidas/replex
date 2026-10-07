from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from replex.cassette import (
    Cassette,
    CassetteMeta,
    ExchangeEvent,
    RecordedRequest,
    RecordedResponse,
    StreamEvent,
    ToolCallEvent,
    ToolError,
    cassette_path,
    dumps,
    list_cassettes,
    load,
    loads,
    save,
)
from replex.cassette.storage import atomic_write_text, name_from_path, sanitise_name
from replex.errors import CassetteError, CassetteNotFound, UnsupportedSchemaVersion


def make_cassette() -> Cassette:
    return Cassette(
        name="refund_request",
        created_at="2026-10-07T09:00:00Z",
        replex_version="0.1.0",
        meta=CassetteMeta(provider="mock", model="mock-sonnet", git_sha="abc1234", tags=["x"]),
        events=[
            ExchangeEvent(
                index=0,
                request=RecordedRequest(
                    method="POST",
                    url="http://127.0.0.1:1/v1/messages",
                    headers={"x-api-key": "[REDACTED]"},
                    body={"model": "m", "messages": [{"role": "user", "content": "héllo"}]},
                    match_key="sha256:00",
                ),
                response=RecordedResponse(
                    status=200,
                    headers={"content-type": "text/event-stream"},
                    body=None,
                    stream=[StreamEvent(event="message_start", data={"type": "message_start"})],
                ),
                duration_ms=812,
            ),
            ToolCallEvent(
                index=1, name="get_order", args={"order_id": "1042"}, result={"status": "ok"}
            ),
            ToolCallEvent(
                index=2, name="boom", args={}, error=ToolError(type="ValueError", message="no")
            ),
        ],
    )


def test_round_trip_is_lossless(tmp_path: Path) -> None:
    c = make_cassette()
    p = save(c, tmp_path / "c.replex.json")
    loaded = load(p)
    assert loaded == c
    assert loaded.model_dump() == c.model_dump()
    assert [type(e).__name__ for e in loaded.events] == [
        "ExchangeEvent",
        "ToolCallEvent",
        "ToolCallEvent",
    ]


def test_save_twice_is_byte_identical(tmp_path: Path) -> None:
    c = make_cassette()
    a = save(c, tmp_path / "a.replex.json").read_bytes()
    b = save(load(tmp_path / "a.replex.json"), tmp_path / "b.replex.json").read_bytes()
    assert a == b


def test_output_is_deterministic_and_readable() -> None:
    text = dumps(make_cassette())
    assert text.endswith("}\n")
    assert '\n  "created_at"' in text  # two-space indent
    data = json.loads(text)
    assert list(data) == sorted(data)  # sorted keys
    assert "héllo" in text  # not escaped


def test_exchange_and_tool_views() -> None:
    c = make_cassette()
    assert [e.index for e in c.exchanges] == [0]
    assert [e.name for e in c.tool_calls] == ["get_order", "boom"]


def test_unsupported_schema_version_has_clear_error() -> None:
    data = make_cassette().model_dump(mode="json")
    data["schema_version"] = 99
    with pytest.raises(UnsupportedSchemaVersion, match="schema_version 99 is not supported"):
        loads(json.dumps(data))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('{"schema_version": 1, "name": "x"}', "invalid cassette"),
    ],
)
def test_invalid_cassettes(text: str, message: str) -> None:
    with pytest.raises(CassetteError, match=message):
        loads(text)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CassetteNotFound):
        load(tmp_path / "nope.replex.json")


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    atomic_write_text(tmp_path / "sub" / "f.txt", "hello")
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["f.txt"]


def test_atomic_write_cleans_up_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    def boom(*_a: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(tmp_path / "f.txt", "hello")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tests/test_a.py::test_b[x-1]", "tests_test_a.py__test_b_x-1"),
        ("refund_request", "refund_request"),
        ("a/../b", "a_.._b"),
    ],
)
def test_sanitise_name(raw: str, expected: str) -> None:
    assert sanitise_name(raw) == expected


def test_sanitise_rejects_empty() -> None:
    with pytest.raises(CassetteError):
        sanitise_name("///")


def test_paths_and_listing(tmp_path: Path) -> None:
    p = cassette_path(tmp_path, "one")
    assert p.name == "one.replex.json"
    assert name_from_path(p) == "one"
    assert name_from_path("x.json") == "x"
    save(make_cassette(), p)
    save(make_cassette(), cassette_path(tmp_path, "two"))
    (tmp_path / ".hidden.replex.json").write_text("{}")
    assert [x.name for x in list_cassettes(tmp_path)] == ["one.replex.json", "two.replex.json"]
    assert list_cassettes(tmp_path / "missing") == []


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)


@settings(max_examples=60, deadline=None)
@given(body=json_values, result=json_values)
def test_round_trip_property(body: object, result: object) -> None:
    c = Cassette(
        name="p",
        created_at="t",
        replex_version="0",
        events=[
            ExchangeEvent(
                index=0,
                request=RecordedRequest(method="POST", url="u", body=body, match_key="k"),
                response=RecordedResponse(status=200, body=body),
            ),
            ToolCallEvent(index=1, name="t", args={"v": body}, result=result),
        ],
    )
    text = dumps(c)
    assert dumps(loads(text)) == text
