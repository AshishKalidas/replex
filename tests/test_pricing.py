"""Prices, the cost of traces, the cost drift check and where cost is shown."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from rich.console import Console
from tracekit import make_trace

import replex
from replex import Pricing, compare
from replex.cassette import Cassette, save
from replex.cli import cli
from replex.config import Config, load_config
from replex.drift import render_report, render_trace
from replex.drift.policy import parse_policy
from replex.errors import ConfigError
from replex.pricing import BUILTIN_PRICES, ModelPrice, canonical_model, format_usd
from replex.providers import build_trace
from replex.testing import MockProvider, MockReply, MockToolCall
from replex.trace.model import ModelStep, Usage

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def exchange(index: int, model: str, usage: dict[str, Any], *, stream: bool = False) -> Any:
    request = {"method": "POST", "url": OPENROUTER, "body": {"model": model}, "match_key": "k"}
    message = {"role": "assistant", "content": "Done."}
    if stream:
        chunks = [
            {"object": "chat.completion.chunk", "model": model, "choices": [{"delta": message}]},
            {"object": "chat.completion.chunk", "model": model, "choices": [], "usage": usage},
        ]
        response: dict[str, Any] = {"status": 200, "stream": [{"data": c} for c in chunks]}
    else:
        body = {
            "object": "chat.completion",
            "model": model,
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": usage,
        }
        response = {"status": 200, "body": body}
    return {"index": index, "type": "exchange", "request": request, "response": response}


def cassette(*events: Any) -> Cassette:
    return Cassette.model_validate(
        {"name": "c", "created_at": "t", "replex_version": "0", "events": list(events)}
    )


def step(model: str, tokens_in: int, tokens_out: int, reported: float | None = None) -> ModelStep:
    return ModelStep(
        index=0,
        model=model,
        usage=Usage(input_tokens=tokens_in, output_tokens=tokens_out),
        reported_cost=reported,
    )


# ------------------------------------------------------------------ prices


@pytest.mark.parametrize(
    ("name", "canonical"),
    [
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("anthropic/claude-sonnet-5.5", "claude-sonnet-5-5"),
        ("openrouter/anthropic/claude-haiku-5.5", "claude-haiku-5-5"),
        ("Claude-Opus-4-5-20251101", "claude-opus-4-5"),
        ("claude-opus-4-5@20251101", "claude-opus-4-5"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("openai/gpt-4.1", "gpt-4-1"),
    ],
)
def test_canonical_model(name: str, canonical: str) -> None:
    assert canonical_model(name) == canonical


def test_builtin_prices_and_matching() -> None:
    pricing = Pricing()
    sonnet = ModelPrice(input=2.0, output=10.0)
    assert pricing.price_for("claude-sonnet-5-5") == sonnet
    assert pricing.price_for("anthropic/claude-sonnet-5.5") == sonnet
    assert pricing.price_for("anthropic/claude-haiku-5.5") == ModelPrice(input=0.1, output=0.5)
    assert pricing.price_for("gpt-4o-2024-08-06") == BUILTIN_PRICES["gpt-4o"]
    assert pricing.price_for("mock-sonnet") is None
    assert pricing.price_for("anthropic/claude-sonnet-5.5:free") is None
    assert all(canonical_model(k) == k for k in BUILTIN_PRICES)


def test_overrides_take_precedence() -> None:
    pricing = Pricing(
        {
            "claude-sonnet-5-5": {"input": 1.0, "output": 5.0},
            "My.Model": ModelPrice(input=3, output=4),
        }
    )
    assert pricing.price_for("anthropic/claude-sonnet-5.5") == ModelPrice(input=1.0, output=5.0)
    assert pricing.price_for("My.Model") == ModelPrice(input=3, output=4)
    assert pricing.price_for("vendor/my-model") == ModelPrice(input=3, output=4)
    assert Pricing(builtin=False).price_for("claude-sonnet-5-5") is None


def test_step_cost() -> None:
    pricing = Pricing()
    assert pricing.step_cost(step("claude-sonnet-5-5", 1_000_000, 100_000)) == 3.0
    assert pricing.step_cost(step("claude-haiku-5-5", 1000, 200)) == pytest.approx(0.0002)
    assert pricing.step_cost(step("mock-sonnet", 10, 10)) is None
    assert pricing.step_cost(step("mock-sonnet", 10, 10, reported=0.25)) == 0.25
    assert pricing.step_cost(step("claude-sonnet-5-5", 10, 10, reported=0.0)) == 0.0
    assert pricing.step_cost(step("mock-sonnet", 0, 0)) == 0.0


def test_priced_trace_totals_and_unknown_models() -> None:
    trace = make_trace("t", "model>get_order", ("get_order", {}), "model:Done", tokens=1000)
    assert trace.cost is None and trace.unpriced_models == []
    unknown = Pricing().priced(trace)
    assert unknown.cost is None and unknown.unpriced_models == ["mock-sonnet"]
    priced = Pricing({"mock-sonnet": {"input": 1.0, "output": 4.0}}).priced(trace)
    assert [s.cost for s in priced.model_calls] == [0.005, 0.005]
    assert priced.cost == 0.01 and priced.unpriced_models == []
    assert trace.cost is None, "pricing returns a copy"


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (None, "unknown"),
        (0.0, "$0.00"),
        (0.00004, "<$0.0001"),
        (0.0123, "$0.0123"),
        (1234.5, "$1,234.50"),
    ],
)
def test_format_usd(amount: float | None, text: str) -> None:
    assert format_usd(amount) == text


# ------------------------------------------------------------------ configuration


def test_prices_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.replex]\nprices = {"my-model" = {input = 1.0, output = 4.0}}\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.prices == {"my-model": ModelPrice(input=1.0, output=4.0)}
    assert Pricing.from_config(cfg).price_for("my-model") == ModelPrice(input=1.0, output=4.0)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.replex]\nprices = {"my-model" = {input = -1.0, output = 4.0}}\n'
    )
    with pytest.raises(ConfigError, match="prices"):
        load_config(tmp_path)


# ------------------------------------------------------------------ traces


@pytest.mark.parametrize("stream", [False, True])
def test_provider_reported_cost_wins(stream: bool) -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.0042}
    trace = build_trace(cassette(exchange(0, "anthropic/claude-sonnet-5.5", usage, stream=stream)))
    [m] = trace.model_calls
    assert m.reported_cost == 0.0042 and m.cost == 0.0042 and trace.cost == 0.0042


def test_trace_cost_from_tokens_and_unknown_models() -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    priced = build_trace(cassette(exchange(0, "anthropic/claude-sonnet-5.5", usage)))
    assert priced.model_calls[0].reported_cost is None
    assert priced.cost == pytest.approx(0.003)
    for bad in ({**usage, "cost": True}, {**usage, "cost": "0.1"}, {**usage, "cost": -1}):
        assert build_trace(cassette(exchange(0, "x", bad))).model_calls[0].reported_cost is None
    mixed = cassette(
        exchange(0, "anthropic/claude-sonnet-5.5", usage), exchange(1, "acme-large", usage)
    )
    trace = build_trace(mixed)
    assert trace.cost is None and trace.unpriced_models == ["acme-large"]
    own = build_trace(mixed, pricing=Pricing({"acme-large": {"input": 0, "output": 0}}))
    assert own.cost == pytest.approx(0.003)


def test_session_uses_configured_prices(tmp_path: Path) -> None:
    cfg = Config(
        root=str(tmp_path),
        intercept_hosts=["127.0.0.1"],
        prices={"mock-sonnet": ModelPrice(input=1.0, output=4.0)},
    )
    with MockProvider(["Hello."]) as mock, replex.session("s", mode="record", config=cfg) as s:
        with httpx.Client() as c:
            c.post(mock.url + "/v1/messages", json={"model": "mock-sonnet", "messages": []})
    assert s.trace.cost is not None and s.trace.cost > 0


# ------------------------------------------------------------------ the cost check


PRICES = Pricing({"cheap": {"input": 1.0, "output": 1.0}, "dear": {"input": 2.0, "output": 2.0}})


def run(model: str, tokens: int = 500_000) -> Any:
    return PRICES.priced(make_trace("t", "model:Done", model=model, tokens=tokens))


def cost_findings(base: Any, cand: Any, **check: Any) -> list[Any]:
    pol = parse_policy({"version": 1, "defaults": {"cost": check}} if check else {})
    return [f for f in compare(base, cand, pol).findings if f.check == "cost"]


def test_cost_increase_over_the_limit() -> None:
    [f] = cost_findings(run("cheap"), run("dear"))
    assert f.severity == "warn"
    assert f.message == "cost rose from $1.00 to $2.00 (+100%, allowed +25%)"
    assert (f.before, f.after) == (1.0, 2.0)
    [f] = cost_findings(run("cheap"), run("dear"), max_increase_pct=20, severity="fail")
    assert f.severity == "fail"
    assert not compare(
        run("cheap"), run("dear"), parse_policy({"defaults": {"cost": {"severity": "fail"}}})
    ).passed


def test_cost_within_the_limit_is_reported_only() -> None:
    [f] = cost_findings(run("dear"), run("cheap"))
    assert f.severity == "info" and f.message == "cost $2.00 -> $1.00 (-50%) (reported only)"
    [f] = cost_findings(run("cheap"), run("dear"), max_increase_pct=100)
    assert f.severity == "info" and "(+100%)" in f.message
    [f] = cost_findings(run("cheap", tokens=0), run("cheap"))
    assert f.severity == "info" and f.message == "cost $0.00 -> $1.00 (reported only)"
    assert cost_findings(run("cheap"), run("dear"), severity="ignore") == []


def test_cost_unknown_on_one_or_both_sides() -> None:
    [f] = cost_findings(run("cheap"), run("acme"))
    assert f.severity == "info"
    assert f.message == "cost of the candidate is unknown: no price for 'acme' (reported only)"
    [f] = cost_findings(make_trace("t", "model:x"), run("cheap"))
    assert f.message == "cost of the baseline is unknown: not priced (reported only)"
    assert cost_findings(run("acme"), run("acme")) == []


def test_compare_prices_with_the_given_pricing(tmp_path: Path) -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 0}
    base = save(cassette(exchange(0, "acme", usage)), tmp_path / "a.replex.json")
    cand = cassette(exchange(0, "acme", {**usage, "prompt_tokens": 2000}))
    assert compare(base, cand).baseline.cost is None
    report = compare(base, cand, pricing=Pricing({"acme": {"input": 1.0, "output": 1.0}}))
    assert (report.baseline.cost, report.candidate.cost) == (0.001, 0.002)
    assert [f.severity for f in report.findings if f.check == "cost"] == ["warn"]
    assert report.to_dict()["checks"]["cost"] == {"max_increase_pct": 25.0, "severity": "warn"}


def test_reports_show_cost() -> None:
    report = compare(run("cheap"), run("dear"))
    md = report.to_markdown()
    assert "| Cost | $1.00 | $2.00 |" in md
    assert "| ⚠️ warn | `cost` |" in md
    console = Console(record=True, width=160)
    render_report(report, console)
    render_trace(run("cheap"), console)
    render_trace(PRICES.priced(make_trace("t", "model:x", model="acme")), console)
    out = console.export_text()
    assert re.search(r"cost\s+\$1\.00\s+\$2\.00", out)
    assert "500000→500000 tok  $1.00" in out
    assert "1 steps  ·  1000000 tokens  ·  $1.00" in out
    assert "cost unknown (no price for acme)" in out


# ------------------------------------------------------------------ command line


V1 = [MockReply(tool_calls=[MockToolCall("lookup", {"id": "1"})]), "Done."]
V2 = [
    MockReply(tool_calls=[MockToolCall("lookup", {"id": "1"})]),
    MockReply(tool_calls=[MockToolCall("lookup", {"id": "1"})]),
    "Done, after checking twice.",
]


@replex.tool(safe=True)
def lookup(id: str) -> dict[str, str]:
    return {"id": id}


def agent(url: str) -> None:
    messages: list[Any] = [{"role": "user", "content": "look up 1"}]
    with httpx.Client() as c:
        for _ in range(4):
            body = {"model": "mock-sonnet", "messages": messages}
            rsp = c.post(url + "/v1/messages", json=body).json()
            messages.append({"role": "assistant", "content": rsp["content"]})
            uses = [b for b in rsp["content"] if b["type"] == "tool_use"]
            if not uses:
                return
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": u["id"],
                    "content": json.dumps(lookup(**u["input"])),
                }
                for u in uses
            ]
            messages.append({"role": "user", "content": results})


def test_cli_shows_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.replex]\nintercept_hosts = ["127.0.0.1"]\n'
        'prices = {"mock-sonnet" = {input = 1000.0, output = 4000.0}}\n'
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    with MockProvider(V1) as mock, replex.session("order", mode="record", config=cfg):
        agent(mock.url)
    with MockProvider(V2) as mock, replex.session("order", mode="drift", config=cfg) as s:
        agent(mock.url)
    assert [f.severity for f in s.drift_report.findings if f.check == "cost"] == ["warn"]
    runner = CliRunner()
    [row] = json.loads(runner.invoke(cli, ["ls", "--json"]).output)
    assert row["cost"] > 0
    assert "$" in runner.invoke(cli, ["ls"]).output
    assert "$" in runner.invoke(cli, ["show", "order"]).output
    drift = runner.invoke(cli, ["drift", "order", "order", "--json"])
    data = json.loads(drift.output)
    assert data["candidate"]["cost"] > data["baseline"]["cost"]
    assert "| Cost |" in runner.invoke(cli, ["drift", "order", "order", "--markdown"]).output
    assert runner.invoke(cli, ["diff", "order", "order"]).exit_code == 0
    report = runner.invoke(
        cli, ["report", "tests/cassettes", ".replex/candidates", "--json", "r.json"]
    )
    assert report.exit_code == 0
    assert json.loads((tmp_path / "r.json").read_text())[0]["candidate"]["cost"] > 0


def test_console_prices_the_project(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from replex.console.server import create_app, project_workspace

    usage = {"prompt_tokens": 1000, "completion_tokens": 0}
    save(cassette(exchange(0, "acme", usage)), tmp_path / "base" / "c.replex.json")
    save(cassette(exchange(0, "acme", usage)), tmp_path / "cand" / "c.replex.json")
    pricing = Pricing({"acme": {"input": 2.0, "output": 2.0}})
    ws = project_workspace(tmp_path / "base", tmp_path / "cand", pricing=pricing)
    client = TestClient(create_app([ws]), base_url="http://127.0.0.1:8765")
    runs = client.get("/api/w/project/runs").json()["runs"]
    assert [r["cost"] for r in runs] == [0.002, 0.002]
    drift = client.get("/api/w/project/drift/c").json()
    assert drift["report"]["candidate"]["cost"] == 0.002
    assert drift["candidate"]["cost"] == 0.002
    assert drift["candidate"]["steps"][0]["cost"] == 0.002
