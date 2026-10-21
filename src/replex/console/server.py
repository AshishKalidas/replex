"""The local, read-only console: a small FastAPI app over one or more workspaces.

A workspace is a baseline directory, a candidate directory and a policy. The console serves
your project's workspace and the bundled samples. In your project it can also accept a
candidate as the new baseline, or discard it; everything else is read-only. It binds to
127.0.0.1 by default, has no authentication, makes no external requests and sends no
telemetry. Cassettes are redacted before they are written, so the console never sees secrets.
"""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from replex._version import __version__
from replex.cassette.model import Cassette
from replex.cassette.storage import (
    cassette_path,
    list_cassettes,
    load,
    name_from_path,
    promote_candidate,
    sanitise_name,
)
from replex.config import check_ci_guard
from replex.drift.compare import DriftReport, compare
from replex.drift.policy import Policy, load_policy
from replex.errors import RecordingRefused, ReplexError
from replex.pricing import Pricing
from replex.providers import build_trace
from replex.trace.model import Trace

STATIC = Path(__file__).parent / "static"
# Requests must name the console by a loopback host. Without this check a web page could use
# DNS rebinding to point its own hostname at 127.0.0.1 and read the cassettes.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
ACTION_HEADER = "x-replex-action"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'"
)


def _mtime(path: Path | None) -> float | None:
    return path.stat().st_mtime if path is not None and path.is_file() else None


@dataclass
class _Store:
    baseline_dir: Path
    candidate_dir: Path
    policy_path: Path | None
    pricing: Pricing | None = None
    # key -> (version stamp, value). One entry per key: a new stamp replaces the old value.
    _cache: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    def directory(self, kind: str) -> Path:
        if kind == "baseline":
            return self.baseline_dir
        if kind == "candidate":
            return self.candidate_dir
        raise HTTPException(404, f"unknown run kind {kind!r}")

    def path(self, kind: str, name: str) -> Path:
        try:
            valid = bool(name) and sanitise_name(name) == name
        except ReplexError:
            valid = False
        if not valid:
            raise HTTPException(400, "invalid cassette name")
        p = cassette_path(self.directory(kind), name)
        if not p.is_file():
            raise HTTPException(404, f"no {kind} cassette named {name!r}")
        return p

    def _cached(self, key: str, stamp: Any, build: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        value = build()
        self._cache[key] = (stamp, value)
        return value

    def cassette(self, kind: str, name: str) -> Cassette:
        p = self.path(kind, name)
        try:
            result: Cassette = self._cached(f"cassette:{p}", _mtime(p), lambda: load(p))
        except ReplexError as exc:
            raise HTTPException(422, str(exc)) from exc
        return result

    def trace(self, kind: str, name: str) -> Trace:
        p = self.path(kind, name)
        result: Trace = self._cached(
            f"trace:{p}",
            _mtime(p),
            lambda: build_trace(self.cassette(kind, name), pricing=self.pricing),
        )
        return result

    def policy(self) -> Policy:
        result: Policy = self._cached(
            "policy",
            _mtime(self.policy_path),
            lambda: load_policy(self.policy_path) if _mtime(self.policy_path) else Policy(),
        )
        return result

    def drift(self, name: str) -> DriftReport | None:
        try:
            base, cand = self.path("baseline", name), self.path("candidate", name)
        except HTTPException:
            return None
        report: DriftReport = self._cached(
            f"drift:{name}",
            (_mtime(base), _mtime(cand), _mtime(self.policy_path)),
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
        "cost": t.cost,
        "latency_ms": t.total_latency_ms,
        "drift": None if report is None else ("passed" if report.passed else "failed"),
        "warnings": 0 if report is None else len(report.warnings),
    }


def _split(run_id: str) -> tuple[str, str]:
    kind, _, name = run_id.partition(":")
    if not name:
        raise HTTPException(400, "run ids look like 'baseline:<name>' or 'candidate:<name>'")
    return kind, name


@dataclass(frozen=True)
class Workspace:
    """A set of baselines, candidates and a policy that the console can show.

    ``project`` is your own repository (from ``[tool.replex]`` or the command line); the
    samples ship inside the package, so the console has something to show before you have
    recorded anything.
    """

    id: str
    label: str
    baseline_dir: Path
    candidate_dir: Path
    policy_path: Path | None = None
    #: Whether the console may accept or discard candidates here (never for a sample).
    writable: bool = False
    #: Prices used for costs; the built-in table when ``None``.
    pricing: Pricing | None = None

    def describe(self) -> dict[str, Any]:
        baselines = list_cassettes(self.baseline_dir)
        candidates = list_cassettes(self.candidate_dir)
        return {
            "id": self.id,
            "label": self.label,
            "baseline_dir": str(self.baseline_dir),
            "candidate_dir": str(self.candidate_dir),
            "policy": str(self.policy_path) if self.policy_path else None,
            "baselines": len(baselines),
            "candidates": len(candidates),
            "writable": self.writable,
        }


#: The samples that ship inside the package: workspace id -> (label, directory).
BUNDLED_SAMPLES: dict[str, tuple[str, Path]] = {
    "sample": ("Sample workspace", Path(__file__).parent / "sample"),
    "sample-models": ("Sample: model swap", Path(__file__).parent / "sample-models"),
}
SAMPLE = BUNDLED_SAMPLES["sample"][1]


def sample_workspace(sample_id: str = "sample") -> Workspace:
    """A bundled sample, read-only.

    ``sample`` is the support agent against the mock model, whose prompt change drifts in two
    scenarios. ``sample-models`` holds real recordings from Claude Sonnet 5.5, with Claude Haiku
    5.5's runs of the same scenarios as the latest runs.
    """
    label, root = BUNDLED_SAMPLES[sample_id]
    return Workspace(
        id=sample_id,
        label=label,
        baseline_dir=root / "cassettes",
        candidate_dir=root / "candidates",
        policy_path=root / "replex.policy.yaml",
    )


def sample_workspaces() -> list[Workspace]:
    """Every bundled sample, in the order the console offers them."""
    return [sample_workspace(sample_id) for sample_id in BUNDLED_SAMPLES]


def project_workspace(
    baseline_dir: Path,
    candidate_dir: Path,
    policy_path: Path | None = None,
    *,
    pricing: Pricing | None = None,
) -> Workspace:
    """Your own cassettes, read live from disk, priced with ``pricing``."""
    return Workspace(
        id="project",
        label="Your project",
        baseline_dir=Path(baseline_dir),
        candidate_dir=Path(candidate_dir),
        policy_path=policy_path,
        writable=True,
        pricing=pricing,
    )


def default_workspace(workspaces: Sequence[Workspace]) -> str:
    """The project when it has cassettes, otherwise the first workspace that does."""
    for ws in workspaces:
        if list_cassettes(ws.baseline_dir) or list_cassettes(ws.candidate_dir):
            return ws.id
    return workspaces[0].id


def create_app(
    workspaces: Sequence[Workspace],
    *,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
) -> FastAPI:
    """Build the console application.

    Args:
        workspaces: What the console can show, in the order offered to the user.
        allowed_hosts: Host header values the console answers to (``*`` disables the check).
    """
    if not workspaces:
        raise ValueError("the console needs at least one workspace")
    by_id = {ws.id: ws for ws in workspaces}
    stores = {
        ws.id: _Store(Path(ws.baseline_dir), Path(ws.candidate_dir), ws.policy_path, ws.pricing)
        for ws in workspaces
    }
    app = FastAPI(
        title="RepleX console", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.middleware("http")
    async def security_headers(request: Any, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["content-security-policy"] = CSP
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        # Revalidate on every load, so the browser never keeps an old console after an upgrade
        # and API responses always reflect the files on disk.
        response.headers["cache-control"] = "no-cache"
        return response

    def store_for(ws: str) -> _Store:
        store = stores.get(ws)
        if store is None:
            raise HTTPException(404, f"no workspace named {ws!r}")
        return store

    def writable_store(ws: str, request: Request) -> _Store:
        """Guards for the two write actions.

        The custom header cannot be sent cross-origin without a CORS preflight, which this
        server never approves, so another website cannot trigger a write even on localhost.
        """
        if request.headers.get(ACTION_HEADER) != "1":
            raise HTTPException(403, f"write requests need the {ACTION_HEADER} header")
        store = store_for(ws)
        if not by_id[ws].writable:
            raise HTTPException(403, f"the {by_id[ws].label.lower()} is read-only")
        try:
            check_ci_guard("rewrite")
        except RecordingRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        return store

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/workspaces")
    def list_workspaces() -> dict[str, Any]:
        return {
            "workspaces": [ws.describe() for ws in workspaces],
            "default": default_workspace(workspaces),
        }

    @app.get("/api/w/{ws}/runs")
    def runs(ws: str) -> dict[str, Any]:
        store = store_for(ws)
        rows = [_summary(store, "baseline", p) for p in list_cassettes(store.baseline_dir)]
        rows += [_summary(store, "candidate", p) for p in list_cassettes(store.candidate_dir)]
        return {"runs": rows}

    @app.get("/api/w/{ws}/runs/{kind}/{name}")
    def run(ws: str, kind: str, name: str) -> dict[str, Any]:
        store = store_for(ws)
        c = store.cassette(kind, name)
        return {
            "id": f"{kind}:{name}",
            "meta": c.meta.model_dump(mode="json"),
            "created_at": c.created_at,
            "trace": store.trace(kind, name).model_dump(mode="json"),
        }

    @app.get("/api/w/{ws}/compare")
    def compare_runs(ws: str, a: str = Query(...), b: str = Query(...)) -> dict[str, Any]:
        store = store_for(ws)
        ka, na = _split(a)
        kb, nb = _split(b)
        ta, tb = store.trace(ka, na), store.trace(kb, nb)
        report = compare(ta, tb, store.policy(), name=na)
        return {
            "a": ta.model_dump(mode="json"),
            "b": tb.model_dump(mode="json"),
            "diff": report.diff.model_dump(mode="json"),
            "report": report.to_dict(),
        }

    @app.get("/api/w/{ws}/drift/{name}")
    def drift(ws: str, name: str) -> dict[str, Any]:
        store = store_for(ws)
        report = store.drift(name)
        if report is None:
            raise HTTPException(404, f"{name!r} needs both a baseline and a candidate")
        return {
            "name": name,
            "report": report.to_dict(),
            "baseline": store.trace("baseline", name).model_dump(mode="json"),
            "candidate": store.trace("candidate", name).model_dump(mode="json"),
        }

    @app.post("/api/w/{ws}/scenarios/{name}/accept")
    def accept(ws: str, name: str, request: Request) -> dict[str, Any]:
        store = writable_store(ws, request)
        store.path("candidate", name)  # validates the name, 404 when there is nothing to accept
        try:
            written = promote_candidate(store.candidate_dir, store.baseline_dir, name)
        except ReplexError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"accepted": name, "baseline": str(written)}

    @app.post("/api/w/{ws}/scenarios/{name}/discard")
    def discard(ws: str, name: str, request: Request) -> dict[str, Any]:
        store = writable_store(ws, request)
        store.path("candidate", name).unlink()
        return {"discarded": name}

    @app.exception_handler(HTTPException)
    async def http_error(_request: Any, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    if (STATIC / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def index(path: str = "") -> Response:
        if path.startswith("api/"):
            raise HTTPException(404, f"no API endpoint /{path}")
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
    workspaces: Sequence[Workspace],
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
    print(f"RepleX console: {url}")
    for ws in workspaces:
        print(f"  {ws.label}: {ws.baseline_dir} (candidates: {ws.candidate_dir})")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    hosts = list(LOCAL_HOSTS)
    if host not in ("0.0.0.0", "::", *LOCAL_HOSTS):
        hosts.append(host)
    uvicorn.run(
        create_app(workspaces, allowed_hosts=hosts),
        host=host,
        port=port,
        log_level="warning",
    )
