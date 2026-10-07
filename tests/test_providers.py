"""M3: mock provider + normalisers. Provider gate: text-only, single tool, parallel tools,
multi-step and error responses for each wire format, streaming and non-streaming."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from replex.capture import CaptureContext, HttpSettings, capturing
from replex.cassette import (
    Cassette,
    ExchangeEvent,
    RecordedRequest,
    RecordedResponse,
    StreamEvent,
    ToolCallEvent,
    ToolError,
)
from replex.providers import (
    AnthropicNormaliser,
    Normaliser,
    OpenAICompatNormaliser,
    build_trace,
    detect_provider,
    normaliser_for,
    register_normaliser,
    unregister_normaliser,
)
from replex.testing import MockError, MockProvider, MockReply, MockRequest, MockToolCall
from replex.trace import ModelStep, Trace

SETTINGS = HttpSettings(intercept_hosts=["127.0.0.1"])
WIRES = ["anthropic", "openai"]
STREAMS = [False, True]


def post(
    mock: MockProvider, wire: str, messages: list[Any], stream: bool, system: str = "Be helpful."
) -> httpx.Response:
    tools = ["get_order", "check_refund_policy"]
    if wire == "anthropic":
        url = mock.url + "/v1/messages"
        body: dict[str, Any] = {
            "model": "mock-sonnet",
            "max_tokens": 256,
            "system": system,
            "messages": messages,
            "tools": [{"name": t, "input_schema": {"type": "object"}} for t in tools],
            "stream": stream,
        }
        headers = {
            "x-api-key": "sk-ant-api03-fakefakefakefakefake",
            "anthropic-version": "2023-06-01",
        }
    else:
        url = mock.openai_base_url + "/chat/completions"
        body = {
            "model": "mock-mimo",
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": [
                {"type": "function", "function": {"name": t, "parameters": {}}} for t in tools
            ],
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        headers = {"authorization": "Bearer sk-proj-fakefakefakefakefakefake"}
    with httpx.Client() as c:
        if stream:
            with c.stream("POST", url, json=body, headers=headers) as r:
                r.read()
                return r
        return c.post(url, json=body, headers=headers)


def record_trace(
    script: list[MockReply | str], wire: str, stream: bool, turns: list[list[Any]]
) -> tuple[Trace, Cassette]:
    ctx = CaptureContext(mode="record", http=SETTINGS)
    with MockProvider(script) as mock, capturing(ctx):
        for messages in turns:
            post(mock, wire, messages, stream)
    cassette = Cassette(name="p", created_at="t", replex_version="0", events=ctx.finished_events())
    return build_trace(cassette), cassette


USER = [{"role": "user", "content": "Where is order 1042?"}]


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("wire", WIRES)
def test_text_only(wire: str, stream: bool) -> None:
    trace, cassette = record_trace(
        ["Your order shipped yesterday and arrives Friday."], wire, stream, [USER]
    )
    [step] = trace.model_calls
    assert step.text == "Your order shipped yesterday and arrives Friday."
    assert step.tool_requests == []
    assert step.stop_reason == ("end_turn" if wire == "anthropic" else "stop")
    assert step.streamed is stream
    assert step.system == "Be helpful."
    assert step.usage.input_tokens > 0 and step.usage.output_tokens > 0
    assert trace.final_output == step.text
    assert trace.provider == "mock"
    assert trace.wire_format == wire
    assert trace.model == ("mock-sonnet" if wire == "anthropic" else "mock-mimo")
    assert step.input_messages_digest.startswith("sha256:")
    text = json.dumps(cassette.model_dump())
    assert "fakefakefake" not in text


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("wire", WIRES)
def test_single_tool(wire: str, stream: bool) -> None:
    script = [
        MockReply(
            text="Let me check.",
            tool_calls=[MockToolCall("get_order", {"order_id": "1042", "verbose": True})],
        )
    ]
    trace, _ = record_trace(script, wire, stream, [USER])
    [step] = trace.model_calls
    assert [(r.name, r.args) for r in step.tool_requests] == [
        ("get_order", {"order_id": "1042", "verbose": True})
    ]
    assert step.tool_requests[0].id
    assert step.text == "Let me check."
    assert step.stop_reason == ("tool_use" if wire == "anthropic" else "tool_calls")
    assert trace.tool_names == ["get_order"]  # from the model's requests: no @tool steps
    assert any("tool requests" in n for n in trace.notes)


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("wire", WIRES)
def test_parallel_tools(wire: str, stream: bool) -> None:
    script = [
        MockReply(
            tool_calls=[
                MockToolCall("get_order", {"order_id": "1042"}),
                MockToolCall(
                    "check_refund_policy", {"category": "electronics", "days_since_delivery": 12}
                ),
            ]
        )
    ]
    trace, _ = record_trace(script, wire, stream, [USER])
    [step] = trace.model_calls
    assert [r.name for r in step.tool_requests] == ["get_order", "check_refund_policy"]
    assert step.tool_requests[1].args == {"category": "electronics", "days_since_delivery": 12}
    assert step.text == ""
    assert trace.final_output == ""


def tool_turn(wire: str) -> list[Any]:
    if wire == "anthropic":
        return [
            *USER,
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_order",
                        "input": {"order_id": "1042"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": '{"status": "shipped"}',
                    }
                ],
            },
        ]
    return [
        *USER,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_order", "arguments": '{"order_id": "1042"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"status": "shipped"}'},
    ]


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("wire", WIRES)
def test_multi_step(wire: str, stream: bool) -> None:
    script = [
        MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
        "It has shipped.",
    ]
    trace, cassette = record_trace(script, wire, stream, [USER, tool_turn(wire)])
    first, second = trace.model_calls
    assert [r.name for r in first.tool_requests] == ["get_order"]
    assert second.text == "It has shipped."
    assert first.input_messages_digest != second.input_messages_digest
    assert trace.final_output == "It has shipped."
    assert trace.usage.total == sum(m.usage.total for m in trace.model_calls)
    assert len(cassette.exchanges) == 2
    assert [s.index for s in trace.steps] == [0, 1]


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("wire", WIRES)
def test_error_response(wire: str, stream: bool) -> None:
    script = [MockReply(error=MockError(529, "overloaded_error", "Overloaded"))]
    trace, cassette = record_trace(script, wire, stream, [USER])
    [step] = trace.model_calls
    assert step.stop_reason == "error"
    assert step.error == "overloaded_error: Overloaded"
    assert cassette.exchanges[0].response.status == 529


def test_script_exhausted_and_unknown_paths() -> None:
    with MockProvider([]) as mock, httpx.Client() as c:
        r = c.post(mock.url + "/v1/messages", json={"messages": []})
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "mock_script_exhausted"
        assert c.post(mock.url + "/v1/other", json={}).status_code == 404
        assert c.post(mock.url + "/v1/messages", content=b"{bad").status_code == 400
        assert c.get(mock.url + "/").status_code == 404


def test_responder_sees_neutral_request() -> None:
    seen: list[MockRequest] = []

    def responder(req: MockRequest) -> MockReply:
        seen.append(req)
        return MockReply(text=f"called={req.called_tools} results={req.tool_results}")

    for wire in WIRES:
        with MockProvider(responder) as mock:
            post(mock, wire, tool_turn(wire), stream=False)
    anth, oai = seen
    for req in (anth, oai):
        assert req.called_tools == ["get_order"]
        assert req.tool_results == [("get_order", '{"status": "shipped"}')]
        assert req.last_user_text == "Where is order 1042?"
        assert req.system == "Be helpful."
        assert req.tools == ["get_order", "check_refund_policy"]
    assert anth.wire_format == "anthropic" and oai.wire_format == "openai"


def test_anthropic_list_system_and_content_blocks() -> None:
    def responder(req: MockRequest) -> MockReply:
        return MockReply(text=req.system or "")

    with MockProvider(responder) as mock, httpx.Client() as c:
        r = c.post(
            mock.url + "/v1/messages",
            json={
                "model": "m",
                "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            },
        )
    assert r.json()["content"][0]["text"] == "AB"


# ------------------------------------------------------------------ normalisers in isolation


def exchange(
    url: str, req: Any, rsp: Any = None, stream: list[StreamEvent] | None = None, status: int = 200
) -> ExchangeEvent:
    return ExchangeEvent(
        index=0,
        request=RecordedRequest(method="POST", url=url, body=req, match_key="k"),
        response=RecordedResponse(status=status, body=rsp, stream=stream),
        duration_ms=5,
    )


def test_detection_by_body_shape() -> None:
    a = exchange(
        "https://proxy.example/custom", {"messages": []}, {"type": "message", "content": []}
    )
    o = exchange(
        "https://proxy.example/custom",
        {"messages": []},
        {"object": "chat.completion", "choices": []},
    )
    s = exchange(
        "https://proxy.example/x",
        {},
        stream=[StreamEvent(data={"type": "message_start", "message": {}})],
    )
    c = exchange(
        "https://proxy.example/x",
        {},
        stream=[StreamEvent(data={"object": "chat.completion.chunk", "choices": []})],
    )
    assert isinstance(normaliser_for(a), AnthropicNormaliser)
    assert isinstance(normaliser_for(o), OpenAICompatNormaliser)
    assert isinstance(normaliser_for(s), AnthropicNormaliser)
    assert isinstance(normaliser_for(c), OpenAICompatNormaliser)


def test_raw_fallback_says_so() -> None:
    events: list[Any] = [
        exchange("https://example.com/predict", {"input": "x", "model": "m1"}, {"output": 1}),
        exchange("https://example.com/text", "plain", "plain reply"),
        exchange(
            "https://example.com/s", {}, stream=[StreamEvent(data="a"), StreamEvent(data={"b": 1})]
        ),
        exchange("https://example.com/e", None, None, status=503),
    ]
    trace = build_trace(Cassette(name="r", created_at="t", replex_version="0", events=events))
    assert trace.wire_format == "raw"
    assert trace.provider == "raw"
    assert [m.text for m in trace.model_calls] == ['{"output":1}', "plain reply", 'a{"b":1}', ""]
    assert trace.model_calls[0].model == "m1"
    assert trace.model_calls[3].error == "HTTP 503"
    assert any("RawNormaliser" in n for n in trace.notes)


def test_mixed_and_empty_detection() -> None:
    a = exchange("https://x/v1/messages", {"messages": []}, {"type": "message", "content": []})
    o = exchange("https://x/v1/chat/completions", {"messages": []}, {"choices": []})
    assert detect_provider([a, o]) == ("mixed", "mixed")
    assert detect_provider([]) == ("none", "none")
    assert detect_provider(Cassette(name="n", created_at="t", replex_version="0")) == (
        "none",
        "none",
    )


def test_tool_events_and_meta_provider() -> None:
    events: list[Any] = [
        exchange(
            "https://api.anthropic.com/v1/messages",
            {"model": "claude-sonnet-5-5", "messages": []},
            {
                "type": "message",
                "model": "claude-sonnet-5-5",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "get_order", "input": 5},
                    "junk",
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        ),
        ToolCallEvent(
            index=1, name="get_order", args={"order_id": "1"}, result={"ok": True}, duration_ms=2
        ),
        ToolCallEvent(
            index=2, name="refund", args={}, error=ToolError(type="ValueError", message="nope")
        ),
    ]
    c = Cassette(name="t", created_at="t", replex_version="0", events=events)
    c.meta.provider = "anthropic"
    trace = Trace.from_cassette(c)
    assert trace.provider == "anthropic"
    assert trace.model == "claude-sonnet-5-5"
    assert trace.tool_names == ["get_order", "refund"]
    assert trace.tool_calls[1].error == "ValueError: nope"
    assert trace.model_calls[0].tool_requests[0].args == {"__value__": 5}
    assert trace.total_latency_ms == 7
    assert json.loads(trace.to_json())["name"] == "t"
    assert not trace.notes


def test_stream_edge_cases() -> None:
    anth = exchange(
        "https://x/v1/messages",
        {"messages": [], "system": 7},
        stream=[
            StreamEvent(
                data={
                    "type": "message_start",
                    "message": {"model": "m", "usage": {"input_tokens": 3}},
                }
            ),
            StreamEvent(
                data={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                }
            ),
            StreamEvent(
                data={
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "t", "name": "x", "input": {}},
                }
            ),
            StreamEvent(
                data={
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": "{bad"},
                }
            ),
            StreamEvent(data={"type": "content_block_stop", "index": 1}),
            StreamEvent(
                data={
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 4},
                }
            ),
        ],
    )
    step = AnthropicNormaliser().model_step(anth, 0)
    assert step.text == "hi"
    assert step.system == "7"
    assert step.tool_requests[0].args == {"__raw__": "{bad"}
    assert step.usage.total == 7

    err = exchange(
        "https://x/v1/messages",
        {"messages": []},
        stream=[
            StreamEvent(data={"type": "message_start", "message": {}}),
            StreamEvent(
                data={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
            ),
        ],
    )
    assert AnthropicNormaliser().model_step(err, 0).error == "overloaded_error: busy"

    oai = exchange(
        "https://x/v1/chat/completions",
        {"messages": [{"role": "developer", "content": [{"type": "text", "text": "dev"}]}]},
        stream=[
            StreamEvent(
                data={
                    "object": "chat.completion.chunk",
                    "model": "m2",
                    "choices": [
                        {"index": 1, "delta": {"content": "ignored"}},
                        {"index": 0, "delta": {"content": [{"type": "text", "text": "ok"}]}},
                    ],
                }
            ),
            StreamEvent(data={"error": {"type": "server_error", "message": "x"}}),
        ],
    )
    step = OpenAICompatNormaliser().model_step(oai, 0)
    assert step.error == "server_error: x"
    assert step.system == "dev"

    oai_err = exchange(
        "https://x/v1/chat/completions",
        {"messages": "x"},
        {"error": {"code": "rate_limit", "message": "slow down"}},
        status=429,
    )
    assert OpenAICompatNormaliser().model_step(oai_err, 0).error == "rate_limit: slow down"
    oai_odd = exchange(
        "https://x/v1/chat/completions",
        {"messages": []},
        {
            "choices": [
                {
                    "message": {
                        "content": 5,
                        "tool_calls": [
                            {"id": "c", "function": {"name": "f", "arguments": {"a": 1}}}
                        ],
                    }
                }
            ],
            "usage": "bad",
        },
    )
    step = OpenAICompatNormaliser().model_step(oai_odd, 0)
    assert step.text == "5" and step.tool_requests[0].args == {"a": 1} and step.usage.total == 0


def test_register_custom_normaliser() -> None:
    class Echo(Normaliser):
        name = "echo"

        def matches(self, exchange: ExchangeEvent) -> bool:
            return self.path(exchange) == "/echo"

        def model_step(self, exchange: ExchangeEvent, index: int) -> ModelStep:
            return ModelStep(
                index=index, text=str(self.request_body(exchange).get("say")), wire_format="echo"
            )

    custom = Echo()
    register_normaliser(custom)
    try:
        trace = build_trace(
            Cassette(
                name="e",
                created_at="t",
                replex_version="0",
                events=[exchange("http://h/echo", {"say": "hi"}, {})],
            )
        )
        assert trace.final_output == "hi"
        assert trace.wire_format == "echo"
    finally:
        unregister_normaliser(custom)
    unregister_normaliser(custom)  # idempotent
    assert normaliser_for(exchange("http://h/echo", {}, {})).name == "raw"
