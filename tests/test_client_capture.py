"""Capture of ``requests`` and ``aiohttp``, mirroring the httpx capture tests.

Every capture test runs for both clients (``client`` parameter): ``requests`` is synchronous,
``aiohttp`` asynchronous.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from replex import _runtime
from replex.capture import CaptureContext, HttpSettings, capturing, patching
from replex.cassette import Cassette, dumps
from replex.errors import NetworkBlocked, ReplayMismatch, UnusedRecording
from replex.testing import MockProvider, MockReply

requests = pytest.importorskip("requests")
aiohttp = pytest.importorskip("aiohttp")

from replex.capture import aiohttp_client, requests_client  # noqa: E402

CLIENTS = ["requests", "aiohttp"]
INTERCEPT = HttpSettings(intercept_hosts=["127.0.0.1"], passthrough_hosts=["localhost"])
OFFLINE = "http://127.0.0.1:9"  # nothing listens here


# ----------------------------------------------------------------- helpers


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    content: bytes
    chunks: list[bytes] = field(default_factory=list)

    def json(self) -> Any:
        return json.loads(self.content)


def lower(headers: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def request(client: str, method: str, url: str, **kw: Any) -> Reply:
    """One request through a plain ``requests`` or ``aiohttp`` session."""
    if client == "requests":
        r = requests.request(method, url, data=kw.pop("content", None), **kw)
        return Reply(r.status_code, lower(r.headers), r.content)

    async def go() -> Reply:
        async with aiohttp.ClientSession() as s:
            async with s.request(method, url, data=kw.pop("content", None), **kw) as r:
                return Reply(r.status, lower(r.headers), await r.read())

    return asyncio.run(go())


def stream(client: str, url: str, **kw: Any) -> Reply:
    """A streamed request, keeping the chunks as the client delivered them."""
    if client == "requests":
        with requests.request(kw.pop("method", "GET"), url, stream=True, **kw) as r:
            chunks = list(r.iter_content(chunk_size=None))
            return Reply(r.status_code, lower(r.headers), b"".join(chunks), chunks)

    async def go() -> Reply:
        async with aiohttp.ClientSession() as s:
            async with s.request(kw.pop("method", "GET"), url, **kw) as r:
                chunks = [c async for c in r.content.iter_any()]
                return Reply(r.status, lower(r.headers), b"".join(chunks), chunks)

    return asyncio.run(go())


def lines(reply: Reply, prefix: str) -> list[str]:
    return [line for line in reply.content.decode().splitlines() if line.startswith(prefix)]


def to_cassette(ctx: CaptureContext) -> Cassette:
    return Cassette(
        name="t",
        created_at="2026-10-07T00:00:00Z",
        replex_version="0.1.0",
        events=ctx.finished_events(),
    )


def record(settings: HttpSettings, fn: Callable[[], Any]) -> tuple[Cassette, Any]:
    ctx = CaptureContext(mode="record", http=settings)
    with capturing(ctx):
        out = fn()
    return to_cassette(ctx), out


def replay(cassette: Cassette, settings: HttpSettings, fn: Callable[[], Any]) -> Any:
    ctx = CaptureContext(
        mode="replay", http=settings, baseline=cassette, cassette_label="t.replex.json"
    )
    with capturing(ctx):
        out = fn()
        ctx.check_unused()
    return out


# ----------------------------------------------------------------- record / replay


@pytest.mark.parametrize("client", CLIENTS)
def test_record_then_replay_json(client: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/echo"
    body = {"messages": [{"role": "user", "content": "hi"}], "request_id": "volatile-1"}
    cassette, live = record(
        INTERCEPT,
        lambda: request(
            client, "POST", url, json=body, headers={"x-api-key": "sk-ant-secretsecretsecret"}
        ),
    )
    assert live.json()["hit"] == 1
    [event] = cassette.exchanges
    assert event.request.headers["x-api-key"] == "[REDACTED]"
    assert event.request.body == body
    assert event.response.headers["x-request-token"] == "[REDACTED]"
    assert "sk-ant-secret" not in dumps(cassette)

    body2 = dict(body, request_id="volatile-2")  # ignored field differs: still matches
    replayed = replay(cassette, INTERCEPT, lambda: request(client, "POST", url, json=body2))
    assert replayed.json() == live.json()
    assert replayed.status == 200
    assert plain_server.hits == 1  # served from the cassette


@pytest.mark.parametrize("client", CLIENTS)
def test_replay_makes_no_network_calls(
    client: str, plain_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = plain_server.url + "/x"
    cassette, live = record(INTERCEPT, lambda: request(client, "POST", url, json={"q": 1}))

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("network access attempted during replay")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    replayed = replay(cassette, INTERCEPT, lambda: request(client, "POST", url, json={"q": 1}))
    assert replayed.json() == live.json()


@pytest.mark.parametrize("client", CLIENTS)
def test_streaming_is_recorded_and_replayed_event_by_event(client: str, plain_server: Any) -> None:
    url = plain_server.url + "/stream"
    cassette, live = record(INTERCEPT, lambda: stream(client, url))
    [event] = cassette.exchanges
    events = event.response.stream
    assert events is not None
    assert [e.event for e in events] == ["start", "delta", "delta", None]
    assert events[1].data == {"type": "delta", "text": "Hel"}
    assert events[-1].data == "[DONE]"

    replayed = replay(cassette, INTERCEPT, lambda: stream(client, url))
    assert len(replayed.chunks) == len(events)  # one chunk per event, in order
    assert replayed.chunks[1] == b'event: delta\ndata: {"type":"delta","text":"Hel"}\n\n'
    assert lines(replayed, "event") == lines(live, "event")
    assert lines(replayed, "data") == lines(live, "data")
    assert replayed.headers["content-type"] == "text/event-stream"


@pytest.mark.parametrize("client", CLIENTS)
def test_unclosed_stream_is_still_filed(client: str, plain_server: Any) -> None:
    url = plain_server.url + "/stream"

    def partial() -> None:
        if client == "requests":
            r = requests.get(url, stream=True)
            next(r.iter_content(chunk_size=8))
            return

        async def go() -> None:
            async with aiohttp.ClientSession() as s:
                r = await s.get(url)
                await r.content.read(8)
                r.release()

        asyncio.run(go())

    cassette, _ = record(INTERCEPT, partial)
    assert len(cassette.exchanges) == 1
    assert cassette.exchanges[0].response.stream is not None


@pytest.mark.parametrize("client", CLIENTS)
def test_other_body_encodings(client: str, plain_server: Any) -> None:
    def run() -> list[Reply]:
        return [
            request(client, "GET", plain_server.url + "/gzip"),
            request(client, "GET", plain_server.url + "/text"),
            request(client, "GET", plain_server.url + "/binary"),
            request(client, "GET", plain_server.url + "/empty"),
            request(client, "POST", plain_server.url + "/raw", content=b"not json \xff"),
            request(client, "GET", plain_server.url + "/error"),
        ]

    cassette, live = record(INTERCEPT, run)
    encodings = [e.response.body_encoding for e in cassette.exchanges]
    assert encodings == ["json", "text", "base64", "empty", "json", "json"]
    assert cassette.exchanges[0].response.body == {"zipped": True}
    assert cassette.exchanges[4].request.body_encoding == "base64"
    replayed = replay(cassette, INTERCEPT, run)

    def content(r: Reply) -> Any:  # JSON is stored parsed, so compare it parsed
        return r.json() if "json" in r.headers.get("content-type", "") else r.content

    assert [content(r) for r in replayed] == [content(r) for r in live]
    assert [r.status for r in replayed] == [200, 200, 200, 204, 200, 500]


def test_requests_bodies_of_every_kind(plain_server: Any) -> None:
    url = plain_server.url + "/b"

    def run() -> list[Any]:
        return [
            requests.post(url, data={"form": "value"}).json(),
            requests.post(url, data=iter([b'{"gen', b'erated": 1}'])).json(),
            requests.post(url, data='{"text": "é"}'.encode()).json(),
        ]

    cassette, live = record(INTERCEPT, run)
    bodies = [e.request.body for e in cassette.exchanges]
    assert bodies == ["form=value", {"generated": 1}, {"text": "é"}]
    assert replay(cassette, INTERCEPT, run) == live


def test_aiohttp_streamed_body_is_captured(plain_server: Any) -> None:
    url = plain_server.url + "/b"

    async def chunks() -> Any:
        yield b'{"streamed": '
        yield b"true}"

    async def go() -> Any:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=chunks(), headers={"content-length": "18"}) as r:
                return await r.json()

    cassette, live = record(INTERCEPT, lambda: asyncio.run(go()))
    assert cassette.exchanges[0].request.body == {"streamed": True}
    assert live["echo"] == {"streamed": True}


# ----------------------------------------------------------------- matching


@pytest.mark.parametrize("client", CLIENTS)
def test_strict_mismatch_message(client: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"
    body = {"system": "You are a careful support agent.", "messages": []}
    cassette, _ = record(INTERCEPT, lambda: request(client, "POST", url, json=body))
    changed = dict(body, system="You are a fast support agent.")
    with pytest.raises(ReplayMismatch) as info:
        replay(cassette, INTERCEPT, lambda: request(client, "POST", url, json=changed))
    msg = str(info.value)
    assert "request #0 (POST /v1/messages) does not match the recording" in msg
    assert "First difference at $.system" in msg


@pytest.mark.parametrize("client", CLIENTS)
def test_sequence_matching(client: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"

    def run(tag: str) -> list[Any]:
        return [request(client, "POST", url, json={"p": tag, "i": i}).json() for i in range(2)]

    cassette, live = record(INTERCEPT, lambda: run("old prompt"))
    seq = HttpSettings(intercept_hosts=["127.0.0.1"], match="sequence")
    assert replay(cassette, seq, lambda: run("new prompt")) == live


@pytest.mark.parametrize("client", CLIENTS)
def test_unused_recording(client: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"
    cassette, _ = record(
        INTERCEPT, lambda: [request(client, "POST", url, json={"i": i}) for i in range(3)]
    )
    with pytest.raises(UnusedRecording, match="2 recorded exchange"):
        replay(cassette, INTERCEPT, lambda: request(client, "POST", url, json={"i": 0}))


@pytest.mark.parametrize("client", CLIENTS)
def test_redaction_happens_before_matching(client: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"

    def run() -> Any:
        return request(client, "POST", url, json={"m": "I am jane@example.com"}).json()

    cassette, live = record(INTERCEPT, run)
    assert "jane@example.com" not in dumps(cassette)
    replayed = replay(cassette, INTERCEPT, run)
    assert replayed["echo"]["m"] == "I am [REDACTED_EMAIL_1]"
    assert live["echo"]["m"] == "I am jane@example.com"


# ----------------------------------------------------------------- hosts


@pytest.mark.parametrize("client", CLIENTS)
def test_network_blocked_for_unknown_hosts(client: str) -> None:
    cassette = Cassette(name="t", created_at="x", replex_version="0")
    with pytest.raises(NetworkBlocked, match=r"blocked GET https://example\.invalid/x"):
        replay(cassette, INTERCEPT, lambda: request(client, "GET", "https://example.invalid/x"))


@pytest.mark.parametrize("client", CLIENTS)
def test_passthrough_and_unintercepted_hosts(client: str, plain_server: Any) -> None:
    local = f"http://localhost:{plain_server.port}/p"
    cassette, _ = record(INTERCEPT, lambda: request(client, "GET", local))
    assert cassette.events == []  # localhost is not intercepted: not recorded
    replay(cassette, INTERCEPT, lambda: request(client, "GET", local))
    assert plain_server.hits == 2  # passthrough reached the server in replay


@pytest.mark.parametrize("client", CLIENTS)
def test_failed_requests_leave_no_event(client: str) -> None:
    errors = (requests.ConnectionError, aiohttp.ClientConnectionError)

    def boom() -> None:
        with pytest.raises(errors):
            request(client, "GET", "http://127.0.0.1:9/unreachable")

    cassette, _ = record(INTERCEPT, boom)
    assert cassette.events == []


@pytest.mark.parametrize("client", CLIENTS)
def test_requests_outside_a_session_are_untouched(client: str, plain_server: Any) -> None:
    patching.install()
    try:
        assert request(client, "GET", plain_server.url + "/o").json()["hit"] == 1
    finally:
        patching.uninstall()


# ----------------------------------------------------------------- the mock provider


def anthropic_request(stream: bool) -> dict[str, Any]:
    return {
        "model": "mock-model",
        "max_tokens": 64,
        "stream": stream,
        "messages": [{"role": "user", "content": "Where is order 1042?"}],
    }


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("streaming", [False, True])
def test_mock_provider_records_and_replays_offline(client: str, streaming: bool) -> None:
    key = {"x-api-key": "sk-ant-api03-test-key-that-must-never-be-written"}

    def call(base: str) -> Reply:
        url = base + "/v1/messages"
        body = anthropic_request(streaming)
        if streaming:
            return stream(client, url, method="POST", json=body, headers=key)
        return request(client, "POST", url, json=body, headers=key)

    with MockProvider([MockReply(text="Order 1042 has shipped.")]) as mock:
        ctx = CaptureContext(mode="record", http=INTERCEPT)
        with capturing(ctx):
            live = call(mock.url)
    cassette = to_cassette(ctx)
    assert "sk-ant-api03" not in dumps(cassette)
    assert (cassette.exchanges[0].response.stream is not None) is streaming

    replayed = replay(cassette, INTERCEPT, lambda: call(OFFLINE))
    if streaming:
        assert lines(replayed, "event") == lines(live, "event")
        assert lines(replayed, "data") == lines(live, "data")
        assert len(replayed.chunks) == len(cassette.exchanges[0].response.stream or [])
    else:
        assert replayed.json() == live.json()
        assert replayed.json()["content"][0]["text"] == "Order 1042 has shipped."


# ----------------------------------------------------------------- across clients


def httpx_call(url: str, body: Any) -> Any:
    with httpx.Client() as c:
        return c.post(url, json=body).json()


def client_call(client: str) -> Callable[[str, Any], Any]:
    return lambda url, body: request(client, "POST", url, json=body).json()


CALLERS: dict[str, Callable[[str, Any], Any]] = {
    "httpx": httpx_call,
    "requests": client_call("requests"),
    "aiohttp": client_call("aiohttp"),
}


@pytest.mark.parametrize(
    ("recorder", "replayer"),
    [
        ("httpx", "requests"),
        ("httpx", "aiohttp"),
        ("requests", "httpx"),
        ("aiohttp", "httpx"),
        ("requests", "aiohttp"),
    ],
)
def test_cassettes_replay_across_clients(recorder: str, replayer: str, plain_server: Any) -> None:
    url = plain_server.url + "/v1/messages"
    body = {"messages": [{"role": "user", "content": "hi"}]}
    cassette, live = record(INTERCEPT, lambda: CALLERS[recorder](url, body))
    replayed = replay(cassette, INTERCEPT, lambda: CALLERS[replayer](url, body))
    assert replayed == live
    assert plain_server.hits == 1


# ----------------------------------------------------------------- patching


def originals() -> tuple[Any, Any]:
    return requests.adapters.HTTPAdapter.send, aiohttp.ClientSession._request


def test_patches_removed_after_exception() -> None:
    before = originals()
    with pytest.raises(RuntimeError), capturing(CaptureContext(mode="record", http=INTERCEPT)):
        assert all(now is not then for now, then in zip(originals(), before, strict=True))
        raise RuntimeError("agent crashed")
    assert originals() == before
    assert _runtime.active_capture() is None


def test_patches_wrap_and_restore_third_party_patches(
    monkeypatch: pytest.MonkeyPatch, plain_server: Any
) -> None:
    """Mocking and instrumentation libraries patch these methods too: theirs run under ours."""
    calls: list[str] = []
    send, send_async = originals()

    def instrumented(self: Any, req: Any, *a: Any, **kw: Any) -> Any:
        calls.append("requests")
        return send(self, req, *a, **kw)

    async def instrumented_async(self: Any, *a: Any, **kw: Any) -> Any:
        calls.append("aiohttp")
        return await send_async(self, *a, **kw)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", instrumented)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", instrumented_async)
    cassette, _ = record(
        INTERCEPT,
        lambda: [request(c, "POST", plain_server.url + "/i", json={}) for c in CLIENTS],
    )
    assert calls == ["requests", "aiohttp"]
    assert len(cassette.exchanges) == 2
    assert originals() == (instrumented, instrumented_async)


def test_aiohttp_session_middlewares_still_run(plain_server: Any) -> None:
    seen: list[str] = []

    async def tag(req: Any, handler: Any) -> Any:
        req.headers["x-tag"] = "outer"
        seen.append(str(req.url))
        return await handler(req)

    async def go() -> Any:
        async with aiohttp.ClientSession(middlewares=(tag,)) as s:
            async with s.post(plain_server.url + "/m", json={}) as r:
                return await r.json()

    cassette, _ = record(INTERCEPT, lambda: asyncio.run(go()))
    assert seen == [plain_server.url + "/m"]
    assert cassette.exchanges[0].request.headers["x-tag"] == "outer"


def test_aiohttp_versions_without_middlewares_are_not_patched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert aiohttp_client.supported("3.12.0")
    assert aiohttp_client.supported("4.0.0a1")
    assert not aiohttp_client.supported("3.11.18")
    monkeypatch.setattr(aiohttp, "__version__", "3.11.18")
    assert aiohttp_client.make_patch() is None
    assert isinstance(requests_client.make_patch(), requests_client.RequestsPatch)


def test_clients_that_are_not_installed_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patching, "CLIENT_MODULES", {"replex_no_such_client": "nowhere"})
    patching.client_patches.cache_clear()
    try:
        assert patching.client_patches() == ()
    finally:
        patching.client_patches.cache_clear()


# ----------------------------------------------------------------- response details


def test_replayed_cookies_reach_the_session(plain_server: Any) -> None:
    url = plain_server.url + "/c"
    cassette, _ = record(INTERCEPT, lambda: request("requests", "GET", url))
    cassette.exchanges[0].response.headers["set-cookie"] = "session=abc; Path=/"

    def with_requests() -> Any:
        with requests.Session() as s:
            s.get(url)
            return s.cookies.get("session")

    async def with_aiohttp() -> Any:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            async with s.get(url) as r:
                await r.read()
                assert r.cookies["session"].value == "abc"
            return {c.key: c.value for c in s.cookie_jar}

    assert replay(cassette, INTERCEPT, with_requests) == "abc"
    assert replay(cassette, INTERCEPT, lambda: asyncio.run(with_aiohttp())) == {"session": "abc"}


def test_requests_raw_body_and_early_close(plain_server: Any) -> None:
    url = plain_server.url + "/text"
    cassette, _ = record(INTERCEPT, lambda: [request("requests", "GET", url) for _ in range(2)])

    def run() -> bytes:
        unread = requests.get(url, stream=True)
        unread.close()
        return bytes(requests.get(url, stream=True).raw.read())

    assert replay(cassette, INTERCEPT, run) == b"plain text reply"
