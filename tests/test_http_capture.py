"""M2: HTTP capture and replay against a plain local HTTP server.

Every capture test runs in both sync and async form (``kind`` parameter).
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from replex import _runtime
from replex.capture import CaptureContext, HttpSettings, capturing, patching
from replex.capture.matching import body_diff, drop_fields, first_difference, match_key
from replex.capture.sse import parse_sse, render_event
from replex.capture.transport import (
    AsyncReplexTransport,
    ReplexTransport,
    async_http_client,
    http_client,
)
from replex.cassette import Cassette, StreamEvent, dumps
from replex.errors import NetworkBlocked, ReplayMismatch, SessionActive, UnusedRecording

KINDS = ["sync", "async"]
INTERCEPT = HttpSettings(intercept_hosts=["127.0.0.1"], passthrough_hosts=["localhost"])


# ----------------------------------------------------------------- helpers


def request(kind: str, method: str, url: str, **kw: Any) -> httpx.Response:
    """One request through a default (patchable) client."""
    if kind == "sync":
        with httpx.Client() as c:
            return c.request(method, url, **kw)

    async def go() -> httpx.Response:
        async with httpx.AsyncClient() as c:
            r = await c.request(method, url, **kw)
            await r.aread()
            return r

    return asyncio.run(go())


def stream_lines(kind: str, url: str, client_factory: Callable[[], Any] | None = None) -> list[str]:
    """Consume a streamed response line by line."""
    if kind == "sync":
        with client_factory() if client_factory else httpx.Client() as c:
            with c.stream("GET", url) as r:
                return list(r.iter_lines())

    async def go() -> list[str]:
        async with client_factory() if client_factory else httpx.AsyncClient() as c:
            async with c.stream("GET", url) as r:
                return [line async for line in r.aiter_lines()]

    return asyncio.run(go())


def record(settings: HttpSettings, fn: Callable[[], Any]) -> tuple[Cassette, Any]:
    ctx = CaptureContext(mode="record", http=settings)
    with capturing(ctx):
        out = fn()
    return to_cassette(ctx), out


def to_cassette(ctx: CaptureContext) -> Cassette:
    return Cassette(
        name="t",
        created_at="2026-10-07T00:00:00Z",
        replex_version="0.1.0",
        events=ctx.finished_events(),
    )


def replay(
    cassette: Cassette, settings: HttpSettings, fn: Callable[[], Any]
) -> tuple[CaptureContext, Any]:
    ctx = CaptureContext(
        mode="replay", http=settings, baseline=cassette, cassette_label="t.replex.json"
    )
    with capturing(ctx):
        out = fn()
        ctx.check_unused()
    return ctx, out


# ----------------------------------------------------------------- record / replay


@pytest.mark.parametrize("kind", KINDS)
def test_record_then_replay_json(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/echo"
    body = {"messages": [{"role": "user", "content": "hi"}], "request_id": "volatile-1"}
    cassette, live = record(
        INTERCEPT,
        lambda: request(
            kind, "POST", url, json=body, headers={"x-api-key": "sk-ant-secretsecretsecret"}
        ),
    )
    assert live.json()["hit"] == 1
    [event] = cassette.exchanges
    assert event.request.headers["x-api-key"] == "[REDACTED]"
    assert event.response.headers["x-request-token"] == "[REDACTED]"
    assert event.response.body["echo"]["messages"][0]["content"] == "hi"
    assert "sk-ant-secret" not in dumps(cassette)

    body2 = dict(body, request_id="volatile-2")  # ignored field differs: still matches
    _, replayed = replay(cassette, INTERCEPT, lambda: request(kind, "POST", url, json=body2))
    assert replayed.json() == live.json()
    assert plain_server.hits == 1  # served from the cassette


@pytest.mark.parametrize("kind", KINDS)
def test_replay_twice_is_byte_identical(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/a"

    def run() -> list[Any]:
        return [request(kind, "POST", url, json={"i": i}).json() for i in range(3)]

    cassette, _ = record(INTERCEPT, run)
    ctx1, out1 = replay(cassette, INTERCEPT, run)
    ctx2, out2 = replay(cassette, INTERCEPT, run)
    assert out1 == out2
    dump1 = [e.model_dump_json() for e in ctx1.finished_events()]
    dump2 = [e.model_dump_json() for e in ctx2.finished_events()]
    assert dump1 == dump2


@pytest.mark.parametrize("kind", KINDS)
def test_replay_makes_no_network_calls(
    kind: str, plain_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = plain_server.url + "/x"
    cassette, live = record(INTERCEPT, lambda: request(kind, "POST", url, json={"q": 1}))

    def no_network(*_a: Any, **_k: Any) -> None:
        raise AssertionError("network access attempted during replay")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    _, replayed = replay(cassette, INTERCEPT, lambda: request(kind, "POST", url, json={"q": 1}))
    assert replayed.json() == live.json()


@pytest.mark.parametrize("kind", KINDS)
def test_streaming_fidelity(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/stream"
    cassette, live_lines = record(INTERCEPT, lambda: stream_lines(kind, url))
    [event] = cassette.exchanges
    stream = event.response.stream
    assert stream is not None
    assert [e.event for e in stream] == ["start", "delta", "delta", None]
    assert stream[-1].data == "[DONE]"
    assert stream[1].data == {"type": "delta", "text": "Hel"}

    _, replayed_lines = replay(cassette, INTERCEPT, lambda: stream_lines(kind, url))
    assert [line for line in replayed_lines if line.startswith("data")] == [
        line.replace(", ", ",") for line in live_lines if line.startswith("data")
    ]
    assert [line for line in replayed_lines if line.startswith("event")] == [
        line for line in live_lines if line.startswith("event")
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_unclosed_stream_is_still_filed(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/stream"

    def partial() -> None:
        if kind == "sync":
            client = httpx.Client()
            r = client.send(client.build_request("GET", url), stream=True)
            next(r.iter_lines())
        else:

            async def go() -> None:
                client = httpx.AsyncClient()
                r = await client.send(client.build_request("GET", url), stream=True)
                async for _ in r.aiter_lines():
                    break

            asyncio.run(go())

    cassette, _ = record(INTERCEPT, partial)
    assert len(cassette.exchanges) == 1
    assert cassette.exchanges[0].response.stream is not None


@pytest.mark.parametrize("kind", KINDS)
def test_other_body_encodings(kind: str, plain_server: Any) -> None:
    def run() -> list[httpx.Response]:
        return [
            request(kind, "GET", plain_server.url + "/gzip"),
            request(kind, "GET", plain_server.url + "/text"),
            request(kind, "GET", plain_server.url + "/binary"),
            request(kind, "GET", plain_server.url + "/empty"),
            request(kind, "POST", plain_server.url + "/raw", content=b"not json \xff"),
            request(kind, "POST", plain_server.url + "/form", content=b"plain words"),
            request(kind, "GET", plain_server.url + "/error"),
        ]

    cassette, live = record(INTERCEPT, run)
    encodings = [e.response.body_encoding for e in cassette.exchanges]
    assert encodings == ["json", "text", "base64", "empty", "json", "json", "json"]
    assert cassette.exchanges[0].response.body == {"zipped": True}
    assert "content-encoding" not in cassette.exchanges[0].response.headers
    assert cassette.exchanges[4].request.body_encoding == "base64"
    assert cassette.exchanges[5].request.body_encoding == "text"
    _, replayed = replay(cassette, INTERCEPT, run)

    def content(r: httpx.Response) -> Any:
        return r.json() if "json" in r.headers.get("content-type", "") else r.content

    assert [content(r) for r in replayed] == [content(r) for r in live]
    assert [r.status_code for r in replayed] == [200, 200, 200, 204, 200, 200, 500]


# ----------------------------------------------------------------- matching


@pytest.mark.parametrize("kind", KINDS)
def test_strict_mismatch_message(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"
    body = {
        "system": "You are a careful support agent.",
        "messages": [{"role": "user", "content": "refund"}],
    }
    cassette, _ = record(INTERCEPT, lambda: request(kind, "POST", url, json=body))
    changed = dict(body, system="You are a fast support agent.")
    with pytest.raises(ReplayMismatch) as info:
        replay(cassette, INTERCEPT, lambda: request(kind, "POST", url, json=changed))
    msg = str(info.value)
    assert "request #0 (POST /v1/messages) does not match the recording" in msg
    assert "First difference at $.system" in msg
    assert '"You are a careful support agent."' in msg
    assert '"You are a fast support agent."' in msg
    assert "--replex-mode=rewrite" in msg
    assert "t.replex.json" in msg


@pytest.mark.parametrize("kind", KINDS)
def test_more_requests_than_recorded(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"
    cassette, _ = record(INTERCEPT, lambda: request(kind, "POST", url, json={"a": 1}))

    def twice() -> None:
        request(kind, "POST", url, json={"a": 1})
        request(kind, "POST", url, json={"a": 1})

    with pytest.raises(ReplayMismatch, match="no unused recorded exchanges left"):
        replay(cassette, INTERCEPT, twice)


@pytest.mark.parametrize("kind", KINDS)
def test_sequence_matching(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"

    def run(tag: str) -> list[Any]:
        return [request(kind, "POST", url, json={"prompt": tag, "i": i}).json() for i in range(2)]

    cassette, live = record(INTERCEPT, lambda: run("old prompt"))
    seq = HttpSettings(intercept_hosts=["127.0.0.1"], match="sequence")
    _, replayed = replay(cassette, seq, lambda: run("new prompt"))
    assert replayed == live
    with pytest.raises(ReplayMismatch):
        replay(cassette, seq, lambda: run("x") + run("y"))


@pytest.mark.parametrize("kind", KINDS)
def test_unused_recording(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"
    cassette, _ = record(
        INTERCEPT, lambda: [request(kind, "POST", url, json={"i": i}) for i in range(3)]
    )
    with pytest.raises(UnusedRecording, match="2 recorded exchange"):
        replay(cassette, INTERCEPT, lambda: request(kind, "POST", url, json={"i": 0}))
    relaxed = HttpSettings(intercept_hosts=["127.0.0.1"], strict_unused=False)
    replay(cassette, relaxed, lambda: request(kind, "POST", url, json={"i": 1}))


@pytest.mark.parametrize("kind", KINDS)
def test_out_of_order_strict_matching(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"
    cassette, live = record(
        INTERCEPT, lambda: [request(kind, "POST", url, json={"i": i}).json() for i in range(2)]
    )
    _, replayed = replay(
        cassette,
        INTERCEPT,
        lambda: [request(kind, "POST", url, json={"i": i}).json() for i in (1, 0)],
    )
    assert replayed == [live[1], live[0]]


@pytest.mark.parametrize("kind", KINDS)
def test_redaction_happens_before_matching(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"

    def run() -> Any:
        first = request(
            kind, "POST", url, json={"m": "I am jane@example.com, call +1 415 555 0100"}
        ).json()
        second = request(
            kind, "POST", url, json={"m": "bob@example.com and jane@example.com"}
        ).json()
        return first, second

    cassette, live = record(INTERCEPT, run)
    text = dumps(cassette)
    assert "jane@example.com" not in text and "bob@example.com" not in text
    assert cassette.exchanges[1].request.body == {"m": "[REDACTED_EMAIL_2] and [REDACTED_EMAIL_1]"}
    _, replayed = replay(cassette, INTERCEPT, run)
    # The same live originals match the redacted recording; the replayed responses carry
    # tokens, never the originals.
    assert replayed[0]["echo"]["m"] == "I am [REDACTED_EMAIL_1], call [REDACTED_PHONE_1]"
    assert replayed[1]["echo"]["m"] == "[REDACTED_EMAIL_2] and [REDACTED_EMAIL_1]"
    assert live[1]["echo"]["m"] == "bob@example.com and jane@example.com"


@pytest.mark.parametrize("kind", KINDS)
def test_user_normalisers(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"

    def strip_seed(body: Any) -> Any:
        body.pop("seed", None)
        return body

    settings = HttpSettings(
        intercept_hosts=["127.0.0.1"], normalisers=[strip_seed], ignore_request_fields=["meta.*.ts"]
    )
    cassette, _ = record(
        settings, lambda: request(kind, "POST", url, json={"seed": 1, "meta": [{"ts": 1}]})
    )
    replay(
        cassette,
        settings,
        lambda: request(kind, "POST", url, json={"seed": 2, "meta": [{"ts": 9}]}),
    )


# ----------------------------------------------------------------- hosts


@pytest.mark.parametrize("kind", KINDS)
def test_network_blocked_for_unknown_hosts(kind: str) -> None:
    cassette = Cassette(name="t", created_at="x", replex_version="0")
    with pytest.raises(NetworkBlocked, match=r"blocked GET https://example\.invalid/x"):
        replay(cassette, INTERCEPT, lambda: request(kind, "GET", "https://example.invalid/x"))


@pytest.mark.parametrize("kind", KINDS)
def test_passthrough_and_unintercepted_hosts(kind: str, plain_server: Any) -> None:
    local = f"http://localhost:{plain_server.port}/p"
    cassette, _ = record(INTERCEPT, lambda: request(kind, "GET", local))
    assert cassette.events == []  # localhost is not intercepted: not recorded
    replay(cassette, INTERCEPT, lambda: request(kind, "GET", local))
    assert plain_server.hits == 2  # passthrough reached the server in replay


def test_host_patterns_with_port_and_glob(plain_server: Any) -> None:
    settings = HttpSettings(intercept_hosts=[f"127.0.0.1:{plain_server.port}", "*.anthropic.com"])
    ctx = CaptureContext(mode="record", http=settings)
    assert ctx.intercepts(httpx.URL(plain_server.url))
    assert ctx.intercepts(httpx.URL("https://api.anthropic.com/v1/messages"))
    assert not ctx.intercepts(httpx.URL("http://127.0.0.1:1/"))


# ----------------------------------------------------------------- explicit clients, patching


@pytest.mark.parametrize("kind", KINDS)
def test_explicit_clients_without_patch(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/e"
    ctx = CaptureContext(mode="record", http=INTERCEPT)
    with capturing(ctx, patch=False):
        if kind == "sync":
            with http_client() as c:
                c.post(url, json={"a": 1})
            with httpx.Client() as plain:  # not patched: not captured
                plain.post(url, json={"a": 2})
        else:

            async def go() -> None:
                async with async_http_client() as c:
                    await c.post(url, json={"a": 1})
                async with httpx.AsyncClient() as plain:
                    await plain.post(url, json={"a": 2})

            asyncio.run(go())
    assert len(ctx.finished_events()) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_explicit_client_and_patch_do_not_double_capture(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/e"
    if kind == "sync":
        cassette, _ = record(INTERCEPT, lambda: http_client().post(url, json={"a": 1}))
    else:

        async def go() -> None:
            async with async_http_client() as c:
                await c.post(url, json={"a": 1})

        cassette, _ = record(INTERCEPT, lambda: asyncio.run(go()))
    assert len(cassette.exchanges) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_explicit_transport_outside_session(kind: str, plain_server: Any) -> None:
    url = plain_server.url + "/o"
    if kind == "sync":
        with httpx.Client(transport=ReplexTransport()) as c:
            assert c.get(url).json()["hit"] == 1
    else:

        async def go() -> int:
            async with httpx.AsyncClient(transport=AsyncReplexTransport()) as c:
                return int((await c.get(url)).json()["hit"])

        assert asyncio.run(go()) == 1


def test_patch_removed_after_exception() -> None:
    original = httpx.HTTPTransport.handle_request
    ctx = CaptureContext(mode="record", http=INTERCEPT)
    with pytest.raises(RuntimeError), capturing(ctx):
        assert patching.is_installed()
        assert httpx.HTTPTransport.handle_request is not original
        raise RuntimeError("agent crashed")
    assert not patching.is_installed()
    assert httpx.HTTPTransport.handle_request is original
    assert _runtime.active_capture() is None


def test_sessions_do_not_nest() -> None:
    with capturing(CaptureContext(mode="record")):
        with pytest.raises(SessionActive), capturing(CaptureContext(mode="record")):
            pass
        assert patching.is_installed()


@pytest.mark.parametrize("kind", KINDS)
def test_failed_requests_leave_no_event(kind: str) -> None:
    settings = HttpSettings(intercept_hosts=["127.0.0.1"])

    def boom() -> None:
        with pytest.raises(httpx.ConnectError):
            request(kind, "GET", "http://127.0.0.1:9/unreachable")

    cassette, _ = record(settings, boom)
    assert cassette.events == []


# ----------------------------------------------------------------- unit helpers


def test_sse_round_trip() -> None:
    raw = 'event: a\nid: 7\ndata: {"x":1}\n\ndata: line one\ndata: line two\n\n: comment\n\nretry: 5\n\n'
    events = parse_sse(raw)
    assert events == [
        StreamEvent(event="a", data={"x": 1}, id="7"),
        StreamEvent(data="line one\nline two"),
    ]
    assert render_event(events[0]) == b'event: a\nid: 7\ndata: {"x":1}\n\n'
    assert render_event(events[1]) == b"data: line one\ndata: line two\n\n"
    assert parse_sse('data: {"broken"\n\n') == [StreamEvent(data='{"broken"')]
    assert parse_sse('data: "quoted"\n\n')[0].data == '"quoted"'


def test_drop_fields_and_keys() -> None:
    body = {"a": {"b": 1, "c": 2}, "l": [{"x": 1}, {"x": 2, "y": 3}], "k": 1}
    assert drop_fields(body, ["a.b", "l.*.x", "missing.path", "l.9"]) == {
        "a": {"c": 2},
        "l": [{}, {"y": 3}],
        "k": 1,
    }
    assert drop_fields(body, ["l.0"]) == {"a": {"b": 1, "c": 2}, "l": [{"x": 2, "y": 3}], "k": 1}
    assert drop_fields("text", ["a"]) == "text"
    assert match_key("post", "http://h/p?q=1", {"b": 1, "a": 2}) == match_key(
        "POST", "http://other/p", {"a": 2, "b": 1}
    )


def test_first_difference_and_diff() -> None:
    assert first_difference({"a": [1, 2]}, {"a": [1, 2]}) is None
    assert first_difference({"a": [1, 2]}, {"a": [1, 3]}) == ("$.a[1]", 2, 3)
    assert first_difference({"a": [1]}, {"a": [1, 2]})[0] == "$.a[1]"  # type: ignore[index]
    assert first_difference({"a": 1}, {"b": 1})[0] == "$.a"  # type: ignore[index]
    assert first_difference({"b": 1}, {"a": 1, "b": 1})[0] == "$.a"  # type: ignore[index]
    assert first_difference(1, "1") == ("$", 1, "1")
    long_a = {"k": [str(i) for i in range(100)]}
    long_b = {"k": [str(i) + "x" for i in range(100)]}
    text = body_diff(long_a, long_b, max_lines=10)
    assert "more diff lines" in text
    assert json.loads(json.dumps(text))
