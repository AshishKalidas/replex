"""Capture for ``requests``: ``HTTPAdapter.send`` is wrapped while a session is active.

Every ``requests`` session sends through an adapter, and adapters that override ``send``
normally call the base method, so wrapping it sees all traffic. A captured request is handled
as an ``httpx.Request`` (see :mod:`replex.capture.bridge`) and the result is turned back into
a ``requests.Response`` whose body is read lazily, so a streamed reply arrives chunk by chunk.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from http.client import HTTPMessage
from typing import Any

import httpx
import requests
from requests.adapters import HTTPAdapter
from requests.cookies import extract_cookies_to_jar
from requests.models import PreparedRequest
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers

from replex import _runtime
from replex.capture.bridge import copy_changed_headers, decoded_headers
from replex.capture.transport import handle_sync

Send = Callable[..., requests.Response]

_CHUNK = 64 * 1024


def _request_body(request: PreparedRequest) -> bytes:
    """The request body as bytes. A file or generator body is read once and replaced by them."""
    body = request.body
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    parts: Iterable[bytes | str] = [body.read()] if hasattr(body, "read") else body
    content = b"".join(p.encode("utf-8") if isinstance(p, str) else p for p in parts)
    request.body = content
    return content


def _to_httpx(request: PreparedRequest) -> httpx.Request:
    assert request.method is not None
    assert request.url is not None
    headers = [
        (name, value if isinstance(value, str) else value.decode("latin-1"))
        for name, value in request.headers.items()
    ]
    return httpx.Request(
        request.method,
        request.url,
        headers=headers,
        content=_request_body(request) or None,
    )


class _LiveStream(httpx.SyncByteStream):
    """The undecoded body of a live ``requests`` response, as httpx reads it."""

    def __init__(self, response: requests.Response) -> None:
        self._response = response

    def __iter__(self) -> Iterator[bytes]:
        yield from self._response.raw.stream(_CHUNK, decode_content=False)

    def close(self) -> None:
        self._response.close()


class _OriginalResponse:
    """Carries the headers where ``requests`` looks for cookies to store."""

    def __init__(self, headers: httpx.Headers) -> None:
        self.msg = HTTPMessage()
        for name, value in headers.multi_items():
            self.msg[name] = value


class _Body:
    """Stands in for the urllib3 response behind ``requests.Response.raw``.

    It serves the decoded body of an httpx response, keeping its chunks: a chunk larger than
    the size the caller asks for is split, smaller ones are never joined.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._chunks = response.iter_bytes()
        self._original_response = _OriginalResponse(response.headers)

    def stream(self, amt: int | None = None, decode_content: bool | None = None) -> Iterator[bytes]:
        for chunk in self._chunks:
            if not amt:
                yield chunk
                continue
            for start in range(0, len(chunk), amt):
                yield chunk[start : start + amt]

    def read(self, amt: int | None = None, decode_content: bool | None = None) -> bytes:
        return b"".join(self.stream())

    def close(self) -> None:
        self._response.close()


def _to_requests(
    response: httpx.Response, request: PreparedRequest, adapter: HTTPAdapter
) -> requests.Response:
    """Build the ``requests.Response`` that :meth:`HTTPAdapter.build_response` would."""
    out = requests.Response()
    out.status_code = response.status_code
    out.headers = CaseInsensitiveDict(decoded_headers(response.headers))
    out.encoding = get_encoding_from_headers(out.headers)
    out.raw = _Body(response)
    out.reason = response.reason_phrase
    out.url = str(request.url)
    out.request = request
    out.connection = adapter
    extract_cookies_to_jar(out.cookies, request, out.raw)
    return out


class RequestsPatch:
    """The patch for ``requests``.

    The wrapped method is the one in place at install time, so a patch another library applied
    first (``responses``, instrumentation) keeps working underneath ours.
    """

    def __init__(self) -> None:
        self.wrapped: Send = HTTPAdapter.send
        patch = self

        def patched(
            adapter: HTTPAdapter, request: PreparedRequest, *args: Any, **kwargs: Any
        ) -> requests.Response:
            inner = patch.wrapped
            ctx = _runtime.active_capture()
            if ctx is None or ctx.sends_unchanged(httpx.URL(str(request.url))):
                return inner(adapter, request, *args, **kwargs)

            def send(r: httpx.Request) -> httpx.Response:
                copy_changed_headers(request.headers, r)
                live = inner(adapter, request, *args, **kwargs)
                # urllib3 keeps repeated headers (Set-Cookie) apart; requests joins them.
                headers = getattr(live.raw, "headers", live.headers)
                return httpx.Response(
                    live.status_code,
                    headers=list(headers.items()),
                    stream=_LiveStream(live),
                    request=r,
                )

            response = handle_sync(ctx, _to_httpx(request), send)
            return _to_requests(response, request, adapter)

        self.patched: Send = patched

    def install(self) -> None:
        self.wrapped = HTTPAdapter.send
        HTTPAdapter.send = self.patched  # type: ignore[method-assign]

    def uninstall(self) -> None:
        HTTPAdapter.send = self.wrapped  # type: ignore[method-assign]


def make_patch() -> RequestsPatch:
    """The patch for the installed ``requests``."""
    return RequestsPatch()
