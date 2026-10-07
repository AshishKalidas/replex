"""httpx transports that record and replay exchanges (sync and async, streaming aware)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import httpx

from replex import _runtime
from replex.capture.context import CaptureContext, _Pending, encode_body
from replex.capture.patching import bypass_patch
from replex.capture.sse import parse_sse, render_event
from replex.cassette.model import ExchangeEvent, RecordedRequest

SyncSend = Callable[[httpx.Request], httpx.Response]
AsyncSend = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _is_event_stream(headers: httpx.Headers) -> bool:
    return "text/event-stream" in headers.get("content-type", "").lower()


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


# --------------------------------------------------------------------- replay


class _ReplayStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """Yields recorded server-sent events one at a time, in their original order."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _replay_response(ctx: CaptureContext, request: httpx.Request) -> httpx.Response:
    request.read()
    recorded = ctx.replay_exchange(request)
    ctx.append(lambda i: recorded.model_copy(update={"index": i}))
    rsp = recorded.response
    headers = dict(rsp.headers)
    if rsp.stream is not None:
        chunks = [render_event(e) for e in rsp.stream]
        return httpx.Response(
            rsp.status, headers=headers, stream=_ReplayStream(chunks), request=request
        )
    return httpx.Response(
        rsp.status,
        headers=headers,
        content=encode_body(rsp.body, rsp.body_encoding),
        request=request,
    )


# --------------------------------------------------------------------- record


class _Recording:
    """Collects one exchange while it is in flight and files it when complete."""

    def __init__(self, ctx: CaptureContext, request: httpx.Request) -> None:
        self.ctx = ctx
        self.start = time.perf_counter()
        self.pending: _Pending = ctx.reserve()
        self.request: RecordedRequest = ctx.recorded_request(request)
        self.chunks: list[bytes] = []
        self.done = False

    def finish(self, response: httpx.Response, content: bytes | None) -> None:
        if self.done:
            return
        self.done = True
        stream = None
        if content is None:
            stream = parse_sse(b"".join(self.chunks).decode("utf-8", errors="replace"))
        event = ExchangeEvent(
            index=self.pending.index,
            request=self.request,
            response=self.ctx.recorded_response(
                response.status_code, response.headers, content, stream
            ),
            duration_ms=_elapsed_ms(self.start),
        )
        self.ctx.fill(self.pending, event)

    def fail(self) -> None:
        self.done = True
        self.ctx.drop(self.pending)


class _TeeSyncStream(httpx.SyncByteStream):
    def __init__(
        self, inner: httpx.SyncByteStream, rec: _Recording, response: httpx.Response
    ) -> None:
        self._inner, self._rec, self._response = inner, rec, response
        rec.ctx.open_streams.append(self._finish)

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            self._rec.chunks.append(chunk)
            yield chunk

    def _finish(self) -> None:
        self._rec.finish(self._response, None)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self._finish()


class _TeeAsyncStream(httpx.AsyncByteStream):
    def __init__(
        self, inner: httpx.AsyncByteStream, rec: _Recording, response: httpx.Response
    ) -> None:
        self._inner, self._rec, self._response = inner, rec, response
        rec.ctx.open_streams.append(self._finish)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._rec.chunks.append(chunk)
            yield chunk

    def _finish(self) -> None:
        self._rec.finish(self._response, None)

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._finish()


def _prepare_for_recording(request: httpx.Request) -> None:
    request.read()
    # Ask for an uncompressed body so streamed bytes can be stored as readable events.
    request.headers["accept-encoding"] = "identity"


def _record_sync(ctx: CaptureContext, request: httpx.Request, send: SyncSend) -> httpx.Response:
    _prepare_for_recording(request)
    rec = _Recording(ctx, request)
    try:
        response = send(request)
    except BaseException:
        rec.fail()
        raise
    if _is_event_stream(response.headers) and not response.headers.get("content-encoding"):
        stream = response.stream
        assert isinstance(stream, httpx.SyncByteStream)
        response.stream = _TeeSyncStream(stream, rec, response)
    else:
        try:
            response.read()
        except BaseException:
            rec.fail()
            raise
        rec.finish(response, response.content)
    return response


async def _record_async(
    ctx: CaptureContext, request: httpx.Request, send: AsyncSend
) -> httpx.Response:
    _prepare_for_recording(request)
    rec = _Recording(ctx, request)
    try:
        response = await send(request)
    except BaseException:
        rec.fail()
        raise
    if _is_event_stream(response.headers) and not response.headers.get("content-encoding"):
        stream = response.stream
        assert isinstance(stream, httpx.AsyncByteStream)
        response.stream = _TeeAsyncStream(stream, rec, response)
    else:
        try:
            await response.aread()
        except BaseException:
            rec.fail()
            raise
        rec.finish(response, response.content)
    return response


# --------------------------------------------------------------------- dispatch


def handle_sync(ctx: CaptureContext, request: httpx.Request, send: SyncSend) -> httpx.Response:
    """Route one synchronous request according to the capture mode and host lists."""
    if ctx.mode == "replay":
        if ctx.intercepts(request.url):
            return _replay_response(ctx, request)
        if ctx.passes_through(request.url):
            return send(request)
        raise ctx.blocked(request)
    if ctx.intercepts(request.url):
        return _record_sync(ctx, request, send)
    return send(request)


async def handle_async(
    ctx: CaptureContext, request: httpx.Request, send: AsyncSend
) -> httpx.Response:
    """Route one asynchronous request according to the capture mode and host lists."""
    if ctx.mode == "replay":
        if ctx.intercepts(request.url):
            return _replay_response(ctx, request)
        if ctx.passes_through(request.url):
            return await send(request)
        raise ctx.blocked(request)
    if ctx.intercepts(request.url):
        return await _record_async(ctx, request, send)
    return await send(request)


# --------------------------------------------------------------------- explicit clients


class ReplexTransport(httpx.BaseTransport):
    """A synchronous transport that records or replays while a session is active.

    Outside a session it forwards every request to the wrapped transport unchanged.
    """

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._inner = transport if transport is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        ctx = _runtime.active_capture()
        with bypass_patch():
            if ctx is None:
                return self._inner.handle_request(request)
            return handle_sync(ctx, request, self._inner.handle_request)

    def close(self) -> None:
        self._inner.close()


class AsyncReplexTransport(httpx.AsyncBaseTransport):
    """An asynchronous transport that records or replays while a session is active."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = transport if transport is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ctx = _runtime.active_capture()
        with bypass_patch():
            if ctx is None:
                return await self._inner.handle_async_request(request)
            return await handle_async(ctx, request, self._inner.handle_async_request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def http_client(**kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` whose requests RepleX records and replays.

    Pass it to an SDK with ``http_client=``. Keyword arguments go to ``httpx.Client``; a
    ``transport`` argument is wrapped rather than replaced.
    """
    inner = kwargs.pop("transport", None)
    return httpx.Client(transport=ReplexTransport(inner), **kwargs)


def async_http_client(**kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose requests RepleX records and replays."""
    inner = kwargs.pop("transport", None)
    return httpx.AsyncClient(transport=AsyncReplexTransport(inner), **kwargs)
