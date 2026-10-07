"""M5: the pytest plugin, exercised end to end with pytester."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

AGENT_TEST = """
import json, os
import httpx, pytest, replex
from replex.testing import MockProvider, MockReply, MockToolCall

RAN = []

@replex.tool
def get_order(order_id):
    RAN.append(order_id)
    return {"order_id": order_id, "status": "shipped"}

@replex.tool(safe=True)  # a fake: safe to run live in drift
def delete_order(order_id):
    return {"deleted": order_id}

def script():
    if os.environ.get("AGENT_VARIANT") == "v2":
        return [MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
                MockReply(tool_calls=[MockToolCall("delete_order", {"order_id": "1042"})]),
                "Deleted instead."]
    return [MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
            "Order 1042 has shipped."]

TOOLS = {"get_order": get_order, "delete_order": delete_order}

def run_agent(base):
    messages = [{"role": "user", "content": "Where is 1042?"}]
    with httpx.Client() as c:
        for _ in range(5):
            rsp = c.post(base + "/v1/messages", json={"model": "mock-sonnet", "messages": messages}).json()
            messages.append({"role": "assistant", "content": rsp["content"]})
            uses = [b for b in rsp["content"] if b["type"] == "tool_use"]
            if not uses:
                return rsp["content"][0]["text"]
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": u["id"],
                 "content": json.dumps(TOOLS[u["name"]](**u["input"]))} for u in uses]})

@pytest.fixture
def base_url():
    with MockProvider(script()) as mock:
        yield mock.url

@pytest.mark.replex(cassette="where_is_order")
def test_where_is_order(replex_session, base_url):
    answer = run_agent(base_url)
    assert "1042" in answer or "Deleted" in answer
    assert replex_session.trace.tool_names[0] == "get_order"
    if replex_session.mode == "replay":
        assert RAN == []

def test_untouched(base_url):
    # no marker, no fixture: real network to the mock, nothing recorded
    assert run_agent(base_url).endswith(".")
"""

PYTEST_INI = """
[tool.pytest.ini_options]
asyncio_default_fixture_loop_scope = "function"
"""

PYPROJECT = (
    PYTEST_INI
    + """
[tool.replex]
cassette_dir = "cassettes"
intercept_hosts = ["127.0.0.1"]
passthrough_hosts = []
"""
)


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyprojecttoml(PYPROJECT)
    pytester.makepyfile(test_agent=AGENT_TEST)
    return pytester


def cassette(p: pytest.Pytester, name: str = "where_is_order", sub: str = "cassettes") -> Path:
    return Path(p.path) / sub / f"{name}.replex.json"


def test_record_then_replay(project: pytest.Pytester) -> None:
    res = project.runpytest("--replex-mode=record")
    res.assert_outcomes(passed=2)
    res.stdout.fnmatch_lines(["*RepleX*", "*mode: record | replayed: 0 | recorded: 1*"])
    assert cassette(project).is_file()
    data = json.loads(cassette(project).read_text())
    assert data["meta"]["provider"] == "mock"

    res = project.runpytest()  # default mode: replay
    res.assert_outcomes(passed=2)
    res.stdout.fnmatch_lines(["*mode: replay | replayed: 1 | recorded: 0*"])


def test_replay_missing_cassette(project: pytest.Pytester) -> None:
    res = project.runpytest("-k", "where_is_order")
    res.assert_outcomes(errors=1)
    res.stdout.fnmatch_lines(["*CassetteNotFound*", "*Record it first*"])


def test_record_does_not_overwrite_without_flag(project: pytest.Pytester) -> None:
    project.runpytest("--replex-mode=record").assert_outcomes(passed=2)
    res = project.runpytest("--replex-mode=record", "-k", "where_is_order")
    res.assert_outcomes(errors=1)
    res.stdout.fnmatch_lines(["*CassetteExists*"])
    project.runpytest("--replex-mode=record", "--replex-overwrite").assert_outcomes(passed=2)


def test_drift_fails_on_policy_violation_and_writes_report(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.runpytest("--replex-mode=record").assert_outcomes(passed=2)
    monkeypatch.setenv("AGENT_VARIANT", "v2")
    (Path(project.path) / "replex.policy.yaml").write_text(
        "version: 1\ndefaults:\n  forbidden_tools: {patterns: ['delete_*'], severity: fail}\n"
    )
    res = project.runpytest(
        "--replex-mode=drift", "--replex-report=out/report.html", "-k", "where_is_order"
    )
    res.assert_outcomes(failed=1)
    res.stdout.fnmatch_lines(
        [
            "*DriftDetected*",
            "*[[]forbidden_tools[]] forbidden tool 'delete_order' was called*",
            "*replex approve where_is_order*",
            "*drift passed: 0 | drift failed: 1*",
            "*drift report: *report.html",
        ]
    )
    assert cassette(project, sub=".replex/candidates").is_file()
    html = (Path(project.path) / "out/report.html").read_text()
    assert "where_is_order" in html


def test_drift_passes_and_reports_markdown(project: pytest.Pytester) -> None:
    project.runpytest("--replex-mode=record").assert_outcomes(passed=2)
    res = project.runpytest(
        "--replex-mode=drift", "--replex-report=report.md", "-k", "where_is_order", "-rA"
    )
    res.assert_outcomes(passed=1)
    res.stdout.fnmatch_lines(["*drift passed: 1 | drift failed: 0*"])
    assert "RepleX drift report" in (Path(project.path) / "report.md").read_text()
    res = project.runpytest(
        "--replex-mode=drift", "--replex-report=report.json", "-k", "where_is_order"
    )
    assert json.loads((Path(project.path) / "report.json").read_text())[0]["passed"] is True


def test_rewrite_approves_new_behaviour(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.runpytest("--replex-mode=record").assert_outcomes(passed=2)
    monkeypatch.setenv("AGENT_VARIANT", "v2")
    project.runpytest("--replex-mode=rewrite").assert_outcomes(passed=2)
    data = json.loads(cassette(project).read_text())
    assert [e.get("name") for e in data["events"] if e["type"] == "tool_call"] == [
        "get_order",
        "delete_order",
    ]
    project.runpytest().assert_outcomes(passed=2)


def test_live_mode(project: pytest.Pytester) -> None:
    res = project.runpytest("--replex-mode=live")
    # The marked test asks for replex_session.trace, which live mode does not provide.
    res.assert_outcomes(passed=1, failed=1)
    res.stdout.fnmatch_lines(["*NoActiveSession*"])
    assert not cassette(project).exists()


@pytest.mark.parametrize("mode", ["record", "rewrite"])
def test_ci_guard(project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("CI", "true")
    res = project.runpytest(f"--replex-mode={mode}")
    assert res.ret == pytest.ExitCode.USAGE_ERROR
    res.stderr.fnmatch_lines(["*CI must never silently create baselines*"])
    monkeypatch.setenv("REPLEX_ALLOW_RECORD_IN_CI", "1")
    project.runpytest(f"--replex-mode={mode}").assert_outcomes(passed=2)


def test_env_mode_and_bad_config(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyprojecttoml(PYTEST_INI + '[tool.replex]\nmatch = "fuzzy"\n')
    pytester.makepyfile("def test_x():\n    pass\n")
    res = pytester.runpytest()
    assert res.ret == pytest.ExitCode.USAGE_ERROR
    res.stderr.fnmatch_lines(["*invalid RepleX configuration*"])


def test_marker_defaults_and_fixture_without_marker(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(PYTEST_INI + '[tool.replex]\ncassette_dir = "cas"\n')
    pytester.makepyfile(
        test_names="""
import pytest, replex

@replex.tool
def add(a, b):
    return a + b

@pytest.mark.replex
def test_marker_only():
    assert add(1, 2) == 3

def test_fixture_only(replex_session):
    assert add(2, 2) == 4
    assert replex_session.trace.tool_names == ["add"]

class TestGroup:
    @pytest.mark.replex(match="sequence", tools="live")
    def test_in_class(self, replex_session):
        assert replex_session.config.match == "sequence"
        assert add(0, 1) == 1

@pytest.mark.replex(bogus=1)
def test_bad_marker():
    pass
"""
    )
    res = pytester.runpytest("--replex-mode=record")
    res.assert_outcomes(passed=3, errors=1)
    res.stdout.fnmatch_lines(["*unknown replex marker argument*bogus*"])
    names = sorted(p.name for p in (Path(pytester.path) / "cas").iterdir())
    assert names == [
        "TestGroup__test_in_class.replex.json",
        "test_fixture_only.replex.json",
        "test_marker_only.replex.json",
    ]
    pytester.runpytest("-k", "not bad_marker").assert_outcomes(passed=3)


def test_failing_test_still_finishes_session(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(PYTEST_INI + '[tool.replex]\ncassette_dir = "cas"\n')
    pytester.makepyfile("""
import pytest, replex

@replex.tool
def add(a, b):
    return a + b

@pytest.mark.replex
def test_fails():
    add(1, 1)
    assert False, "agent gave the wrong answer"

def test_next_test_can_start_a_session(replex_session):
    add(1, 1)
""")
    res = pytester.runpytest("--replex-mode=record", "-p", "no:randomly")
    res.assert_outcomes(passed=1, failed=1)
    assert (Path(pytester.path) / "cas" / "test_fails.replex.json").is_file()


def test_xdist_parallel_record(project: pytest.Pytester) -> None:
    pytest.importorskip("xdist")
    project.makepyfile(
        test_more="""
import pytest, replex

@replex.tool
def square(n):
    return n * n

@pytest.mark.replex
@pytest.mark.parametrize("n", range(6))
def test_square(n):
    assert square(n) == n * n
"""
    )
    res = project.runpytest_subprocess("--replex-mode=record", "-n", "2", "-p", "no:cacheprovider")
    res.assert_outcomes(passed=8)
    res.stdout.fnmatch_lines(["*recorded: 7*"])
    files = sorted((Path(project.path) / "cassettes").glob("*.replex.json"))
    assert len(files) == 7
    res = project.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider")
    res.assert_outcomes(passed=8)
    res.stdout.fnmatch_lines(["*replayed: 7*"])
