"""The capture context: the state shared by HTTP capture and tool capture during a session.

The capture layer knows nothing about providers. It stores raw, redacted exchanges and tool
calls in one ordered event list, and serves them back during replay.
"""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from replex.capture import matching
from replex.cassette.model import (
    BodyEncoding,
    Cassette,
    Event,
    ExchangeEvent,
    RecordedRequest,
    RecordedResponse,
    StreamEvent,
    ToolCallEvent,
)
from replex.errors import NetworkBlocked, ReplayMismatch, UnusedRecording
from replex.redaction.engine import Redactor

CaptureMode = Literal["record", "replay"]
ToolsMode = Literal["replay", "live", "auto"]
MatchMode = Literal["strict", "sequence"]

# Headers that describe the transfer, not the content. Bodies are stored decoded.
_TRANSFER_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


@dataclass
class HttpSettings:
    """HTTP capture settings, resolved from configuration."""

    intercept_hosts: Sequence[str] = ("api.anthropic.com",)
    passthrough_hosts: Sequence[str] = ("localhost", "127.0.0.1")
    match: MatchMode = "strict"
    ignore_request_fields: Sequence[str] = matching.DEFAULT_IGNORE_FIELDS
    normalisers: Sequence[matching.BodyNormaliser] = ()
    strict_unused: bool = True


def _host_matches(url: httpx.URL, patterns: Sequence[str]) -> bool:
    host = (url.host or "").lower()
    hostport = f"{host}:{url.port}" if url.port else host
    return any(
        fnmatch.fnmatchcase(host, p.lower()) or fnmatch.fnmatchcase(hostport, p.lower())
        for p in patterns
    )


def decode_body(content: bytes, content_type: str = "") -> tuple[Any, BodyEncoding]:
    """Decode raw bytes into the form stored in a cassette."""
    if not content:
        return None, "empty"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(content).decode("ascii"), "base64"
    if "json" in content_type or text.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(text), "json"
        except json.JSONDecodeError:
            pass
    return text, "text"


def encode_body(body: Any, encoding: BodyEncoding) -> bytes:
    """Inverse of :func:`decode_body`."""
    if encoding == "empty" or body is None:
        return b""
    if encoding == "base64":
        return base64.b64decode(body)
    if encoding == "text":
        return str(body).encode("utf-8")
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _counters_from(value: Any) -> dict[str, int]:
    r = Redactor()
    r.observe(value)
    return dict(r._counters)


class _Pending:
    """Placeholder that reserves an event's position while a response is still streaming."""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = index


@dataclass
class CaptureContext:
    """Everything the transport and the tool decorator need while a session is active."""

    mode: CaptureMode
    http: HttpSettings = field(default_factory=HttpSettings)
    tools_mode: ToolsMode | None = None
    redactor: Redactor = field(default_factory=Redactor)
    baseline: Cassette | None = None
    cassette_label: str = "<cassette>"
    stats: dict[str, int] = field(default_factory=lambda: {"tool_executions": 0})

    def __post_init__(self) -> None:
        if self.tools_mode is None:
            # replay serves recorded results; drift (record + baseline) replays what it can
            # and runs safe tools; plain recording runs everything live.
            if self.mode == "replay":
                self.tools_mode = "replay"
            else:
                self.tools_mode = "auto" if self.baseline is not None else "live"
        self.lock = threading.RLock()
        self.events: list[Event | _Pending] = []
        self.open_streams: list[Callable[[], None]] = []
        self._used: set[int] = set()
        self._request_counter = 0
        # Replay: the redactor that maps live originals onto the cassette's tokens.
        self._replay_redactor = Redactor(self.redactor.rules)
        self._prefix_counters: list[dict[str, int]] = []
        if self.baseline is not None:
            running: dict[str, int] = {}
            for event in self.baseline.events:
                self._prefix_counters.append(dict(running))
                for kind, n in _counters_from(event.model_dump(mode="json")).items():
                    running[kind] = max(running.get(kind, 0), n)
            self._prefix_counters.append(dict(running))

    # ------------------------------------------------------------- event list
    def reserve(self) -> _Pending:
        """Reserve the next position in the event list."""
        with self.lock:
            pending = _Pending(len(self.events))
            self.events.append(pending)
            return pending

    def fill(self, pending: _Pending, event: Event) -> None:
        """Replace a reservation with the finished event."""
        with self.lock:
            self.events[pending.index] = event

    def drop(self, pending: _Pending) -> None:
        """Forget a reservation (the request failed before a response arrived)."""
        with self.lock:
            self.events[pending.index] = _Pending(-1)

    def append(self, make: Callable[[int], Event]) -> Event:
        """Append an event built from its index."""
        with self.lock:
            event = make(len(self.events))
            self.events.append(event)
            return event

    def finalize_streams(self) -> None:
        """File any streamed exchange whose stream was never closed by the caller."""
        for finish in list(self.open_streams):
            finish()
        self.open_streams.clear()

    def finished_events(self) -> list[Event]:
        """The run's events with failed reservations removed and indexes renumbered."""
        with self.lock:
            done = [e for e in self.events if not isinstance(e, _Pending)]
        return [e.model_copy(update={"index": i}) for i, e in enumerate(done)]

    # ------------------------------------------------------------- routing
    def intercepts(self, url: httpx.URL) -> bool:
        return _host_matches(url, self.http.intercept_hosts)

    def passes_through(self, url: httpx.URL) -> bool:
        return _host_matches(url, self.http.passthrough_hosts)

    def blocked(self, request: httpx.Request) -> NetworkBlocked:
        return NetworkBlocked(
            f"RepleX blocked {request.method} {request.url} during replay.\n"
            f"  Only hosts in intercept_hosts {list(self.http.intercept_hosts)} are served from "
            f"the cassette, and only passthrough_hosts {list(self.http.passthrough_hosts)} may "
            f"reach the network.\n"
            f"  If this is a model call, add {request.url.host!r} to intercept_hosts and "
            f"re-record; if it is a local service, add it to passthrough_hosts."
        )

    # ------------------------------------------------------------- recording
    def _redact_request(
        self, r: Redactor, method: str, url: str, headers: dict[str, str], content: bytes
    ) -> tuple[str, dict[str, str], Any, BodyEncoding]:
        red_url = r.url(url)
        red_headers = r.headers(headers)
        body, encoding = decode_body(content, headers.get("content-type", ""))
        if encoding in ("json", "text"):
            body = r.value(body)
        return red_url, red_headers, body, encoding

    def _key(self, method: str, url: str, body: Any) -> str:
        normalised = matching.normalise_body(
            body, self.http.ignore_request_fields, self.http.normalisers
        )
        return matching.match_key(method, url, normalised)

    def recorded_request(self, request: httpx.Request) -> RecordedRequest:
        """Redact and describe an outgoing request."""
        with self.lock:
            url, headers, body, encoding = self._redact_request(
                self.redactor,
                request.method,
                str(request.url),
                dict(request.headers),
                request.content,
            )
        return RecordedRequest(
            method=request.method,
            url=url,
            headers=headers,
            body=body,
            body_encoding=encoding,
            match_key=self._key(request.method, url, body),
        )

    def recorded_response(
        self,
        status: int,
        headers: httpx.Headers,
        content: bytes | None,
        stream: list[StreamEvent] | None,
    ) -> RecordedResponse:
        """Redact and describe a response (decoded body, or the list of stream events)."""
        with self.lock:
            red_headers = self.redactor.headers(
                {k: v for k, v in headers.items() if k.lower() not in _TRANSFER_HEADERS}
            )
            if stream is not None:
                red_stream = [
                    e.model_copy(update={"data": self.redactor.value(e.data)}) for e in stream
                ]
                return RecordedResponse(status=status, headers=red_headers, stream=red_stream)
            body, encoding = decode_body(content or b"", headers.get("content-type", ""))
            if encoding in ("json", "text"):
                body = self.redactor.value(body)
            return RecordedResponse(
                status=status, headers=red_headers, body=body, body_encoding=encoding
            )

    # ------------------------------------------------------------- replay
    @contextlib.contextmanager
    def _speculative(self, before_index: int) -> Iterator[Redactor]:
        fork = self._replay_redactor.fork()
        prefix = self._prefix_counters[min(before_index, len(self._prefix_counters) - 1)]
        for kind, n in prefix.items():
            if n > fork._counters.get(kind, 0):
                fork._counters[kind] = n
        yield fork

    def replay_exchange(self, request: httpx.Request) -> ExchangeEvent:
        """Find the recorded exchange for a live request, or raise ``ReplayMismatch``."""
        assert self.baseline is not None
        with self.lock:
            number = self._request_counter
            self._request_counter += 1
            unused = [e for e in self.baseline.exchanges if e.index not in self._used]
            headers = dict(request.headers)
            if self.http.match == "sequence":
                if unused:
                    chosen = unused[0]
                    self._used.add(chosen.index)
                    return chosen
            else:
                for candidate in unused:
                    with self._speculative(candidate.index) as fork:
                        url, _, body, _ = self._redact_request(
                            fork, request.method, str(request.url), headers, request.content
                        )
                        rec = candidate.request
                        if self._key(request.method, url, body) == self._key(
                            rec.method, rec.url, rec.body
                        ):
                            self._replay_redactor.adopt(fork)
                            self._used.add(candidate.index)
                            return candidate
            # No match: build the most useful error we can.
            with self._speculative(unused[0].index if unused else 10**9) as fork:
                _, _, actual, _ = self._redact_request(
                    fork, request.method, str(request.url), headers, request.content
                )
            closest = self._closest(unused, request)
            expected = (
                None
                if closest is None
                else (
                    closest.index,
                    closest.request.method,
                    closest.request.url,
                    matching.normalise_body(
                        closest.request.body,
                        self.http.ignore_request_fields,
                        self.http.normalisers,
                    ),
                )
            )
            raise ReplayMismatch(
                matching.format_mismatch(
                    request_index=number,
                    method=request.method,
                    url=str(request.url),
                    actual_body=matching.normalise_body(
                        actual, self.http.ignore_request_fields, self.http.normalisers
                    ),
                    expected=expected,
                    cassette=self.cassette_label,
                    match_mode=self.http.match,
                    remaining=len(unused),
                )
            )

    @staticmethod
    def _closest(unused: list[ExchangeEvent], request: httpx.Request) -> ExchangeEvent | None:
        path = request.url.path
        for e in unused:
            if httpx.URL(e.request.url).path == path and e.request.method == request.method:
                return e
        return unused[0] if unused else None

    def unused_exchanges(self) -> list[ExchangeEvent]:
        """Recorded exchanges that were never served."""
        if self.baseline is None:
            return []
        return [e for e in self.baseline.exchanges if e.index not in self._used]

    def check_unused(self) -> None:
        """Raise ``UnusedRecording`` if replay left recorded exchanges unserved."""
        if self.mode != "replay" or not self.http.strict_unused:
            return
        unused = self.unused_exchanges()
        if unused:
            listing = "\n".join(
                f"    event {e.index}: {e.request.method} {httpx.URL(e.request.url).path}"
                for e in unused[:10]
            )
            raise UnusedRecording(
                f"{len(unused)} recorded exchange(s) in {self.cassette_label} were never "
                f"requested during replay:\n{listing}\n"
                f"  The agent made fewer model calls than when it was recorded. Re-record if "
                f"this is intended, or set strict_unused = false."
            )

    # ------------------------------------------------------------- tools
    def find_tool(self, name: str, args: dict[str, Any]) -> ToolCallEvent | None:
        """The first unused recorded call with this name and (redacted) arguments."""
        if self.baseline is None:
            return None
        with self.lock:
            for candidate in self.baseline.tool_calls:
                if candidate.name != name or candidate.index in self._used:
                    continue
                with self._speculative(candidate.index) as fork:
                    redacted = fork.value(args)
                    if matching.canonical_json(redacted) == matching.canonical_json(candidate.args):
                        self._replay_redactor.adopt(fork)
                        self._used.add(candidate.index)
                        return candidate
        return None

    def recorded_tool_names(self, name: str) -> list[dict[str, Any]]:
        """Recorded argument sets for a tool (for error messages)."""
        if self.baseline is None:
            return []
        return [c.args for c in self.baseline.tool_calls if c.name == name]
