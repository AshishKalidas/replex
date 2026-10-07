from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host's CI / RepleX environment from leaking into tests."""
    for var in (
        "CI",
        "REPLEX_MODE",
        "REPLEX_ALLOW_RECORD_IN_CI",
        "REPLEX_CASSETTE_DIR",
        "REPLEX_MATCH",
        "REPLEX_TOOLS",
        "REPLEX_POLICY",
        "REPLEX_CANDIDATE_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REPLEX_GIT_SHA", "test000")


# ------------------------------------------------------------------ a plain local HTTP server

import gzip  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class PlainServer:
    """A tiny HTTP server with JSON, streaming and gzip endpoints. Counts every hit."""

    def __init__(self) -> None:
        self.hits = 0
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # silence
                pass

            def _body(self) -> bytes:
                n = int(self.headers.get("content-length") or 0)
                return self.rfile.read(n) if n else b""

            def _send(
                self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.send_header("x-request-token", "server-secret-value")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._handle(b"")

            def do_POST(self) -> None:
                self._handle(self._body())

            def _handle(self, raw: bytes) -> None:
                with server.lock:
                    server.hits += 1
                    n = server.hits
                if self.path.startswith("/stream"):
                    events = [
                        b'event: start\ndata: {"type":"start","n":%d}\n\n' % n,
                        b'event: delta\ndata: {"type":"delta","text":"Hel"}\n\n',
                        b'event: delta\ndata: {"type":"delta","text":"lo"}\n\n',
                        b": a comment line\n\n",
                        b"data: [DONE]\n\n",
                    ]
                    body = b"".join(events)
                    self._send(200, body, "text/event-stream")
                elif self.path.startswith("/gzip"):
                    self._send(
                        200,
                        gzip.compress(b'{"zipped": true}'),
                        "application/json",
                        {"content-encoding": "gzip"},
                    )
                elif self.path.startswith("/text"):
                    self._send(200, b"plain text reply", "text/plain")
                elif self.path.startswith("/binary"):
                    self._send(200, bytes(range(256)), "application/octet-stream")
                elif self.path.startswith("/empty"):
                    self._send(204, b"", "text/plain")
                elif self.path.startswith("/error"):
                    self._send(500, b'{"error": "boom"}', "application/json")
                else:
                    try:
                        payload = json.loads(raw) if raw else None
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        payload = raw.decode("utf-8", "replace")
                    body = json.dumps({"echo": payload, "hit": n, "path": self.path}).encode()
                    self._send(200, body, "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def plain_server() -> Iterator[PlainServer]:
    srv = PlainServer()
    try:
        yield srv
    finally:
        srv.close()
