"""Capture for ``aiohttp``: a client middleware is added to every request while a session is
active.

``ClientSession._request`` is wrapped only to append RepleX's middleware to the request's
middleware chain, innermost, so it sees each fully built request (headers, cookies, auth and
body applied) and each redirect hop, the way the httpx transport does. Client middlewares
arrived in aiohttp 3.12; older releases are not captured.

A captured request is handled as an ``httpx.Request`` (see :mod:`replex.capture.bridge`).
While recording, the caller gets the real ``ClientResponse`` with its body read through the
recorder; during replay it gets a ``ClientResponse`` built from the recording. Either way the
body is pulled chunk by chunk as the caller reads it.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp
import httpx
from aiohttp import ClientRequest, ClientResponse, ClientSession
from aiohttp.base_protocol import BaseProtocol
from aiohttp.helpers import TimerNoop
from aiohttp.http import HttpVersion11
from aiohttp.payload import BytesPayload
from aiohttp.streams import StreamReader
from multidict import CIMultiDict, CIMultiDictProxy

from replex import _runtime
from replex.capture.bridge import copy_changed_headers, decoded_headers
from replex.capture.context import CaptureContext
from replex.capture.transport import handle_async

Handler = Callable[[ClientRequest], Awaitable[ClientResponse]]
RequestMethod = Callable[..., Awaitable[ClientResponse]]

_LIMIT = 2**16


class _NoFlowControl(BaseProtocol):
    """The protocol of a reader with no connection: there is nothing to pause or resume."""

    def pause_reading(self) -> None:
        pass

    def resume_reading(self, resume_parser: bool = True) -> None:
        pass


class _PullReader(StreamReader):
    """A ``StreamReader`` that takes its next chunk from an iterator when a read needs data."""

    def __init__(self, chunks: AsyncIterator[bytes], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(_NoFlowControl(loop), _LIMIT, loop=loop)
        self._chunks = chunks

    async def _wait(self, func_name: str) -> None:
        try:
            chunk = await anext(self._chunks)
        except StopAsyncIteration:
            self.feed_eof()
        else:
            self.feed_data(chunk)


class _LiveStream(httpx.AsyncByteStream):
    """The body of a live ``aiohttp`` response, as httpx reads it.

    The response releases its own connection when the body ends, as it does without RepleX.
    """

    def __init__(self, content: StreamReader) -> None:
        self._content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._content.iter_any():
            yield chunk


class _AlreadySent:
    """Stands in for the request writer: a replayed request is never written anywhere."""

    output_size = 0


class _ReplayedResponse(ClientResponse):
    """A ``ClientResponse`` built from a recorded exchange instead of a connection."""

    def __init__(
        self, req: ClientRequest, session: ClientSession, response: httpx.Response
    ) -> None:
        kwargs: dict[str, Any] = {
            "writer": None,
            "continue100": None,
            "timer": TimerNoop(),
            "request_info": req.request_info,
            "traces": [],
            "loop": req.loop,
            "session": session,
        }
        if "stream_writer" in inspect.signature(ClientResponse.__init__).parameters:
            kwargs["stream_writer"] = _AlreadySent()
        super().__init__(req.method, req.original_url, **kwargs)
        headers = decoded_headers(response.headers)
        self.version = HttpVersion11
        self.status = response.status_code
        self.reason = response.reason_phrase
        self._headers = CIMultiDictProxy(CIMultiDict(headers))
        self._raw_headers = tuple((k.encode(), v.encode()) for k, v in headers)
        self.content = _PullReader(response.aiter_bytes(), req.loop)
        if cookies := self._headers.getall("set-cookie", ()):
            self._raw_cookie_headers = tuple(cookies)


async def _request_body(req: ClientRequest) -> bytes:
    """The request body as bytes. A streamed body is read once and replaced by them."""
    body = req.body
    if not body:
        return b""
    content = await body.as_bytes()
    if not isinstance(body, BytesPayload):
        await req.update_body(content)
    return content


def _middleware(ctx: CaptureContext, session: ClientSession) -> aiohttp.ClientMiddlewareType:
    async def capture(req: ClientRequest, handler: Handler) -> ClientResponse:
        if ctx.sends_unchanged(httpx.URL(str(req.url))):
            return await handler(req)
        live: list[ClientResponse] = []

        async def send(r: httpx.Request) -> httpx.Response:
            copy_changed_headers(req.headers, r)
            resp = await handler(req)
            live.append(resp)
            return httpx.Response(
                resp.status,
                headers=decoded_headers(httpx.Headers(list(resp.headers.items()))),
                stream=_LiveStream(resp.content),
                request=r,
            )

        request = httpx.Request(
            req.method,
            str(req.url),
            headers=list(req.headers.items()),
            content=await _request_body(req) or None,
        )
        response = await handle_async(ctx, request, send)
        if not live:
            return _ReplayedResponse(req, session, response)
        resp = live[0]
        resp.content = _PullReader(response.aiter_bytes(), req.loop)
        return resp

    return capture


class AiohttpPatch:
    """The patch for ``aiohttp``.

    The wrapped method is the one in place at install time, so a patch another library applied
    first (``aioresponses``, instrumentation) keeps working underneath ours.
    """

    def __init__(self) -> None:
        self.wrapped: RequestMethod = ClientSession._request
        patch = self

        async def patched(
            session: ClientSession, method: str, str_or_url: Any, **kwargs: Any
        ) -> ClientResponse:
            inner = patch.wrapped
            ctx = _runtime.active_capture()
            if ctx is not None:
                chain = kwargs.get("middlewares")
                if chain is None:
                    chain = session._middlewares
                kwargs["middlewares"] = (*chain, _middleware(ctx, session))
            return await inner(session, method, str_or_url, **kwargs)

        self.patched: RequestMethod = patched

    def install(self) -> None:
        self.wrapped = ClientSession._request
        ClientSession._request = self.patched  # type: ignore[method-assign, assignment]

    def uninstall(self) -> None:
        ClientSession._request = self.wrapped  # type: ignore[method-assign, assignment]


def supported(version: str) -> bool:
    """Whether this ``aiohttp`` version has client middlewares (3.12 and later)."""
    major, minor = (int(part) for part in version.split(".")[:2])
    return (major, minor) >= (3, 12)


def make_patch() -> AiohttpPatch | None:
    """The patch for the installed ``aiohttp``, or None when it is too old to capture."""
    return AiohttpPatch() if supported(aiohttp.__version__) else None
