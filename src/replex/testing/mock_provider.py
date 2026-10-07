"""A local mock model provider that speaks both supported wire formats.

It exists so the test suite, the example and the docs run with no API key. It serves:

* ``POST /v1/messages`` in the Anthropic Messages format, and
* ``POST /v1/chat/completions`` (and ``/chat/completions``) in the OpenAI-compatible format,

each streaming (server-sent events) or not, from a *script*: either a list of replies served
in order, or a function that decides the reply from the request. Every response carries the
``x-replex-mock: true`` header, so cassettes recorded from it are labelled ``provider: mock``.

Example::

    from replex.testing import MockProvider, MockReply, MockToolCall

    script = [
        MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
        MockReply(text="Your order has shipped."),
    ]
    with MockProvider(script) as mock:
        client = anthropic.Anthropic(base_url=mock.url, api_key="test")
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

WireFormat = Literal["anthropic", "openai"]
FIXED_CREATED = 1_760_000_000  # constant "created" timestamp keeps recordings deterministic


@dataclass
class MockToolCall:
    """A tool call the mock model requests."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class MockError:
    """An error response in the provider's own error format."""

    status: int = 500
    type: str = "api_error"
    message: str = "mock provider error"


@dataclass
class MockReply:
    """One scripted model reply: text, tool calls, or an error."""

    text: str = ""
    tool_calls: list[MockToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    error: MockError | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class MockRequest:
    """A provider-neutral view of a request received by the mock, given to responders."""

    wire_format: WireFormat
    model: str
    system: str | None
    messages: list[Any]
    tools: list[str]
    stream: bool
    body: dict[str, Any]

    @property
    def called_tools(self) -> list[str]:
        """Names of tools the assistant has already requested in this conversation."""
        names: list[str] = []
        for m in self.messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, list):
                names += [
                    str(b.get("name"))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
            for call in m.get("tool_calls") or []:
                names.append(str((call.get("function") or {}).get("name")))
        return names

    @property
    def tool_results(self) -> list[tuple[str, str]]:
        """``(tool_name, result_text)`` pairs returned to the model so far, in order."""
        ids: dict[str, str] = {}
        for m in self.messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        ids[str(b.get("id"))] = str(b.get("name"))
            for call in m.get("tool_calls") or []:
                ids[str(call.get("id"))] = str((call.get("function") or {}).get("name"))
        results: list[tuple[str, str]] = []
        for m in self.messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool":
                results.append((ids.get(str(m.get("tool_call_id")), "?"), _text(m.get("content"))))
            content = m.get("content")
            if m.get("role") == "user" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name = ids.get(str(b.get("tool_use_id")), "?")
                        results.append((name, _text(b.get("content"))))
        return results

    @property
    def last_user_text(self) -> str:
        """The text of the most recent user message that is not a tool result."""
        for m in reversed(self.messages):
            if isinstance(m, dict) and m.get("role") == "user":
                text = _text(m.get("content"))
                if text:
                    return text
        return ""


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


Responder = Callable[[MockRequest], MockReply]


def _chunks(text: str, words: int = 3) -> list[str]:
    parts = re.findall(r"\S+\s*|\s+", text)
    return ["".join(parts[i : i + words]) for i in range(0, len(parts), words)] or [""]


def _json_chunks(text: str, size: int = 12) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class MockProvider:
    """A threaded local HTTP server that plays a scripted model.

    Args:
        script: Replies served in order (strings are text replies), or a responder function
            ``(MockRequest) -> MockReply``.
        host: Interface to bind; loopback by default.
        port: Port to bind; ``0`` picks a free one.
    """

    def __init__(
        self,
        script: Sequence[MockReply | str] | Responder,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._responder: Responder | None = script if callable(script) else None
        self._queue: list[MockReply] = (
            []
            if callable(script)
            else [MockReply(text=s) if isinstance(s, str) else s for s in script]
        )
        self._host, self._port = host, port
        self._lock = threading.Lock()
        self._counter = 0
        self.requests: list[MockRequest] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> MockProvider:
        """Start serving in a background thread."""
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                provider._handle(self)

            def do_GET(self) -> None:
                provider._send_json(self, 404, {"error": {"message": "not found"}})

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> MockProvider:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        """The bound port."""
        assert self._server is not None, "MockProvider is not running"
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """Base URL for Anthropic-style clients (``base_url=mock.url``)."""
        return f"http://{self._host}:{self.port}"

    @property
    def openai_base_url(self) -> str:
        """Base URL for OpenAI-compatible clients (``base_url=mock.openai_base_url``)."""
        return f"{self.url}/v1"

    # ------------------------------------------------------------------ handling
    def _next_id(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def _reply_for(self, req: MockRequest) -> MockReply:
        if self._responder is not None:
            return self._responder(req)
        with self._lock:
            if not self._queue:
                return MockReply(
                    error=MockError(
                        500, "mock_script_exhausted", "the mock script has no replies left"
                    )
                )
            return self._queue.pop(0)

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("content-length") or 0)
        raw = h.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(h, 400, {"error": {"message": "invalid JSON"}})
            return
        path = h.path.split("?")[0]
        if path.endswith("/v1/messages"):
            wire: WireFormat = "anthropic"
        elif path.endswith("/chat/completions"):
            wire = "openai"
        else:
            self._send_json(h, 404, {"error": {"message": f"unknown path {path}"}})
            return
        messages = body.get("messages") or []
        if wire == "anthropic":
            system = body.get("system")
            system_text = _text(system) if isinstance(system, list) else system
            tools = [str(t.get("name")) for t in body.get("tools") or []]
        else:
            system_text = (
                "\n".join(
                    _text(m.get("content"))
                    for m in messages
                    if isinstance(m, dict) and m.get("role") in ("system", "developer")
                )
                or None
            )
            tools = [str((t.get("function") or {}).get("name")) for t in body.get("tools") or []]
        req = MockRequest(
            wire_format=wire,
            model=str(body.get("model", "mock-model")),
            system=system_text,
            messages=messages,
            tools=tools,
            stream=bool(body.get("stream")),
            body=body,
        )
        self.requests.append(req)
        reply = self._reply_for(req)
        n = self._next_id()
        in_tok = reply.input_tokens or max(1, len(json.dumps(messages) + (system_text or "")) // 4)
        out_tok = reply.output_tokens or max(
            1, len(reply.text + json.dumps([c.args for c in reply.tool_calls])) // 4
        )
        if reply.error is not None:
            self._send_error(h, wire, reply.error, n)
        elif wire == "anthropic":
            self._anthropic(h, req, reply, n, in_tok, out_tok)
        else:
            self._openai(h, req, reply, n, in_tok, out_tok)

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _headers(h: BaseHTTPRequestHandler, status: int, ctype: str, n: int, length: int) -> None:
        h.send_response(status)
        h.send_header("content-type", ctype)
        h.send_header("content-length", str(length))
        h.send_header("x-replex-mock", "true")
        h.send_header("request-id", f"req_mock_{n:04d}")
        h.end_headers()

    def _send_json(self, h: BaseHTTPRequestHandler, status: int, payload: Any, n: int = 0) -> None:
        data = json.dumps(payload).encode()
        self._headers(h, status, "application/json", n, len(data))
        h.wfile.write(data)

    def _send_sse(
        self, h: BaseHTTPRequestHandler, events: list[tuple[str | None, Any]], n: int
    ) -> None:
        out = []
        for name, data in events:
            payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
            prefix = f"event: {name}\n" if name else ""
            out.append(f"{prefix}data: {payload}\n\n")
        body = "".join(out).encode()
        self._headers(h, 200, "text/event-stream", n, len(body))
        h.wfile.write(body)

    def _send_error(
        self, h: BaseHTTPRequestHandler, wire: WireFormat, err: MockError, n: int
    ) -> None:
        if wire == "anthropic":
            payload: dict[str, Any] = {
                "type": "error",
                "error": {"type": err.type, "message": err.message},
            }
        else:
            payload = {"error": {"message": err.message, "type": err.type, "code": None}}
        self._send_json(h, err.status, payload, n)

    def _anthropic(
        self,
        h: BaseHTTPRequestHandler,
        req: MockRequest,
        reply: MockReply,
        n: int,
        in_tok: int,
        out_tok: int,
    ) -> None:
        blocks: list[dict[str, Any]] = []
        if reply.text:
            blocks.append({"type": "text", "text": reply.text})
        for j, call in enumerate(reply.tool_calls, start=1):
            blocks.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_mock_{n:04d}_{j}",
                    "name": call.name,
                    "input": call.args,
                }
            )
        stop = reply.stop_reason or ("tool_use" if reply.tool_calls else "end_turn")
        message = {
            "id": f"msg_mock_{n:04d}",
            "type": "message",
            "role": "assistant",
            "model": req.model,
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
        if not req.stream:
            self._send_json(h, 200, message, n)
            return
        start = dict(
            message,
            content=[],
            stop_reason=None,
            usage={"input_tokens": in_tok, "output_tokens": 1},
        )
        events: list[tuple[str | None, Any]] = [
            ("message_start", {"type": "message_start", "message": start}),
            ("ping", {"type": "ping"}),
        ]
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                events.append(
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": i,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
                for piece in _chunks(block["text"]):
                    events.append(
                        (
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": i,
                                "delta": {"type": "text_delta", "text": piece},
                            },
                        )
                    )
            else:
                events.append(
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": i,
                            "content_block": dict(block, input={}),
                        },
                    )
                )
                for piece in _json_chunks(json.dumps(block["input"])):
                    events.append(
                        (
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": i,
                                "delta": {"type": "input_json_delta", "partial_json": piece},
                            },
                        )
                    )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": out_tok},
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        self._send_sse(h, events, n)

    def _openai(
        self,
        h: BaseHTTPRequestHandler,
        req: MockRequest,
        reply: MockReply,
        n: int,
        in_tok: int,
        out_tok: int,
    ) -> None:
        calls: list[dict[str, Any]] = [
            {
                "id": f"call_mock_{n:04d}_{j}",
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.args)},
            }
            for j, c in enumerate(reply.tool_calls, start=1)
        ]
        finish = reply.stop_reason or ("tool_calls" if calls else "stop")
        usage = {
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
        }
        base = {"id": f"chatcmpl-mock-{n:04d}", "created": FIXED_CREATED, "model": req.model}
        if not req.stream:
            message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
            if calls:
                message["tool_calls"] = calls
            self._send_json(
                h,
                200,
                dict(
                    base,
                    object="chat.completion",
                    choices=[{"index": 0, "message": message, "finish_reason": finish}],
                    usage=usage,
                ),
                n,
            )
            return

        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            return dict(
                base,
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            )

        events: list[tuple[str | None, Any]] = [(None, chunk({"role": "assistant", "content": ""}))]
        if reply.text:
            events += [(None, chunk({"content": piece})) for piece in _chunks(reply.text)]
        for k, call in enumerate(calls):
            events.append(
                (
                    None,
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": k,
                                    "id": call["id"],
                                    "type": "function",
                                    "function": {"name": call["function"]["name"], "arguments": ""},
                                }
                            ]
                        }
                    ),
                )
            )
            for piece in _json_chunks(call["function"]["arguments"]):
                events.append(
                    (None, chunk({"tool_calls": [{"index": k, "function": {"arguments": piece}}]}))
                )
        events.append((None, chunk({}, finish)))
        if (req.body.get("stream_options") or {}).get("include_usage"):
            events.append(
                (None, dict(base, object="chat.completion.chunk", choices=[], usage=usage))
            )
        events.append((None, "[DONE]"))
        self._send_sse(h, events, n)
