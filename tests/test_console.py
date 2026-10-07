"""M8: console server API and the single-file HTML report."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import replex
from replex.config import Config
from replex.drift.report import html_report
from replex.testing import MockProvider, MockReply, MockToolCall

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from replex.console.server import STATIC, create_app  # noqa: E402

STATIC_BUILT = (STATIC / "index.html").is_file() and (STATIC / "assets" / "app.js").is_file()


@replex.tool
def lookup(order_id: str) -> dict[str, Any]:
    return {"order_id": order_id, "email": "jane@example.com"}


def run(url: str) -> None:
    import httpx

    msgs: list[Any] = [{"role": "user", "content": "order 7"}]
    with httpx.Client() as c:
        for _ in range(3):
            r = c.post(url + "/v1/messages", json={"model": "mock-sonnet", "messages": msgs}).json()
            msgs.append({"role": "assistant", "content": r["content"]})
            uses = [b for b in r["content"] if b["type"] == "tool_use"]
            if not uses:
                return
            msgs.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": u["id"],
                            "content": json.dumps(lookup(**u["input"])),
                        }
                        for u in uses
                    ],
                }
            )


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    cfg = Config(root=str(tmp_path), intercept_hosts=["127.0.0.1"])
    one = [MockReply(tool_calls=[MockToolCall("lookup", {"order_id": "7"})]), "Order 7 is fine."]
    two = [MockReply(tool_calls=[MockToolCall("lookup", {"order_id": "7"})]), "Order 7 shipped."]
    with MockProvider(one) as m, replex.session("order", mode="record", config=cfg):
        run(m.url)
    with MockProvider(one) as m, replex.session("solo", mode="record", config=cfg):
        run(m.url)
    with MockProvider(two) as m, replex.session("order", mode="drift", config=cfg):
        run(m.url)
    return cfg.cassette_path, cfg.candidate_path


@pytest.fixture
def client(dirs: tuple[Path, Path]) -> TestClient:
    return TestClient(create_app(dirs[0], dirs[1], None))


def test_runs_and_drift_status(client: TestClient) -> None:
    rows = client.get("/api/runs").json()["runs"]
    assert [(r["kind"], r["name"]) for r in rows] == [
        ("baseline", "order"),
        ("baseline", "solo"),
        ("candidate", "order"),
    ]
    order = rows[0]
    assert (
        order["drift"] in ("passed", "failed")
        and order["steps"] == 3
        and order["tool_names"] == ["lookup"]
    )
    assert rows[1]["drift"] is None
    health = client.get("/api/health").json()
    assert health["ok"] and health["version"] == replex.__version__


def test_run_compare_and_drift(client: TestClient) -> None:
    run_ = client.get("/api/runs/baseline/order").json()
    assert run_["trace"]["final_output"] == "Order 7 is fine."
    assert run_["meta"]["provider"] == "mock"
    assert "jane@example.com" not in json.dumps(run_)  # the console only sees redacted data
    cmp = client.get("/api/compare", params={"a": "baseline:order", "b": "candidate:order"}).json()
    assert cmp["diff"]["entries"][-1]["status"] == "changed"
    assert cmp["report"]["name"] == "order"
    drift = client.get("/api/drift/order").json()
    assert drift["candidate"]["final_output"] == "Order 7 shipped."
    assert {f["check"] for f in drift["report"]["findings"]} >= {"latency"}


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("/api/runs/baseline/missing", 404),
        ("/api/runs/other/order", 404),
        ("/api/runs/baseline/..%2F..%2Fetc", 404),
        ("/api/runs/baseline/a%20b", 400),
        ("/api/runs/baseline/...", 400),
        ("/api/nothing", 404),
        ("/api/compare?a=order&b=candidate:order", 400),
        ("/api/drift/solo", 404),
    ],
)
def test_errors(client: TestClient, url: str, status: int) -> None:
    r = client.get(url)
    assert r.status_code == status
    assert "error" in r.json()


def test_invalid_cassette_is_reported(client: TestClient, dirs: tuple[Path, Path]) -> None:
    (dirs[0] / "broken.replex.json").write_text("{}")
    rows = client.get("/api/runs").json()["runs"]
    assert any(r["name"] == "broken" and "error" in r for r in rows)
    assert client.get("/api/runs/baseline/broken").status_code == 422


def test_security_headers_and_index(client: TestClient) -> None:
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "connect-src 'self'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    if STATIC_BUILT:
        assert r.status_code == 200 and '<div id="root">' in r.text
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/run/baseline/order").status_code == 200  # SPA fallback
    else:  # pragma: no cover
        assert r.status_code == 503


def test_policy_file_is_used(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    pol = tmp_path / "strict.yaml"
    pol.write_text("version: 1\ndefaults:\n  final_output: {mode: exact, severity: fail}\n")
    c = TestClient(create_app(dirs[0], dirs[1], pol))
    assert c.get("/api/drift/order").json()["report"]["passed"] is False
    assert c.get("/api/runs").json()["runs"][0]["drift"] == "failed"


# ------------------------------------------------------------------ single-file HTML report

EXTERNAL = re.compile(
    r"""(?:src|href|action|poster)\s*=\s*["']?\s*(?:https?:)?//"""  # attributes loading a URL
    r"""|url\(\s*["']?\s*(?:https?:)?//"""  # CSS url()
    r"""|@import"""  # CSS imports
    r"""|(?:fetch|import|importScripts|sendBeacon)\s*\(\s*["'`]\s*(?:https?:)?//""",  # JS loads
    re.IGNORECASE,
)


@pytest.mark.skipif(not STATIC_BUILT, reason="console front end not built")
def test_html_report_is_self_contained(dirs: tuple[Path, Path]) -> None:
    report = replex.compare(dirs[0] / "order.replex.json", dirs[1] / "order.replex.json")
    html = html_report([report])
    assert html.lower().startswith("<!doctype html>")
    assert '<script id="replex-data" type="application/json">' in html
    assert '<script type="module">' in html and "<style>" in html
    assert not re.search(r"<script[^>]*\bsrc=", html), "scripts must be inlined"
    assert not re.search(r"<link[^>]*rel=\"stylesheet\"", html), "styles must be inlined"
    assert not EXTERNAL.search(html), EXTERNAL.search(html)
    data = re.search(r'<script id="replex-data" type="application/json">(.*?)</script>', html, re.S)
    assert data is not None
    payload = json.loads(data.group(1).replace("<\\/", "</"))
    assert payload["kind"] == "replex-report"
    assert payload["pairs"][0]["candidate"]["final_output"] == "Order 7 shipped."
    assert "jane@example.com" not in html


def test_report_data_escapes_script_terminators() -> None:
    from replex.drift.report import _script_json

    text = _script_json({"x": "</script><script>alert(1)</script> "})
    assert "</script" not in text and " " not in text
