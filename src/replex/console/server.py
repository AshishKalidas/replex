"""The local, read-only console: a small FastAPI app over the cassette directories.

It binds to 127.0.0.1 by default, has no authentication, makes no external requests and sends
no telemetry. It only ever reads cassettes, which are already redacted, so the console never
has access to original secrets.
"""

from __future__ import annotations

import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from replex._version import __version__
from replex.cassette.model import Cassette
from replex.cassette.storage import (
    cassette_path,
    list_cassettes,
    load,
    name_from_path,
    sanitise_name,
)
from replex.drift.compare import DriftReport, compare
from replex.drift.diff import diff
from replex.drift.policy import Policy, load_policy
from replex.errors import ReplexError
from replex.providers import build_trace
from replex.trace.model import Trace

STATIC = Path(__file__).parent / "static"
Kind = Literal["baseline", "candidate"]
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'"
)


@dataclass
class _Store:
    baseline_dir: Path
    candidate_dir: Path
    policy_path: Path | None
    _cache: dict[tuple[str, float], Any] = field(default_factory=dict)

    def directory(self, kind: str) -> Path:
        if kind == "baseline":
            return self.baseline_dir
        if kind == "candidate":
            return self.candidate_dir
        raise HTTPException(404, f"unknown run kind {kind!r}")

    def path(self, kind: str, name: str) -> Path:
        if not name or sanitise_name(name) != name:
            raise HTTPException(400, "invalid cassette name")
        p = cassette_path(self.directory(kind), name)
        if not p.is_file():
            raise HTTPException(404, f"no {kind} cassette named {name!r}")
        return p

    def _cached(self, key: str, path: Path, build: Any) -> Any:
        stamp = (key, path.stat().st_mtime)
        if stamp not in self._cache:
            self._cache[stamp] = build()
        return self._cache[stamp]

    def cassette(self, kind: str, name: str) -> Cassette:
        p = self.path(kind, name)
        try:
            result: Cassette = self._cached(f"c:{p}", p, lambda: load(p))
        except ReplexError as exc:
            raise HTTPException(422, str(exc)) from exc
        return result

    def trace(self, kind: str, name: str) -> Trace:
        p = self.path(kind, name)
        result: Trace = self._cached(f"t:{p}", p, lambda: build_trace(self.cassette(kind, name)))
        return result

    def policy(self) -> Policy:
        if self.policy_path and self.policy_path.is_file():
            return load_policy(self.policy_path)
        return Policy()

    def drift(self, name: str) -> DriftReport | None:
        try:
            base, cand = self.path("baseline", name), self.path("candidate", name)
        except HTTPException:
            return None
        key = f"d:{name}:{cand.stat().st_mtime}"
        report: DriftReport = self._cached(
            key,
            base,
            lambda: compare(
                self.trace("baseline", name),
                self.trace("candidate", name),
                self.policy(),
                name=name,
            ),
        )
        return report


def _summary(store: _Store, kind: str, path: Path) -> dict[str, Any]:
    name = name_from_path(path)
    row: dict[str, Any] = {"id": f"{kind}:{name}", "kind": kind, "name": name}
    try:
        c = store.cassette(kind, name)
        t = store.trace(kind, name)
    except HTTPException as exc:
        return {**row, "error": str(exc.detail)}
    report = store.drift(name)
    return {
        **row,
        "provider": c.meta.provider,
        "model": c.meta.model,
        "created_at": c.created_at,
        "steps": t.steps_count,
        "tool_calls": len(t.tool_calls),
        "tool_names": t.tool_names,
        "tokens": t.usage.total,
        "latency_ms": t.total_latency_ms,
        "drift": None if report is None else ("passed" if report.passed else "failed"),
        "warnings": 0 if report is None else len(report.warnings),
    }


def _split(run_id: str) -> tuple[str, str]:
    kind, _, name = run_id.partition(":")
    if not name:
        raise HTTPException(400, "run ids look like 'baseline:<name>' or 'candidate:<name>'")
    return kind, name


def create_app(baseline_dir: Path, candidate_dir: Path, policy_path: Path | None = None) -> FastAPI:
    """Build the console application."""
    store = _Store(Path(baseline_dir), Path(candidate_dir), policy_path)
    app = FastAPI(
        title="RepleX console", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.middleware("http")
    async def security_headers(request: Any, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["content-security-policy"] = CSP
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        return response

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "baseline_dir": str(store.baseline_dir),
            "candidate_dir": str(store.candidate_dir),
        }

    @app.get("/api/runs")
    def runs() -> dict[str, Any]:
        rows = [_summary(store, "baseline", p) for p in list_cassettes(store.baseline_dir)]
        rows += [_summary(store, "candidate", p) for p in list_cassettes(store.candidate_dir)]
        return {"runs": rows}

    @app.get("/api/runs/{kind}/{name}")
    def run(kind: str, name: str) -> dict[str, Any]:
        c = store.cassette(kind, name)
        return {
            "id": f"{kind}:{name}",
            "meta": c.meta.model_dump(mode="json"),
            "created_at": c.created_at,
            "trace": store.trace(kind, name).model_dump(mode="json"),
        }

    @app.get("/api/compare")
    def compare_runs(a: str = Query(...), b: str = Query(...)) -> dict[str, Any]:
        ka, na = _split(a)
        kb, nb = _split(b)
        ta, tb = store.trace(ka, na), store.trace(kb, nb)
        report = compare(ta, tb, store.policy(), name=na)
        return {
            "a": ta.model_dump(mode="json"),
            "b": tb.model_dump(mode="json"),
            "diff": diff(ta, tb).model_dump(mode="json"),
            "report": report.to_dict(),
        }

    @app.get("/api/drift/{name}")
    def drift(name: str) -> dict[str, Any]:
        report = store.drift(name)
        if report is None:
            raise HTTPException(404, f"{name!r} needs both a baseline and a candidate")
        return {
            "name": name,
            "report": report.to_dict(),
            "baseline": store.trace("baseline", name).model_dump(mode="json"),
            "candidate": store.trace("candidate", name).model_dump(mode="json"),
        }

    @app.exception_handler(HTTPException)
    async def http_error(_request: Any, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    if (STATIC / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def index(path: str = "") -> Response:
        page = STATIC / "index.html"
        if not page.is_file():
            return Response(
                "The console front end is not built. Run `make build-console`.",
                media_type="text/plain",
                status_code=503,
            )
        return FileResponse(page, media_type="text/html")

    return app


def serve(
    *,
    baseline_dir: Path,
    candidate_dir: Path,
    policy_path: Path | None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the console with uvicorn until interrupted."""
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"warning: serving on {host}; the console has no authentication. "
            f"Only do this inside a container or on a trusted network."
        )
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"RepleX console: {url}  (baselines: {baseline_dir}, candidates: {candidate_dir})")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        create_app(baseline_dir, candidate_dir, policy_path),
        host=host,
        port=port,
        log_level="warning",
    )
