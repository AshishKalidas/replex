"""M7: every CLI command, through click's runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

import replex
from replex.cli import cli
from replex.config import load_config
from replex.testing import MockProvider, MockReply, MockToolCall


@replex.tool
def get_order(order_id: str) -> dict[str, Any]:
    return {"order_id": order_id, "status": "delivered"}


@replex.tool
def check_refund_policy(order_id: str) -> dict[str, Any]:
    return {"eligible": True}


@replex.tool(safe=True)
def search_docs(query: str) -> list[str]:
    return ["Refunds are accepted within 30 days."]


TOOLS = {f.__name__: f for f in (get_order, check_refund_policy, search_docs)}


def agent(url: str) -> str:
    messages: list[Any] = [{"role": "user", "content": "refund 1042"}]
    with httpx.Client() as c:
        for _ in range(6):
            rsp = c.post(
                url + "/v1/messages", json={"model": "mock-sonnet", "messages": messages}
            ).json()
            messages.append({"role": "assistant", "content": rsp["content"]})
            uses = [b for b in rsp["content"] if b["type"] == "tool_use"]
            if not uses:
                return str(rsp["content"][0]["text"])
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": u["id"],
                            "content": json.dumps(TOOLS[u["name"]](**u["input"])),
                        }
                        for u in uses
                    ],
                }
            )
    raise AssertionError


V1 = [
    MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
    MockReply(tool_calls=[MockToolCall("check_refund_policy", {"order_id": "1042"})]),
    "Refund approved for order 1042.",
]
V2 = [
    MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
    MockReply(tool_calls=[MockToolCall("search_docs", {"query": "refund window"})]),
    "Refund approved.",
]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.replex]\nintercept_hosts = ["127.0.0.1"]\n')
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    with MockProvider(V1) as mock, replex.session("refund", mode="record", config=cfg):
        agent(mock.url)
    with MockProvider(V1) as mock, replex.session("same", mode="record", config=cfg):
        agent(mock.url)
    with MockProvider(V2) as mock, replex.session("refund", mode="drift", config=cfg):
        agent(mock.url)
    with MockProvider(V1) as mock, replex.session("same", mode="drift", config=cfg):
        agent(mock.url)
    return tmp_path


def run(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_version_and_help() -> None:
    assert run("--version").output.strip() == f"replex, version {replex.__version__}"
    out = run("--help").output
    for cmd in ("ls", "show", "diff", "drift", "report", "redact", "console", "approve"):
        assert cmd in out


def test_ls(project: Path) -> None:
    res = run("ls")
    assert res.exit_code == 0
    assert "refund" in res.output and "mock-sonnet" in res.output
    data = json.loads(run("ls", "--json").output)
    assert [r["name"] for r in data] == ["refund", "same"]
    assert data[0]["steps"] == 5 and data[0]["tool_calls"] == 2 and data[0]["provider"] == "mock"
    assert len(json.loads(run("ls", ".replex/candidates", "--json").output)) == 2
    (project / "tests/cassettes/broken.replex.json").write_text("{}")
    assert "invalid" in run("ls").output
    assert "no cassettes" in run("ls", "empty_dir").output


def test_show(project: Path) -> None:
    res = run("show", "refund")
    assert res.exit_code == 0
    assert "tool  get_order" in res.output and "Refund approved for order 1042." in res.output
    trace = json.loads(run("show", "tests/cassettes/refund.replex.json", "--json").output)
    assert trace["steps"][1]["name"] == "get_order"
    assert run("show", "refund", "--full").exit_code == 0
    res = CliRunner().invoke(cli, ["show", "nope"])
    assert res.exit_code == 2 and "no cassette file 'nope'" in res.output


def test_diff(project: Path) -> None:
    res = run("diff", "refund", ".replex/candidates/refund.replex.json")
    assert res.exit_code == 0
    assert "search_docs" in res.output and "added" in res.output
    data = json.loads(run("diff", "refund", "refund", "--json").output)
    statuses = [e["status"] for e in data["entries"]]
    assert "removed" in statuses and "added" in statuses


def test_drift_exit_codes(project: Path) -> None:
    res = run("drift", "refund", "refund")
    assert res.exit_code == 1
    assert "FAILED" in res.output and "tool_sequence" in res.output
    report = json.loads(run("drift", "refund", "refund", "--json").output)
    assert report["passed"] is False
    assert {f["check"] for f in report["findings"]} >= {"tool_sequence", "new_tools"}
    assert run("drift", "refund", "refund", "--markdown").output.startswith("### ❌")
    ok = run("drift", "same", "same")
    assert ok.exit_code == 0 and "PASSED" in ok.output
    (project / "lenient.yaml").write_text(
        "version: 1\ndefaults:\n  tool_sequence: {mode: ignore}\n  new_tools: {severity: warn}\n"
        "  tool_args: {mode: ignore}\n"
    )
    assert run("drift", "refund", "refund", "--policy", "lenient.yaml").exit_code == 0


def test_drift_bad_policy(project: Path) -> None:
    (project / "bad.yaml").write_text("version: 7\n")
    res = CliRunner().invoke(cli, ["drift", "same", "same", "--policy", "bad.yaml"])
    assert res.exit_code == 1 and "version" in res.output


def test_report(project: Path) -> None:
    res = run(
        "report",
        "tests/cassettes",
        ".replex/candidates",
        "--html",
        "out/r.html",
        "--markdown",
        "out/r.md",
        "--json",
        "out/r.json",
    )
    assert res.exit_code == 0
    assert "FAILED" in res.output and "passed" in res.output
    assert (project / "out/r.html").read_text().count("refund") > 0
    assert "## ❌ RepleX drift report" in (project / "out/r.md").read_text()
    assert len(json.loads((project / "out/r.json").read_text())) == 2
    assert run("report", "tests/cassettes", ".replex/candidates", "--fail-on-drift").exit_code == 1
    (project / "empty").mkdir()
    res = run("report", "tests/cassettes", "empty", "--fail-on-drift")
    assert res.exit_code == 0 and "no candidate" in res.output
    run("report", "tests/cassettes", ".replex/candidates", "--markdown", "t.md", "--title", "Mine")
    assert (project / "t.md").read_text().startswith("## ❌ Mine\n")


def test_report_compact(project: Path) -> None:
    candidates = project / ".replex/candidates"
    (candidates / "same.replex.json").rename(candidates / "new_one.replex.json")
    res = run(
        "report",
        "tests/cassettes",
        ".replex/candidates",
        "--markdown",
        "c.md",
        "--compact",
        "--title",
        "Agent review",
        "--link",
        "https://ci.example/run/1",
        "--max-chars",
        "5000",
    )
    assert res.exit_code == 0
    md = (project / "c.md").read_text()
    assert md.startswith("## ❌ Agent review: 1 of 1 scenario changed behaviour\n")
    assert "· [full report](https://ci.example/run/1)" in md
    assert "| `refund` | ❌ Behaviour changed:" in md
    assert "| `new_one` | 🆕 New scenario" in md
    assert "1 recorded scenario not run live</summary>\n\n`same`" in md
    (project / "empty").mkdir()
    run("report", "tests/cassettes", "empty", "--markdown", "e.md", "--compact")
    assert "no latest runs to compare" in (project / "e.md").read_text()
    res = CliRunner().invoke(
        cli, ["report", "tests/cassettes", "empty", "--markdown", "e.md", "--link", "x"]
    )
    assert res.exit_code == 2 and "--compact only" in res.output


def test_redact_check(project: Path) -> None:
    res = run("redact", "--check", "tests/cassettes")
    assert res.exit_code == 0 and "nothing unredacted" in res.output
    assert json.loads(run("redact", "--check", "tests/cassettes", "--json").output) == []
    leak = project / "tests/cassettes/leak.replex.json"
    leak.write_text(
        json.dumps(
            {
                "headers": {"x-api-key": "sk-ant-api03-reallyreallysecret"},
                "note": "mail bob@example.com",
            }
        )
    )
    res = run("redact", "--check", "tests/cassettes")
    assert res.exit_code == 1 and "HEADER" in res.output and "EMAIL" in res.output
    assert "reallyreally" not in res.output
    assert run("redact", "--check", str(leak), "--json").exit_code == 1
    res = CliRunner().invoke(cli, ["redact", "tests/cassettes"])
    assert res.exit_code == 2 and "--check" in res.output


def test_approve(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cand = project / ".replex/candidates/refund.replex.json"
    expected = cand.read_text()
    monkeypatch.setenv("CI", "true")
    res = CliRunner().invoke(cli, ["approve", "refund"])
    assert res.exit_code == 1 and "CI" in res.output
    monkeypatch.delenv("CI")
    res = run("approve", "refund")
    assert res.exit_code == 0 and "approved refund" in res.output
    assert (project / "tests/cassettes/refund.replex.json").read_text() == expected
    assert not cand.exists()
    res = CliRunner().invoke(cli, ["approve", "refund"])
    assert res.exit_code == 1 and "no candidate" in res.output
    data = json.loads(run("approve", "same", "--json").output)
    assert data["approved"] == "same"


def test_console_needs_extra_or_serves(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    import replex.console.server as server

    monkeypatch.setattr(server, "serve", lambda **kw: calls.append(kw))
    res = run("console", "--no-open", "--port", "9999")
    assert res.exit_code == 0
    assert calls[0]["port"] == 9999 and calls[0]["host"] == "127.0.0.1"
    project_ws, sample_ws = calls[0]["workspaces"]
    assert (project_ws.id, project_ws.writable) == ("project", True)
    assert project_ws.baseline_dir == project / "tests/cassettes"
    assert project_ws.candidate_dir == project / ".replex/candidates"
    assert (sample_ws.id, sample_ws.writable) == ("sample", False)

    assert run("console", "--no-open", "--sample").exit_code == 0
    assert [w.id for w in calls[1]["workspaces"]] == ["sample"]
    assert run("console", "--no-open", "--no-sample", "other").exit_code == 0
    assert [w.id for w in calls[2]["workspaces"]] == ["project"]
    assert calls[2]["workspaces"][0].baseline_dir == Path("other")


def test_module_entry_point() -> None:
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "replex.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert replex.__version__ in out.stdout
