"""M6: drift checks (a passing and a failing fixture for every check), policies, reports."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from tracekit import make_trace

from replex import compare, load_policy
from replex.cassette import Cassette, save
from replex.drift import html_report, render_report, render_trace, reports_markdown
from replex.drift.compare import as_trace, similarity
from replex.drift.policy import Policy, parse_policy
from replex.errors import PolicyError

ROOT = Path(__file__).resolve().parents[1]

BASE = make_trace(
    "refund_request",
    "model>get_order",
    ("get_order", {"order_id": "1042", "request_id": "a"}),
    "model>check_refund_policy",
    ("check_refund_policy", {"category": "shoes", "amount": 49.99}),
    "model>create_refund_request",
    ("create_refund_request", {"order_id": "1042", "amount": 49.99}),
    'model:{"refund_id": "R-1", "status": "requested"}',
)


def policy(**checks: Any) -> Policy:
    return parse_policy({"version": 1, "defaults": checks})


def findings(
    cand: Any, pol: Policy | None = None, check: str | None = None, base: Any = BASE
) -> list[Any]:
    report = compare(base, cand, pol or policy())
    return [f for f in report.findings if check is None or f.check == check]


def variant(*spec: Any, **kw: Any) -> Any:
    return make_trace("refund_request", *spec, **kw)


SKIP_CHECK = variant(
    "model>get_order",
    ("get_order", {"order_id": "1042", "request_id": "a"}),
    "model>create_refund_request",
    ("create_refund_request", {"order_id": "1042", "amount": 49.99}),
    'model:{"refund_id": "R-1", "status": "requested"}',
)


# ------------------------------------------------------------------ tool_sequence


def test_identical_runs_pass_every_check() -> None:
    report = compare(BASE, BASE)
    assert report.passed
    assert [f.check for f in report.findings] == ["latency"]
    assert report.findings[0].severity == "info"


@pytest.mark.parametrize(
    ("mode", "cand", "fails"),
    [
        ("exact", BASE, False),
        ("exact", SKIP_CHECK, True),
        ("subsequence", variant(*BASE.steps[:6], ("search_docs", {}), BASE.steps[6]), False),
        ("subsequence", SKIP_CHECK, True),
        (
            "same_set",
            variant(("check_refund_policy", {}), ("get_order", {}), ("create_refund_request", {})),
            False,
        ),
        ("same_set", SKIP_CHECK, True),
        ("ignore", SKIP_CHECK, False),
    ],
)
def test_tool_sequence(mode: str, cand: Any, fails: bool) -> None:
    got = findings(cand, policy(tool_sequence={"mode": mode}), "tool_sequence")
    assert bool(got) is fails
    if fails:
        assert got[0].severity == "fail"
        assert got[0].before == ["get_order", "check_refund_policy", "create_refund_request"]
        assert "tool sequence changed" in got[0].message


def test_tool_sequence_step_index_points_at_divergence() -> None:
    [f] = findings(SKIP_CHECK, check="tool_sequence")
    assert f.step_index == 3  # candidate's create_refund_request step


# ------------------------------------------------------------------ tool_args


def changed_args(order_id: str = "1042", amount: float = 49.99, request_id: str = "b") -> Any:
    return variant(
        "model>get_order",
        ("get_order", {"order_id": order_id, "request_id": request_id}),
        "model>check_refund_policy",
        ("check_refund_policy", {"category": "shoes", "amount": amount}),
        "model>create_refund_request",
        ("create_refund_request", {"order_id": order_id, "amount": amount}),
        'model:{"refund_id": "R-1", "status": "requested"}',
    )


def test_tool_args_fail_and_ignore_keys() -> None:
    got = findings(changed_args(), check="tool_args")
    assert [f.message for f in got] == ["arguments of 'get_order' changed"]
    assert got[0].before == {"order_id": "1042", "request_id": "a"}
    assert got[0].step_index == 1
    assert (
        findings(changed_args(), policy(tool_args={"ignore_keys": ["request_id"]}), "tool_args")
        == []
    )
    per_tool = policy(tool_args={"tools": {"get_order": {"ignore_keys": ["request_id"]}}})
    assert findings(changed_args(), per_tool, "tool_args") == []
    assert findings(changed_args(), policy(tool_args={"mode": "ignore"}), "tool_args") == []


def test_tool_args_numeric_tolerance() -> None:
    near = changed_args(amount=49.995, request_id="a")
    assert len(findings(near, check="tool_args")) == 2
    assert findings(near, policy(tool_args={"tolerance": 0.01}), "tool_args") == []
    per_tool = policy(tool_args={"tools": {"check_refund_policy": {"tolerance": 0.01}}})
    assert [f.message for f in findings(near, per_tool, "tool_args")] == [
        "arguments of 'create_refund_request' changed"
    ]
    far = changed_args(amount=60.0, request_id="a")
    assert len(findings(far, policy(tool_args={"tolerance": 0.01}), "tool_args")) == 2


def test_tool_args_on_model_requests_when_tools_not_decorated() -> None:
    a = make_trace("r", "model>get_order")
    b = make_trace("r", "model>get_order")
    b.steps[0].tool_requests[0].args = {"order_id": "9"}  # type: ignore[union-attr]
    assert [f.check for f in findings(b, base=a) if f.severity == "fail"] == ["tool_args"]


@pytest.mark.parametrize(
    ("x", "y", "same"),
    [
        ({"a": [1, 2.0]}, {"a": [1.0, 2]}, True),
        ({"a": True}, {"a": 1}, False),
        ({"a": [1]}, {"a": [1, 2]}, False),
        ({"a": {"b": 1}}, {"a": {"c": 1}}, False),
    ],
)
def test_numeric_comparison(x: Any, y: Any, same: bool) -> None:
    from replex.drift.compare import _numbers_close

    assert _numbers_close(x, y, 0.0) is same


# ------------------------------------------------------------------ new_tools / forbidden_tools


def test_new_tools() -> None:
    cand = variant(*BASE.steps[:6], ("search_docs", {"q": "refund"}), BASE.steps[6])
    [f] = findings(cand, check="new_tools")
    assert f.after == ["search_docs"] and f.step_index == 6 and f.severity == "fail"
    assert findings(SKIP_CHECK, check="new_tools") == []
    assert findings(cand, policy(new_tools={"severity": "ignore"}), "new_tools") == []
    warn = findings(cand, policy(new_tools={"severity": "warn"}), "new_tools")
    assert warn[0].severity == "warn"


def test_forbidden_tools() -> None:
    pol = policy(forbidden_tools={"patterns": ["delete_*", "send_payment"]})
    cand = variant(("get_order", {}), ("delete_order", {}), ("send_payment", {}))
    got = findings(cand, pol, "forbidden_tools")
    assert [f.message for f in got] == [
        "forbidden tool 'delete_order' was called (matches 'delete_*')",
        "forbidden tool 'send_payment' was called (matches 'send_payment')",
    ]
    assert findings(BASE, pol, "forbidden_tools") == []
    assert findings(cand, policy(), "forbidden_tools") == []  # no patterns configured


# ------------------------------------------------------------------ final_output


def with_output(text: str) -> Any:
    return variant(*BASE.steps[:6], f"model:{text}")


@pytest.mark.parametrize(
    ("mode", "text", "fails"),
    [
        ("exact", '{"refund_id": "R-1", "status": "requested"}', False),
        ("exact", '{"refund_id": "R-1", "status": "approved"}', True),
        ("json_equal", '```json\n{"status": "requested", "refund_id": "R-1"}\n```', False),
        ("json_equal", '{"refund_id": "R-2", "status": "requested"}', True),
        ("json_equal", "not json at all", True),
        ("similarity", '{"refund_id": "R-1", "status": "requested."}', False),
        ("similarity", "Sorry, I cannot help with refunds today.", True),
        ("ignore", "anything", False),
    ],
)
def test_final_output_modes(mode: str, text: str, fails: bool) -> None:
    got = findings(
        with_output(text), policy(final_output={"mode": mode, "severity": "fail"}), "final_output"
    )
    assert bool(got) is fails
    if fails:
        assert got[0].after == text and got[0].step_index == 6


def test_final_output_json_schema(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "required": ["refund_id", "status"],
        "properties": {"status": {"enum": ["requested", "approved"]}},
    }
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "refund.json").write_text(json.dumps(schema))
    (tmp_path / "policy.yaml").write_text(
        "version: 1\noverrides:\n  refund_request:\n"
        "    final_output: {mode: json_schema, schema: schemas/refund.json, severity: fail}\n"
    )
    pol = load_policy(tmp_path / "policy.yaml")
    assert (
        findings(with_output('{"refund_id": "R-9", "status": "approved"}'), pol, "final_output")
        == []
    )
    [bad] = findings(with_output('{"refund_id": "R-9", "status": "lost"}'), pol, "final_output")
    assert "violates the schema" in bad.message and bad.severity == "fail"
    [nojson] = findings(with_output("nope"), pol, "final_output")
    assert "not valid JSON" in nojson.message
    inline = policy(final_output={"mode": "json_schema", "schema": {"type": "array"}})
    assert findings(with_output("[1]"), inline, "final_output") == []
    with pytest.raises(PolicyError, match="needs a 'schema'"):
        compare(BASE, BASE, policy(final_output={"mode": "json_schema"}))
    with pytest.raises(PolicyError, match="cannot load JSON schema"):
        compare(BASE, BASE, policy(final_output={"mode": "json_schema", "schema": "missing.json"}))
    (tmp_path / "list.json").write_text("[]")
    pol2 = parse_policy(
        {"defaults": {"final_output": {"mode": "json_schema", "schema": "list.json"}}},
        base_dir=tmp_path,
    )
    with pytest.raises(PolicyError, match="must be an object"):
        compare(BASE, BASE, pol2)


def test_similarity_threshold_message() -> None:
    [f] = findings(with_output("totally different"), check="final_output")
    assert f.severity == "warn"
    assert "is below 0.80" in f.message
    assert similarity("a", "a") == 1.0
    assert 0 < similarity("abcd", "abce") < 1


def test_embeddings_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    pol = policy(
        final_output={"mode": "similarity", "similarity_backend": "embeddings", "threshold": 0.9}
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(PolicyError, match="semantic"):
        compare(BASE, with_output("x"), pol)

    class FakeModel:
        def __init__(self, name: str) -> None:
            self.name = name

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] if "R-1" in t else [0.0, 1.0] for t in texts]

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    assert findings(with_output('{"refund_id": "R-1"}'), pol, "final_output") == []
    [f] = findings(with_output("other"), pol, "final_output")
    assert "0.00 is below 0.90" in f.message


# ------------------------------------------------------------------ steps / tokens / latency


def test_steps() -> None:
    longer = variant(
        *BASE.steps[:6], ("search_docs", {}), "model>x", ("search_docs", {}), BASE.steps[6]
    )
    [f] = findings(longer, check="steps")
    assert f.message == "step count rose from 7 to 10 (+3, allowed +1)"
    assert (
        findings(variant(*BASE.steps[:6], ("search_docs", {}), BASE.steps[6]), check="steps") == []
    )
    assert findings(longer, policy(steps={"max_increase": 3}), "steps") == []
    assert findings(longer, policy(steps={"severity": "ignore"}), "steps") == []


def test_tokens() -> None:
    hungry = variant(*BASE.steps, tokens=20)
    hungry.usage = hungry.usage.model_copy(update={"input_tokens": 60, "output_tokens": 60})
    [f] = findings(hungry, check="tokens")
    assert f.message == "tokens rose from 80 to 120 (+50%, allowed +25%)"
    assert findings(hungry, policy(tokens={"max_increase_pct": 50}), "tokens") == []
    zero = make_trace("refund_request", ("t", {}))
    assert findings(hungry, base=zero, check="tokens") == []


def test_latency_is_reported_only() -> None:
    slow = variant(*BASE.steps)
    slow.total_latency_ms = BASE.total_latency_ms * 3
    report = compare(BASE, slow)
    [f] = [f for f in report.findings if f.check == "latency"]
    assert f.severity == "info" and "+200%" in f.message
    assert report.passed
    assert findings(slow, policy(latency={"severity": "ignore"}), "latency") == []
    zero = make_trace("refund_request")
    zero.total_latency_ms = 0
    assert "->" in findings(zero, base=zero, check="latency")[0].message


# ------------------------------------------------------------------ policies


def test_repository_example_policy_loads() -> None:
    pol = load_policy(ROOT / "replex.policy.yaml")
    assert pol.defaults.forbidden_tools.patterns == ["delete_*", "send_payment"]
    checks = pol.for_cassette("refund_request")
    assert checks.final_output.mode == "json_schema"
    assert checks.final_output.severity == "fail"
    assert checks.tool_sequence.mode == "exact"  # inherited from defaults
    assert pol.for_cassette("other").final_output.mode == "similarity"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version: 2\n", "version"),
        ("defaults:\n  tool_sequence: {mode: fuzzy}\n", "tool_sequence.mode"),
        ("defaults:\n  not_a_check: {}\n", "not_a_check"),
        ("defaults: [1\n", "invalid YAML"),
        ("- a\n", "must be a mapping"),
        ("overrides:\n  x:\n    steps: {max_increase: -1}\n", "invalid override for 'x'"),
        ("defaults:\n  final_output: {threshold: 2}\n", "threshold"),
    ],
)
def test_policy_validation(tmp_path: Path, text: str, message: str) -> None:
    (tmp_path / "p.yaml").write_text(text)
    with pytest.raises(PolicyError, match=message):
        load_policy(tmp_path / "p.yaml")


def test_policy_defaults_and_missing() -> None:
    assert load_policy(None) == Policy()
    assert parse_policy(None).defaults.tool_sequence.mode == "exact"
    with pytest.raises(PolicyError, match="not found"):
        load_policy("does/not/exist.yaml")


def test_compare_accepts_cassettes_and_paths(tmp_path: Path) -> None:
    c = Cassette(name="refund_request", created_at="t", replex_version="0")
    path = save(c, tmp_path / "c.replex.json")
    assert as_trace(c).name == "refund_request"
    assert compare(path, str(path), tmp_path / "missing.yaml" if False else None).passed
    pol_file = tmp_path / "p.yaml"
    pol_file.write_text("version: 1\n")
    assert compare(c, c, pol_file).passed


# ------------------------------------------------------------------ reports


def failing_report() -> Any:
    pol = policy(forbidden_tools={"patterns": ["delete_*"]})
    cand = variant(*SKIP_CHECK.steps[:4], ("delete_order", {"id": "1|2"}), SKIP_CHECK.steps[4])
    return compare(BASE, cand, pol)


def test_report_json_and_markdown() -> None:
    report = failing_report()
    assert not report.passed
    data = json.loads(report.to_json())
    assert data["passed"] is False
    assert {f["check"] for f in data["findings"]} >= {
        "tool_sequence",
        "forbidden_tools",
        "new_tools",
    }
    md = report.to_markdown()
    assert md.startswith("### ❌ RepleX drift: `refund_request` — 3 failures")
    assert "| ❌ fail | `forbidden_tools` |" in md
    assert "1\\|2" in md  # pipes escaped
    assert "<details><summary>Aligned steps</summary>" in md
    summary = reports_markdown([report, compare(BASE, BASE)])
    assert summary.startswith("## ❌ RepleX drift report")
    assert "2 cassette(s) compared · 1 passed · 1 failed" in summary
    assert "✅ RepleX drift" in summary


def test_terminal_rendering() -> None:
    console = Console(record=True, width=140)
    render_report(failing_report(), console)
    render_report(compare(BASE, BASE), console)
    t = make_trace(
        "x", "model>get_order", ("get_order", {"id": 1}, {"ok": True}), ("boom", {}), "model:done"
    )
    t.steps[2].error = "ValueError: nope"  # type: ignore[union-attr]
    t.steps[0].error = "overloaded"  # type: ignore[union-attr]
    t.notes.append("a note")
    render_trace(t, console)
    render_trace(t, console, full=True)
    out = console.export_text()
    assert "RepleX drift · refund_request · FAILED" in out
    assert "forbidden tool 'delete_order'" in out
    assert "PASSED" in out
    assert "✗ ValueError: nope" in out and "note: a note" in out
    assert "final output: done" in out


def test_html_report_fallback_without_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    import replex.drift.report as report_module

    monkeypatch.setattr(report_module, "_static", lambda name: None)
    html = html_report([failing_report(), compare(BASE, BASE)])
    assert "<table" in html and "forbidden tool" in html and "latency" in html
    assert 'id="replex-data"' in html
