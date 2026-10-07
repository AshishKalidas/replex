"""M4: tool capture and replay."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import enum
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from replex.capture import CaptureContext, HttpSettings, capturing
from replex.capture.tools import (
    UnserialisableValue,
    from_jsonable,
    is_tool,
    to_jsonable,
    tool,
    wrap_tools,
)
from replex.cassette import Cassette, ToolCallEvent, ToolError
from replex.errors import ReplayedToolError, UnrecordedToolCall
from replex.providers import build_trace
from replex.testing import MockProvider, MockReply, MockToolCall

CALLS: list[str] = []


class OrderNotFound(Exception):
    pass


class Order(BaseModel):
    order_id: str
    status: str


@dataclasses.dataclass
class Point:
    x: int
    y: int


class Colour(enum.Enum):
    RED = "red"


@tool
def get_order(order_id: str, verbose: bool = False) -> dict[str, Any]:
    """Look up an order."""
    CALLS.append(f"get_order:{order_id}")
    if order_id == "missing":
        raise OrderNotFound(f"order {order_id} not found")
    return {"order_id": order_id, "status": "shipped", "verbose": verbose}


@tool(safe=True)
async def search_docs(query: str) -> list[str]:
    CALLS.append(f"search_docs:{query}")
    return [f"doc about {query}"]


@tool(name="create_refund")
def create_refund_request(order_id: str, amount: float) -> Order:
    CALLS.append(f"create_refund:{order_id}")
    return Order(order_id=order_id, status="refund_requested")


@tool
def make_point() -> Point:
    return Point(1, 2)


@tool
def weird(value: Any) -> Any:
    return value


@pytest.fixture(autouse=True)
def _reset() -> None:
    CALLS.clear()


def cassette_of(ctx: CaptureContext) -> Cassette:
    return Cassette(name="tools", created_at="t", replex_version="0", events=ctx.finished_events())


def record(fn: Any) -> Cassette:
    ctx = CaptureContext(mode="record")
    with capturing(ctx, patch=False):
        fn()
    return cassette_of(ctx)


def replay(cassette: Cassette, fn: Any, tools_mode: str = "replay") -> CaptureContext:
    ctx = CaptureContext(
        mode="replay", baseline=cassette, tools_mode=tools_mode, cassette_label="tools.replex.json"
    )  # type: ignore[arg-type]
    with capturing(ctx, patch=False):
        fn()
    return ctx


# ------------------------------------------------------------------ outside a session


def test_passthrough_outside_session() -> None:
    assert get_order("1") == {"order_id": "1", "status": "shipped", "verbose": False}
    assert asyncio.run(search_docs("refunds")) == ["doc about refunds"]
    assert CALLS == ["get_order:1", "search_docs:refunds"]
    assert get_order.__name__ == "get_order"
    assert get_order.__doc__ == "Look up an order."
    assert is_tool(get_order) and not is_tool(len)


# ------------------------------------------------------------------ record


def test_record_names_args_applies_defaults_and_records_errors() -> None:
    def run() -> None:
        get_order("1042")
        get_order(order_id="7", verbose=True)
        with pytest.raises(OrderNotFound):
            get_order("missing")
        asyncio.run(search_docs(query="refund window"))

    c = record(run)
    calls = c.tool_calls
    assert [(e.name, e.args) for e in calls] == [
        ("get_order", {"order_id": "1042", "verbose": False}),
        ("get_order", {"order_id": "7", "verbose": True}),
        ("get_order", {"order_id": "missing", "verbose": False}),
        ("search_docs", {"query": "refund window"}),
    ]
    assert calls[2].error == ToolError(
        type="test_tools.OrderNotFound", message="order missing not found"
    )
    assert calls[2].result is None
    assert calls[3].result == ["doc about refund window"]
    assert all(e.duration_ms >= 0 and not e.lossy for e in calls)


def test_record_redacts_args_and_results() -> None:
    @tool
    def lookup_customer(email: str) -> dict[str, str]:
        return {"email": email, "phone": "+1 415 555 0100"}

    c = record(lambda: lookup_customer("jane@example.com"))
    [e] = c.tool_calls
    assert e.args == {"email": "[REDACTED_EMAIL_1]"}
    assert e.result == {"email": "[REDACTED_EMAIL_1]", "phone": "[REDACTED_PHONE_1]"}
    # Replay with the same original value matches the redacted recording.
    ctx = replay(c, lambda: lookup_customer("jane@example.com"))
    assert ctx.stats["tool_executions"] == 0


# ------------------------------------------------------------------ replay


def test_replay_returns_recorded_results_without_executing() -> None:
    def run() -> list[Any]:
        return [
            get_order("1042"),
            asyncio.run(search_docs("x")),
            create_refund_request("1042", 12.5),
        ]

    c = record(run)
    CALLS.clear()
    results: list[Any] = []
    ctx = replay(c, lambda: results.extend(run()))
    assert CALLS == []
    assert ctx.stats["tool_executions"] == 0
    assert results[0] == {"order_id": "1042", "status": "shipped", "verbose": False}
    assert results[1] == ["doc about x"]
    assert isinstance(results[2], Order) and results[2].status == "refund_requested"
    assert [e.name for e in ctx.finished_events()] == ["get_order", "search_docs", "create_refund"]


def test_replay_reraises_recorded_exceptions() -> None:
    def run() -> None:
        with pytest.raises(OrderNotFound):
            get_order("missing")

    c = record(run)
    CALLS.clear()
    with pytest.raises(OrderNotFound, match="order missing not found"):
        replay(c, lambda: get_order("missing"))
    assert CALLS == []


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("ValueError", ValueError),
        ("no.such.module.Error", ReplayedToolError),
        ("builtins.len", ReplayedToolError),
        ("json.JSONDecodeError", ReplayedToolError),  # needs more than a message
    ],
)
def test_exception_rebuild(type_name: str, expected: type[BaseException]) -> None:
    c = Cassette(
        name="t",
        created_at="t",
        replex_version="0",
        events=[
            ToolCallEvent(
                index=0,
                name="get_order",
                args={"order_id": "1", "verbose": False},
                error=ToolError(type=type_name, message="boom"),
            )
        ],
    )
    with pytest.raises(expected, match="boom"):
        replay(c, lambda: get_order("1"))


def test_exception_rebuild_never_imports_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cassette is data: naming a module in an error type must not import it."""
    (tmp_path / "replex_side_effect.py").write_text(
        "import pathlib\npathlib.Path(__file__).with_name('imported').touch()\n"
        "class Boom(Exception):\n    pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    c = Cassette(
        name="t",
        created_at="t",
        replex_version="0",
        events=[
            ToolCallEvent(
                index=0,
                name="get_order",
                args={"order_id": "1", "verbose": False},
                error=ToolError(type="replex_side_effect.Boom", message="boom"),
            )
        ],
    )
    with pytest.raises(ReplayedToolError, match="boom"):
        replay(c, lambda: get_order("1"))
    assert not (tmp_path / "imported").exists()


def test_unrecorded_call_in_replay_mode() -> None:
    c = record(lambda: get_order("1042"))
    with pytest.raises(UnrecordedToolCall) as info:
        replay(c, lambda: get_order("9999"))
    msg = str(info.value)
    assert "'get_order' was called with {'order_id': '9999', 'verbose': False}" in msg
    assert "{'order_id': '1042', 'verbose': False}" in msg
    assert "safe=True" in msg
    assert CALLS == ["get_order:1042"]


def test_each_recording_is_used_once() -> None:
    c = record(lambda: get_order("1"))
    with pytest.raises(UnrecordedToolCall):
        replay(c, lambda: [get_order("1"), get_order("1")])


def test_auto_mode_runs_only_safe_tools() -> None:
    c = record(lambda: get_order("1"))
    CALLS.clear()
    replay(c, lambda: [get_order("1"), asyncio.run(search_docs("new query"))], tools_mode="auto")
    assert CALLS == ["search_docs:new query"]
    with pytest.raises(UnrecordedToolCall):
        replay(c, lambda: get_order("2"), tools_mode="auto")


def test_live_mode_always_executes() -> None:
    c = record(lambda: get_order("1"))
    CALLS.clear()
    ctx = replay(c, lambda: get_order("1"), tools_mode="live")
    assert CALLS == ["get_order:1"]
    assert ctx.stats["tool_executions"] == 1


def test_async_errors_and_unrecorded() -> None:
    @tool
    async def flaky(n: int) -> int:
        if n < 0:
            raise ValueError("negative")
        return n

    async def run() -> None:
        await flaky(1)
        with pytest.raises(ValueError):
            await flaky(-1)

    c = record(lambda: asyncio.run(run()))
    assert [e.error.type if e.error else None for e in c.tool_calls] == [None, "ValueError"]
    replay(c, lambda: asyncio.run(run()))
    with pytest.raises(UnrecordedToolCall):
        replay(c, lambda: asyncio.run(flaky(5)))


# ------------------------------------------------------------------ serialisation


def test_serialisation_fallback_is_flagged_and_replayable() -> None:
    sentinel = object()
    c = record(lambda: weird(sentinel))
    [e] = c.tool_calls
    assert e.lossy
    assert e.args["value"]["__replex_type__"] == "object"
    assert e.result["__replex_repr__"].startswith("<object object")
    out: list[Any] = []
    # A fresh object has a different repr, so replay matches on the recorded repr only via
    # sequence: use live mode for the args but check from_jsonable on the stored result.
    restored = from_jsonable(e.result)
    assert isinstance(restored, UnserialisableValue) and restored.type_name == "object"
    replay(c, lambda: out.append(weird(sentinel)))
    assert isinstance(out[0], UnserialisableValue)


def test_dataclass_return_is_rebuilt() -> None:
    c = record(make_point)
    assert c.tool_calls[0].result == {"x": 1, "y": 2}
    out: list[Any] = []
    replay(c, lambda: out.append(make_point()))
    assert out == [Point(1, 2)]


@pytest.mark.parametrize(
    ("value", "data", "lossy"),
    [
        ({"a": (1, 2), "b": {3, 1}}, {"a": [1, 2], "b": [1, 3]}, False),
        ({1: "x"}, {"1": "x"}, True),
        (Colour.RED, "red", False),
        (dt.date(2026, 10, 7), "2026-10-07", False),
        (b"\x00\x01", {"__replex_bytes__": "AAE="}, False),
        (float("nan"), {"__replex_repr__": "nan", "__replex_type__": "float"}, True),
        ({"a", 1}, None, False),  # unsortable set: order kept as iterated
        (Order(order_id="1", status="s"), {"order_id": "1", "status": "s"}, False),
    ],
)
def test_to_jsonable(value: Any, data: Any, lossy: bool) -> None:
    got, got_lossy = to_jsonable(value)
    if data is not None:
        assert got == data
    assert got_lossy is lossy
    json.dumps(got)


def test_from_jsonable_round_trips_tags() -> None:
    assert from_jsonable({"__replex_bytes__": "AAE="}) == b"\x00\x01"
    nan = from_jsonable({"__replex_repr__": "nan", "__replex_type__": "float"})
    assert nan != nan
    assert from_jsonable([{"a": 1}]) == [{"a": 1}]


# ------------------------------------------------------------------ binding and registries


def test_methods_varargs_and_bad_calls() -> None:
    class Service:
        @tool
        def lookup(self, key: str, *extra: str, **opts: Any) -> str:
            return key + "".join(extra) + str(sorted(opts))

    s = Service()
    c = record(lambda: s.lookup("a", "b", "c", flag=True))
    assert c.tool_calls[0].args == {"key": "a", "extra": ["b", "c"], "flag": True}
    with pytest.raises(TypeError):
        record(lambda: get_order())  # type: ignore[call-arg]


def test_builtin_without_signature() -> None:
    wrapped = tool(name="maximum")(max)
    c = record(lambda: wrapped(1, 5))
    assert c.tool_calls[0].args == {"args": [1, 5], "kwargs": {}}


def test_wrap_tools() -> None:
    def a(x: int) -> int:
        return x + 1

    registry = wrap_tools({"inc": a, "get_order": get_order, "docs": lambda q: q}, safe=["docs"])
    assert registry["get_order"] is get_order
    assert is_tool(registry["inc"]) and registry["inc"](1) == 2
    assert registry["inc"].__replex_tool__.name == "inc"  # type: ignore[attr-defined]
    assert registry["docs"].__replex_tool__.safe  # type: ignore[attr-defined]


# ------------------------------------------------------------------ acceptance: multi-step agent


TOOLS = {"get_order": get_order, "create_refund": create_refund_request}


def run_mini_agent(base_url: str) -> str:
    """A minimal Anthropic-format tool loop over raw httpx."""
    messages: list[Any] = [{"role": "user", "content": "Refund order 1042 please"}]
    with httpx.Client() as client:
        for _ in range(5):
            rsp = client.post(
                base_url + "/v1/messages",
                json={"model": "mock-sonnet", "messages": messages, "max_tokens": 100},
            ).json()
            messages.append({"role": "assistant", "content": rsp["content"]})
            uses = [b for b in rsp["content"] if b["type"] == "tool_use"]
            if not uses:
                return "".join(b["text"] for b in rsp["content"] if b["type"] == "text")
            results = []
            for use in uses:
                out = TOOLS[use["name"]](**use["input"])
                out = out.model_dump() if isinstance(out, BaseModel) else out
                results.append(
                    {"type": "tool_result", "tool_use_id": use["id"], "content": json.dumps(out)}
                )
            messages.append({"role": "user", "content": results})
    raise AssertionError("agent did not finish")


def test_multi_step_agent_replays_with_zero_tool_executions() -> None:
    script = [
        MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
        MockReply(
            tool_calls=[MockToolCall("create_refund", {"order_id": "1042", "amount": 49.99})]
        ),
        MockReply(text="Refund requested for order 1042."),
    ]
    settings = HttpSettings(intercept_hosts=["127.0.0.1"])
    rec = CaptureContext(mode="record", http=settings)
    with MockProvider(script) as mock, capturing(rec):
        answer = run_mini_agent(mock.url)
        port = mock.port
    assert rec.stats["tool_executions"] == 2
    cassette = cassette_of(rec)
    CALLS.clear()

    play = CaptureContext(mode="replay", http=settings, baseline=cassette)
    with capturing(play):
        replayed = run_mini_agent(f"http://127.0.0.1:{port}")  # server is gone: replay only
        play.check_unused()
    assert replayed == answer == "Refund requested for order 1042."
    assert play.stats["tool_executions"] == 0
    assert CALLS == []
    trace = build_trace(
        Cassette(name="t", created_at="t", replex_version="0", events=play.finished_events())
    )
    assert [s.kind for s in trace.steps] == ["model", "tool", "model", "tool", "model"]
    assert trace.tool_names == ["get_order", "create_refund"]
