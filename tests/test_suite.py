"""Suite files and `replex run`: parsing, errors, every mode, expectations and the CLI."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from replex.cli import cli
from replex.config import Config
from replex.errors import RecordingRefused
from replex.suite import (
    ScenarioResult,
    Suite,
    SuiteError,
    load_suite,
    parse_suite,
    run_suite,
    suite_config,
)
from replex.suite.runner import call_agent, import_agent

SUITE = """\
agent: suite_agents:agent
expect:
  no_tools: delete_*
  max_steps: 6
scenarios:
  - name: refund
    input: refund 1042
    expect:
      tools: [get_order, check_refund_policy]
      max_tokens: 100000
      final_output:
        contains: refund_requested
        matches: '"order_id": "\\d+"'
        json_schema: {type: object, required: [decision]}
  - name: structured
    input: {order: 1042, lang: en}
    kwargs: {script: v1}
    expect:
      tools: {sequence: [get_order], mode: subsequence}
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with a pyproject, the suite above, and sys.path restored afterwards."""
    (tmp_path / "pyproject.toml").write_text('[tool.replex]\nintercept_hosts = ["127.0.0.1"]\n')
    (tmp_path / "replex.suite.yaml").write_text(SUITE)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return tmp_path


def suite_file(project: Path, text: str, name: str = "replex.suite.yaml") -> Suite:
    path = project / name
    path.write_text(textwrap.dedent(text))
    return load_suite(path)


def run(project: Path, mode: str, suite: Suite | None = None, **kw: Any) -> list[ScenarioResult]:
    suite = suite or load_suite(project / "replex.suite.yaml")
    return run_suite(suite, suite_config(suite, mode), **kw)


def by_name(results: list[ScenarioResult]) -> dict[str, ScenarioResult]:
    return {r.name: r for r in results}


# ------------------------------------------------------------------ parsing


def test_parse_suite(tmp_path: Path) -> None:
    (tmp_path / "schema.json").write_text('{"type": "object"}')
    text = SUITE.replace(
        "json_schema: {type: object, required: [decision]}", "json_schema: schema.json"
    )
    suite = parse_suite(text, path=tmp_path / "s.yaml")
    assert suite.path == tmp_path / "s.yaml"
    refund, structured = suite.scenarios
    assert structured.input == {"order": 1042, "lang": "en"}
    assert suite.agent_kwargs(structured) == {"script": "v1"}
    expect = suite.expectations(refund)
    assert expect.no_tools == ["delete_*"] and expect.max_steps == 6
    assert expect.tools is not None and expect.tools.mode == "exact"
    assert expect.final_output is not None and expect.final_output.json_schema == {"type": "object"}
    other = suite.expectations(structured)
    assert other.tools is not None and other.tools.mode == "subsequence"
    assert other.final_output is None and other.max_steps == 6


def test_suite_kwargs_are_overlaid_by_the_scenario() -> None:
    suite = parse_suite(
        "agent: m:f\nkwargs: {a: 1, b: 1}\nscenarios: [{name: x, input: q, kwargs: {b: 2}}]\n"
    )
    assert suite.agent_kwargs(suite.scenarios[0]) == {"a": 1, "b": 2}


def test_select() -> None:
    suite = parse_suite(SUITE)
    assert [s.name for s in suite.select([])] == ["refund", "structured"]
    assert [s.name for s in suite.select(["structured"])] == ["structured"]
    with pytest.raises(
        SuiteError, match=r"no scenario named 'nope'; the suite has refund, structured"
    ):
        suite.select(["nope"])


BASE = "agent: m:f\nscenarios:\n  - name: x\n    input: q\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("agent: [m:f\n", "s.yaml:2: invalid YAML"),
        ("- a\n- b\n", "s.yaml: a suite must be a mapping"),
        ("", "s.yaml: a suite must be a mapping"),
        ("agent: m:f\n", "s.yaml:1: scenarios: Field required"),
        ("agent: m:f\nscenarios: []\n", "s.yaml:2: scenarios: List should have at least 1 item"),
        (
            "agent: nope\nscenarios: [{name: x, input: q}]\n",
            "s.yaml:1: agent: Value error, expected 'package.module:function'",
        ),
        ("version: 2\n" + BASE, "s.yaml:1: version: Input should be 1"),
        (
            BASE + "    expect:\n      max_steps: 0\n",
            "s.yaml:6: scenarios[0].expect.max_steps: Input should be greater than 0",
        ),
        (BASE + "    inptu: q\n", "s.yaml:5: scenarios[0].inptu: Extra inputs are not permitted"),
        (BASE.replace("    input: q\n", ""), "s.yaml:3: scenarios[0].input: Field required"),
        (
            BASE + "  - name: x\n    input: r\n",
            "s.yaml:3: scenarios: Value error, scenario name 'x' is used twice",
        ),
        (
            BASE.replace("name: x", "name: a b"),
            "s.yaml:3: scenarios[0].name: Value error, use letters",
        ),
        (
            BASE + "    expect: {tools: {sequence: [a], mode: fuzzy}}\n",
            "s.yaml:5: scenarios[0].expect.tools.mode: Input should be",
        ),
        (
            BASE + "    expect: {final_output: {matches: '('}}\n",
            "scenarios[0].expect.final_output.matches: Value error, invalid regular expression",
        ),
        (
            BASE + "    expect: {final_output: {}}\n",
            "scenarios[0].expect.final_output: Value error, set at least one of",
        ),
        (
            BASE + "    expect: {final_output: {json_schema: missing.json}}\n",
            "scenarios[0].expect.final_output.json_schema: Value error, cannot load JSON schema",
        ),
        (BASE + "    expect: {final_output: {json_schema: list.json}}\n", "must be an object"),
        (
            "expect: {no_tools: [1]}\n" + BASE,
            "s.yaml:1: expect.no_tools[0]: Input should be a valid string",
        ),
    ],
)
def test_parse_errors(tmp_path: Path, text: str, expected: str) -> None:
    (tmp_path / "list.json").write_text("[1]")
    with pytest.raises(SuiteError) as info:
        parse_suite(text, path=tmp_path / "s.yaml")
    assert expected in str(info.value).replace(str(tmp_path) + "/", "")


def test_load_suite_errors(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="suite file not found"):
        load_suite(tmp_path / "missing.yaml")
    with pytest.raises(SuiteError, match="cannot read suite file"):
        load_suite(tmp_path)


# ------------------------------------------------------------------ agents


def test_import_agent(project: Path) -> None:
    import suite_agents

    assert import_agent("suite_agents:agent", project) is suite_agents.agent
    assert str(project.resolve()) in sys.path
    (project / "local_agent_module.py").write_text("def run(question):\n    return question\n")
    assert import_agent("local_agent_module:run", project)("hi") == "hi"
    with pytest.raises(SuiteError, match="cannot import agent 'no_such_module:f'"):
        import_agent("no_such_module:f", project)
    with pytest.raises(SuiteError, match="no attribute 'missing'"):
        import_agent("suite_agents:missing", project)
    with pytest.raises(SuiteError, match="is not callable"):
        import_agent("suite_agents:NOT_CALLABLE", project)


def test_call_agent_sync_and_async() -> None:
    async def double(value: int, by: int = 2) -> int:
        return value * by

    class Later:
        def __await__(self) -> Any:
            return double(3, by=3).__await__()

    assert call_agent(lambda value, by: value * by, 2, {"by": 5}) == 10
    assert call_agent(double, 4, {}) == 8
    assert call_agent(lambda value: Later(), 0, {}) == 9


# ------------------------------------------------------------------ modes


def test_record_then_replay(project: Path) -> None:
    recorded = by_name(run(project, "record"))
    assert {r.status for r in recorded.values()} == {"passed"}
    assert recorded["refund"].outcome == "recorded"
    assert recorded["refund"].written_path == project / "tests/cassettes/refund.replex.json"
    assert recorded["refund"].tools == ["get_order", "check_refund_policy"]

    replayed = by_name(run(project, "replay"))
    assert {r.status for r in replayed.values()} == {"passed"}
    assert replayed["refund"].outcome == "replayed" and replayed["refund"].steps == 5
    assert replayed["structured"].problems == []


def test_record_refuses_to_overwrite_without_the_flag(project: Path) -> None:
    run(project, "record")
    [result] = run(project, "record", only=["refund"])
    assert result.status == "error" and result.error is not None
    assert result.error.startswith("CassetteExists:")
    [result] = run(project, "record", only=["refund"], overwrite=True)
    assert result.status == "passed"


def test_replay_without_a_cassette_is_an_error(project: Path) -> None:
    [result] = run(project, "replay", only=["refund"])
    assert result.status == "error" and result.outcome is None
    assert result.error is not None and "CassetteNotFound" in result.error


def test_failing_expectations(project: Path) -> None:
    run(project, "record", only=["refund"])
    suite = suite_file(
        project,
        """\
        agent: suite_agents:agent
        scenarios:
          - name: refund
            input: refund 1042
            expect:
              tools: {sequence: [get_order], mode: same_set}
              no_tools: [check_*]
              max_steps: 2
              max_tokens: 1
              final_output: {contains: approved, matches: '^Sorry', json_schema: {type: array}}
        """,
        "failing.yaml",
    )
    [result] = run(project, "replay", suite)
    assert result.status == "failed" and result.outcome == "replayed" and result.error is None
    assert [f.splitlines()[0] for f in result.failures] == [
        "tool sequence (same_set) does not match",
        "expected no tool matching 'check_*', but found ['check_refund_policy']",
        "run took 5 steps, more than the maximum of 2",
        result.failures[3].splitlines()[0],
        "final output does not contain 'approved'",
    ]
    assert result.failures[3].startswith("run used ")
    assert result.problems == result.failures


@pytest.mark.parametrize(
    ("final_output", "message"),
    [
        ("{matches: '^Sorry'}", "final output does not match /^Sorry/"),
        ("{json_schema: {type: array}}", "final output does not match the JSON schema"),
    ],
)
def test_final_output_expectations(project: Path, final_output: str, message: str) -> None:
    run(project, "record", only=["refund"])
    text = f"agent: suite_agents:agent\nscenarios:\n  - {{name: refund, input: refund 1042, expect: {{final_output: {final_output}}}}}\n"
    [result] = run(project, "replay", suite_file(project, text, "one.yaml"))
    assert result.status == "failed" and result.failures[0].startswith(message)


def test_drift(project: Path) -> None:
    run(project, "record")
    passed = by_name(run(project, "drift"))
    assert passed["refund"].status == "passed" and passed["refund"].outcome == "drift_passed"
    assert passed["refund"].written_path == project / ".replex/candidates/refund.replex.json"

    v2 = SUITE.replace(
        "agent: suite_agents:agent", "agent: suite_agents:agent\nkwargs: {script: v2}"
    )
    v2 = v2.replace("kwargs: {script: v1}", "kwargs: {}")
    results = by_name(run(project, "drift", suite_file(project, v2, "v2.yaml")))
    refund = results["refund"]
    assert refund.status == "failed" and refund.outcome == "drift_failed"
    assert refund.drift_report is not None and not refund.drift_report.passed
    # The tools expectation failed too, and the policy was still applied.
    assert refund.failures[0].startswith("tool sequence (exact) does not match")
    assert "RepleX drift check failed for 'refund'" in refund.problems[-1]
    assert "replex approve refund" in refund.problems[-1]
    assert refund.to_dict()["drift"]["passed"] is False
    assert results["structured"].status == "failed" and not results["structured"].failures


def test_rewrite_keeps_the_baseline_when_expectations_fail(project: Path) -> None:
    run(project, "record", only=["refund"])
    baseline = project / "tests/cassettes/refund.replex.json"
    before = baseline.read_text()
    failing = SUITE.replace("max_steps: 6", "max_steps: 1")
    [result] = run(project, "rewrite", suite_file(project, failing, "strict.yaml"), only=["refund"])
    assert result.status == "failed" and result.outcome == "error"
    assert baseline.read_text() == before
    [result] = run(project, "rewrite", only=["refund"])
    assert result.status == "passed" and result.outcome == "recorded"


def test_record_notes_failed_expectations_in_the_cassette(project: Path) -> None:
    failing = SUITE.replace("max_steps: 6", "max_steps: 1")
    [result] = run(project, "record", suite_file(project, failing, "strict.yaml"), only=["refund"])
    assert result.status == "failed" and result.outcome == "recorded"
    meta = json.loads((project / "tests/cassettes/refund.replex.json").read_text())["meta"]
    assert meta["error"].startswith("AssertionError: run took 5 steps")


def test_agent_errors(project: Path) -> None:
    run(project, "record", only=["refund"])
    text = "agent: suite_agents:{}\nscenarios: [{{name: refund, input: refund 1042}}]\n"
    [crashed] = run(project, "replay", suite_file(project, text.format("crashing_agent"), "c.yaml"))
    assert crashed.status == "error" and crashed.outcome == "error"
    assert crashed.error == "RuntimeError: the agent fell over"
    assert crashed.problems == [crashed.error]

    [hasty] = run(project, "replay", suite_file(project, text.format("hasty_agent"), "h.yaml"))
    assert hasty.status == "error" and hasty.outcome == "replayed"
    assert hasty.error is not None and hasty.error.startswith("UnusedRecording:")


@pytest.mark.parametrize("agent", ["async_agent", "deferred_agent"])
def test_async_agents(project: Path, agent: str) -> None:
    suite = suite_file(
        project, SUITE.replace("suite_agents:agent", f"suite_agents:{agent}"), "a.yaml"
    )
    assert {r.status for r in run(project, "record", suite)} == {"passed"}
    replayed = run(project, "replay", suite)
    assert [(r.status, r.outcome) for r in replayed] == [("passed", "replayed")] * 2


def test_base_exceptions_end_the_session_and_propagate(project: Path) -> None:
    from replex import _runtime

    text = "agent: builtins:exit\nscenarios: [{name: refund, input: 3}]\n"
    with pytest.raises(SystemExit):
        run(project, "record", suite_file(project, text, "exit.yaml"))
    assert _runtime.active_owner() is None


# ------------------------------------------------------------------ configuration


def test_suite_config(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = load_suite(project / "replex.suite.yaml")
    assert suite_config(suite).mode == "replay"
    monkeypatch.setenv("REPLEX_MODE", "drift")
    assert suite_config(suite).mode == "drift"
    assert suite_config(suite, "replay").mode == "replay"
    cfg = suite_config(suite)
    assert isinstance(cfg, Config) and Path(cfg.root) == project.resolve()
    monkeypatch.setenv("REPLEX_MODE", "live")
    with pytest.raises(SuiteError, match="needs a mode that records or replays"):
        suite_config(suite)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(RecordingRefused):
        suite_config(suite, "record")
    monkeypatch.setenv("REPLEX_ALLOW_RECORD_IN_CI", "1")
    assert suite_config(suite, "record").mode == "record"


# ------------------------------------------------------------------ CLI


def invoke(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_cli_run(project: Path) -> None:
    assert "run" in invoke("--help").output
    res = invoke("run", "--mode", "record")
    assert res.exit_code == 0, res.output
    assert "replex run (record)" in res.output and "2 scenario(s): 2 passed" in res.output

    res = invoke("run")
    assert res.exit_code == 0, res.output
    assert "refund" in res.output and "replayed" in res.output

    data = json.loads(invoke("run", "--json", "--only", "structured").output)
    assert data["mode"] == "replay" and data["passed"] is True and data["report"] is None
    assert [s["name"] for s in data["scenarios"]] == ["structured"]
    assert data["scenarios"][0]["outcome"] == "replayed"


def test_cli_run_failures(project: Path) -> None:
    invoke("run", "--mode", "record", "--only", "refund")
    (project / "failing.yaml").write_text(SUITE.replace("max_steps: 6", "max_steps: 1"))
    res = invoke("run", "failing.yaml")
    assert res.exit_code == 1
    assert "FAILED" in res.output and "ERROR" in res.output
    assert "run took 5 steps, more than the maximum of 1" in res.output
    assert "CassetteNotFound" in res.output
    assert "0 passed, 1 failed, 1 error(s)" in res.output
    data = json.loads(invoke("run", "failing.yaml", "--json").output)
    assert data["passed"] is False
    assert [s["status"] for s in data["scenarios"]] == ["failed", "error"]


def test_cli_run_drift_report(project: Path) -> None:
    invoke("run", "--mode", "record")
    res = invoke("run", "--mode", "drift", "--report", "out/report.md")
    assert res.exit_code == 0, res.output
    assert "drift passed" in res.output and "drift report:" in res.output
    assert "refund" in (project / "out/report.md").read_text()

    (project / "v2.yaml").write_text(SUITE.replace("kwargs: {script: v1}", "kwargs: {script: v2}"))
    res = invoke("run", "v2.yaml", "--mode", "drift", "--only", "structured")
    assert res.exit_code == 1
    assert "policy: tool_sequence" in res.output
    data = json.loads(
        invoke("run", "v2.yaml", "--mode", "drift", "--json", "--report", "r.html").output
    )
    assert data["report"] == str((project / "r.html").resolve())
    assert (project / "r.html").read_text().startswith("<!doctype html>")


def test_cli_run_warnings_are_noted(project: Path) -> None:
    (project / "replex.policy.yaml").write_text(
        "version: 1\ndefaults:\n  tool_sequence: {mode: exact, severity: warn}\n"
        "  new_tools: {severity: warn}\n"
    )
    invoke("run", "--mode", "record", "--only", "structured")
    (project / "v2.yaml").write_text(SUITE.replace("kwargs: {script: v1}", "kwargs: {script: v2}"))
    res = invoke("run", "v2.yaml", "--mode", "drift", "--only", "structured")
    assert res.exit_code == 0, res.output
    assert "drift warning(s)" in res.output


def test_cli_run_errors(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = CliRunner().invoke(cli, ["run", "missing.yaml"])
    assert res.exit_code == 1 and "suite file not found: missing.yaml" in res.output
    res = CliRunner().invoke(cli, ["run", "--only", "nope"])
    assert res.exit_code == 1 and "no scenario named 'nope'" in res.output
    monkeypatch.setenv("CI", "1")
    res = CliRunner().invoke(cli, ["run", "--mode", "record"])
    assert res.exit_code == 1 and "refuses mode='record' in CI" in res.output
    assert not (project / "tests/cassettes").exists()
