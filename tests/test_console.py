"""M8: console server API and the single-file HTML report."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

import replex
from replex.config import Config
from replex.drift.report import html_report
from replex.testing import MockProvider, MockReply, MockToolCall

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from replex.console.server import (  # noqa: E402
    SAMPLE,
    STATIC,
    Workspace,
    create_app,
    default_workspace,
    project_workspace,
    sample_workspace,
)

LOCAL = "http://127.0.0.1:8765"

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


def app_for(dirs: tuple[Path, Path], policy: Path | None = None) -> Any:
    return create_app([project_workspace(dirs[0], dirs[1], policy), sample_workspace()])


@pytest.fixture
def client(dirs: tuple[Path, Path]) -> TestClient:
    return TestClient(app_for(dirs), base_url=LOCAL)


def test_runs_and_drift_status(client: TestClient) -> None:
    rows = client.get("/api/w/project/runs").json()["runs"]
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


def test_workspaces_list_project_and_sample(client: TestClient) -> None:
    data = client.get("/api/workspaces").json()
    assert [w["id"] for w in data["workspaces"]] == ["project", "sample"]
    assert data["default"] == "project"  # it has cassettes
    project = data["workspaces"][0]
    assert (project["baselines"], project["candidates"]) == (2, 1)
    assert project["writable"] is True
    assert data["workspaces"][1]["label"] == "Sample workspace"
    assert data["workspaces"][1]["writable"] is False


def test_empty_project_opens_on_the_sample(tmp_path: Path) -> None:
    empty = project_workspace(tmp_path / "none", tmp_path / "none-either")
    assert default_workspace([empty, sample_workspace()]) == "sample"
    assert default_workspace([empty]) == "project"
    c = TestClient(create_app([empty, sample_workspace()]), base_url=LOCAL)
    assert c.get("/api/w/project/runs").json()["runs"] == []


def test_sample_workspace_ships_a_drifting_example() -> None:
    """The bundled sample is the support-agent example: v2 drifts in the two refund flows."""
    c = TestClient(create_app([sample_workspace()]), base_url=LOCAL)
    rows = c.get("/api/w/sample/runs").json()["runs"]
    drifted = sorted(r["name"] for r in rows if r["kind"] == "candidate" and r["drift"] == "failed")
    assert drifted == ["refund_ineligible_openai", "refund_request"]
    assert all(r["provider"] == "mock" for r in rows)  # never passes for a real model
    report = c.get("/api/w/sample/drift/refund_request").json()["report"]
    assert {f["check"] for f in report["findings"] if f["severity"] == "fail"} == {
        "tool_sequence",
        "new_tools",
    }


def test_sample_baselines_match_the_example() -> None:
    """`make sample` copies the example's committed cassettes; they must not drift apart."""
    example = Path(__file__).resolve().parents[1] / "examples" / "support_agent" / "cassettes"
    for f in sorted((SAMPLE / "cassettes").glob("*.replex.json")):
        if not f.name.startswith("."):
            assert f.read_bytes() == (example / f.name).read_bytes(), f.name


ACT = {"x-replex-action": "1"}


def test_accept_promotes_the_latest_run(client: TestClient, dirs: tuple[Path, Path]) -> None:
    baseline, candidate = dirs[0] / "order.replex.json", dirs[1] / "order.replex.json"
    latest = candidate.read_bytes()
    r = client.post("/api/w/project/scenarios/order/accept", headers=ACT)
    assert r.status_code == 200 and r.json()["accepted"] == "order"
    assert baseline.read_bytes() == latest and not candidate.exists()
    # Nothing left to accept, and the scenario reads as recorded again.
    assert client.post("/api/w/project/scenarios/order/accept", headers=ACT).status_code == 404
    order = next(
        r for r in client.get("/api/w/project/runs").json()["runs"] if r["name"] == "order"
    )
    assert order["drift"] is None


def test_discard_keeps_the_baseline(client: TestClient, dirs: tuple[Path, Path]) -> None:
    baseline = (dirs[0] / "order.replex.json").read_bytes()
    r = client.post("/api/w/project/scenarios/order/discard", headers=ACT)
    assert r.status_code == 200
    assert not (dirs[1] / "order.replex.json").exists()
    assert (dirs[0] / "order.replex.json").read_bytes() == baseline


@pytest.mark.parametrize(
    ("url", "headers", "status"),
    [
        ("/api/w/project/scenarios/order/accept", {}, 403),  # no action header: cross-site guard
        ("/api/w/sample/scenarios/refund_request/accept", ACT, 403),  # the sample is read-only
        ("/api/w/sample/scenarios/refund_request/discard", ACT, 403),
        ("/api/w/project/scenarios/solo/discard", ACT, 404),  # no candidate to discard
        ("/api/w/project/scenarios/a%20b/accept", ACT, 400),  # not a cassette name
        ("/api/w/elsewhere/scenarios/order/accept", ACT, 404),
    ],
)
def test_write_guards(
    client: TestClient, dirs: tuple[Path, Path], url: str, headers: dict[str, str], status: int
) -> None:
    assert client.post(url, headers=headers).status_code == status
    assert (dirs[1] / "order.replex.json").exists()  # nothing was touched
    assert (SAMPLE / "candidates" / "refund_request.replex.json").exists()


def test_writes_are_refused_in_ci(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    r = client.post("/api/w/project/scenarios/order/accept", headers=ACT)
    assert r.status_code == 409 and "CI" in r.json()["error"]


def test_a_workspace_is_required() -> None:
    with pytest.raises(ValueError, match="at least one workspace"):
        create_app([])
    assert Workspace("x", "X", Path("a"), Path("b")).describe()["baselines"] == 0


def test_run_compare_and_drift(client: TestClient) -> None:
    run_ = client.get("/api/w/project/runs/baseline/order").json()
    assert run_["trace"]["final_output"] == "Order 7 is fine."
    assert run_["meta"]["provider"] == "mock"
    assert "jane@example.com" not in json.dumps(run_)  # the console only sees redacted data
    cmp = client.get(
        "/api/w/project/compare", params={"a": "baseline:order", "b": "candidate:order"}
    ).json()
    assert cmp["diff"]["entries"][-1]["status"] == "changed"
    assert cmp["report"]["name"] == "order"
    drift = client.get("/api/w/project/drift/order").json()
    assert drift["candidate"]["final_output"] == "Order 7 shipped."
    assert {f["check"] for f in drift["report"]["findings"]} >= {"latency"}


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("/api/w/project/runs/baseline/missing", 404),
        ("/api/w/project/runs/other/order", 404),
        ("/api/w/project/runs/baseline/..%2F..%2Fetc", 404),
        ("/api/w/project/runs/baseline/a%20b", 400),
        ("/api/w/project/runs/baseline/...", 400),
        ("/api/nothing", 404),
        ("/api/w/project/compare?a=order&b=candidate:order", 400),
        ("/api/w/project/drift/solo", 404),
        ("/api/w/elsewhere/runs", 404),
    ],
)
def test_errors(client: TestClient, url: str, status: int) -> None:
    r = client.get(url)
    assert r.status_code == status
    assert "error" in r.json()


def test_invalid_cassette_is_reported(client: TestClient, dirs: tuple[Path, Path]) -> None:
    (dirs[0] / "broken.replex.json").write_text("{}")
    rows = client.get("/api/w/project/runs").json()["runs"]
    assert any(r["name"] == "broken" and "error" in r for r in rows)
    assert client.get("/api/w/project/runs/baseline/broken").status_code == 422


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
    c = TestClient(app_for(dirs, pol), base_url=LOCAL)
    assert c.get("/api/w/project/drift/order").json()["report"]["passed"] is False
    assert c.get("/api/w/project/runs").json()["runs"][0]["drift"] == "failed"


def test_policy_edits_apply_without_restart(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    pol = tmp_path / "policy.yaml"
    pol.write_text("version: 1\ndefaults:\n  final_output: {mode: exact, severity: fail}\n")
    c = TestClient(app_for(dirs, pol), base_url=LOCAL)
    assert c.get("/api/w/project/drift/order").json()["report"]["passed"] is False
    pol.write_text("version: 1\ndefaults:\n  final_output: {mode: ignore}\n")
    os.utime(pol, (time.time() + 5, time.time() + 5))  # coarse mtime clocks
    assert c.get("/api/w/project/drift/order").json()["report"]["passed"] is True


@pytest.mark.parametrize("host", ["evil.example.com", "testserver"])
def test_foreign_host_headers_are_refused(dirs: tuple[Path, Path], host: str) -> None:
    """DNS rebinding: a page on another origin resolving to 127.0.0.1 gets nothing."""
    c = TestClient(app_for(dirs), base_url=f"http://{host}")
    assert c.get("/api/w/project/runs").status_code == 400
    assert (
        TestClient(app_for(dirs), base_url="http://localhost:8765")
        .get("/api/w/project/runs")
        .status_code
        == 200
    )


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
