"""Drift history: the append-only log, its summaries, the CLI and the console endpoints."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import replex
from replex import history
from replex.cli import cli
from replex.config import Config, load_config
from replex.history import CheckEntry, DecisionEntry
from replex.testing import MockProvider, MockReply, MockToolCall

SECRET = "sk-live-do-not-store"


@replex.tool
def lookup(order_id: str) -> dict[str, Any]:
    return {"order_id": order_id}


@replex.tool(safe=True)
def search(query: str) -> list[str]:
    return [query]


def agent(url: str) -> None:
    import httpx

    msgs: list[Any] = [{"role": "user", "content": "order 7"}]
    tools = {"lookup": lookup, "search": search}
    with httpx.Client() as c:
        for _ in range(4):
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
                            "content": json.dumps(tools[u["name"]](**u["input"])),
                        }
                        for u in uses
                    ],
                }
            )


SAME = [MockReply(tool_calls=[MockToolCall("lookup", {"order_id": "7"})]), "Order 7 is fine."]
CHANGED = [
    MockReply(tool_calls=[MockToolCall("search", {"query": SECRET})]),
    f"Something else entirely: {SECRET}",
]


def run_session(cfg: Config, name: str, mode: str, script: list[Any]) -> replex.Session:
    with MockProvider(script) as m, replex.session(name, mode=mode, config=cfg) as s:
        agent(m.url)
    return s


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=str(tmp_path), intercept_hosts=["127.0.0.1"])


def check(name: str = "a", verdict: str = "passed", at: str = "2026-10-01T10:00:00Z") -> CheckEntry:
    return CheckEntry(
        at=at,
        name=name,
        verdict=verdict,  # type: ignore[arg-type]
        model="m",
        baseline_model="m",
        fail={"tool_sequence": 1} if verdict == "failed" else {},
        tools=["lookup"],
        steps=3,
        tokens=10,
        latency_ms=1,
    )


def decision(name: str = "a", kind: str = "accepted", at: str = "2026-10-01T11:00:00Z") -> Any:
    return DecisionEntry(kind=kind, at=at, name=name, via="cli")  # type: ignore[arg-type]


# ---------------------------------------------------------------------- configuration


def test_history_is_on_by_default_and_can_be_switched_off(tmp_path: Path) -> None:
    assert Config(root=str(tmp_path)).history_path == tmp_path / ".replex/history.jsonl"
    for off in (False, "off", "false", "None", ""):
        assert Config(root=str(tmp_path), history=off).history_path is None
    assert Config(history=True).history == ".replex/history.jsonl"
    assert Config(root=str(tmp_path), history="h.jsonl").history_path == tmp_path / "h.jsonl"
    env = {"REPLEX_HISTORY": "off"}
    assert load_config(tmp_path, env=env).history_path is None
    (tmp_path / "pyproject.toml").write_text("[tool.replex]\nhistory = false\n")
    assert load_config(tmp_path, env={}).history_path is None


# ---------------------------------------------------------------------- writing from sessions


def test_drift_checks_are_recorded_without_messages(cfg: Config) -> None:
    run_session(cfg, "order", "record", SAME)
    assert history.read(cfg.history_path) == []  # recording is not a check
    run_session(cfg, "order", "drift", SAME)
    s = run_session(cfg, "order", "drift", CHANGED)
    assert s.outcome == "drift_failed"
    assert cfg.history_path is not None
    text = cfg.history_path.read_text()
    assert SECRET not in text and "message" not in text
    first, second = history.read(cfg.history_path)
    assert isinstance(first, CheckEntry) and isinstance(second, CheckEntry)
    assert first.verdict == "passed" and first.fail == {} and first.tools == ["lookup"]
    assert (first.git_sha, first.branch, first.model) == ("test000", "main", "mock-sonnet")
    assert second.verdict == "failed" and second.tools == ["search"]
    assert second.fail == {"tool_sequence": 1, "new_tools": 1}
    assert second.steps == s.trace.steps_count and second.tokens == s.trace.usage.total


def test_crashed_and_disabled_runs_write_nothing(cfg: Config, tmp_path: Path) -> None:
    run_session(cfg, "order", "record", SAME)
    with pytest.raises(RuntimeError), replex.session("order", mode="drift", config=cfg):
        raise RuntimeError("boom")
    assert cfg.history_path is not None and not cfg.history_path.exists()
    run_session(cfg.merged(history="off"), "order", "drift", SAME)
    assert not cfg.history_path.exists()


def test_git_branch_comes_from_git_or_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLEX_GIT_BRANCH", "")
    assert history.git_branch(tmp_path) is None
    monkeypatch.delenv("REPLEX_GIT_BRANCH")
    assert history.git_branch(tmp_path) is None  # not a repository
    monkeypatch.setenv("PATH", "")
    assert history.git_branch(tmp_path) is None  # no git at all


def test_an_unwritable_history_warns_instead_of_failing(tmp_path: Path) -> None:
    (tmp_path / "h").mkdir()
    with pytest.warns(UserWarning, match="could not write drift history"):
        history.record(tmp_path / "h", check())
    history.record(None, check())


# ---------------------------------------------------------------------- the file


def _append_many(path: str, worker: int) -> None:
    for i in range(50):
        history.append(Path(path), check(name=f"w{worker}-{i}"))


def test_parallel_writers_never_interleave(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(str(path), w)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    lines = path.read_text().splitlines()
    assert len(lines) == 200
    assert len(history.read(path)) == 200
    assert {e.name for e in history.read(path)} == {
        f"w{w}-{i}" for w in range(4) for i in range(50)
    }


def test_the_file_rotates_and_stays_bounded(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "history.jsonl"
    size = len(check().model_dump_json(exclude_none=True)) + 1
    for i in range(25):
        history.append(path, check(name=f"s{i}"), max_bytes=size * 10)
    rotated = history.rotated_path(path)
    assert rotated.is_file() and path.stat().st_size <= size * 10
    assert rotated.stat().st_size <= size * 10
    names = [e.name for e in history.read(path)]
    assert 10 <= len(names) <= 20  # the oldest generation is gone
    assert names == [f"s{i}" for i in range(25 - len(names), 25)]


def test_unreadable_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history.append(path, check())
    with path.open("a") as f:
        f.write('{"v": 2, "kind": "check"}\n')
        f.write('{"v": 1, "kind": "accepted", "at": "yesterday", "name": "a", "via": "cli"}\n')
        f.write('{"truncated": \n')
    history.append(path, decision())
    assert [e.kind for e in history.read(path)] == ["check", "accepted"]
    assert history.read(None) == [] and history.read(tmp_path / "missing") == []


# ---------------------------------------------------------------------- summaries


def test_scenario_history_windows_checks_and_keeps_decisions() -> None:
    entries = [
        check(at="2026-10-01T10:00:00Z"),
        check(verdict="failed", at="2026-10-02T10:00:00Z"),
        decision(kind="rejected", at="2026-10-02T11:00:00Z"),
        check(verdict="failed", at="2026-10-03T10:00:00Z"),
        decision(at="2026-10-03T11:00:00Z"),
        check(at="2026-10-04T10:00:00Z"),
        check(name="b", at="2026-10-04T12:00:00Z"),
    ]
    h = history.scenario_history("a", entries, limit=3)
    assert (h.checks, h.passed, h.failed, h.total_checks) == (3, 1, 2, 4)
    assert h.pass_rate == pytest.approx(1 / 3)
    assert h.last_check_at == "2026-10-04T10:00:00Z"
    assert h.last_change_at == "2026-10-03T10:00:00Z"
    assert h.last_decision is not None and h.last_decision.kind == "accepted"
    assert [e.kind for e in h.events] == ["check", "rejected", "check", "accepted", "check"]
    only_decisions = history.scenario_history("c", [decision(name="c")], limit=5)
    assert only_decisions.pass_rate is None and len(only_decisions.events) == 1


def test_trend_covers_the_week_to_the_last_entry() -> None:
    entries = [
        check(at="2026-09-01T10:00:00Z"),
        check(verdict="failed", at="2026-10-20T10:00:00Z"),
        decision(at="2026-10-20T11:00:00Z"),
        decision(kind="rejected", name="b", at="2026-10-21T09:00:00Z"),
        check(name="b", at="2026-10-21T10:00:00Z"),
    ]
    t = history.trend(entries)
    assert (t.checks, t.passed, t.failed, t.accepted, t.rejected, t.scenarios) == (2, 1, 1, 1, 1, 2)
    assert (t.since, t.until) == ("2026-10-20T10:00:00Z", "2026-10-21T10:00:00Z")
    assert history.trend([]).checks == 0
    data = history.summary(entries, limit=5)
    assert [s.name for s in data.scenarios] == ["b", "a"]
    assert [s.name for s in history.summary(entries, name="a").scenarios] == ["a"]
    assert history.summary(entries, name="zzz").scenarios == []


def test_trend_line_wording() -> None:
    assert history.trend_line(history.Trend()) == "no drift checks yet"
    line = history.trend_line(history.trend([check(), decision()]))
    assert line.startswith("1 check(s) in the 7 days to 2026-10-01 11:00: 1 passed")


# ---------------------------------------------------------------------- pytest and xdist


def test_xdist_workers_share_one_history(pytester: pytest.Pytester) -> None:
    pytest.importorskip("xdist")
    pytester.makepyprojecttoml('[tool.replex]\ncassette_dir = "cassettes"\n')
    pytester.makepyfile(
        test_many="""
import pytest, replex

@replex.tool
def square(n):
    return n * n

@pytest.mark.replex
@pytest.mark.parametrize("n", range(8))
def test_square(n):
    assert square(n) == n * n
"""
    )
    args = ("-n", "2", "-p", "no:cacheprovider")
    pytester.runpytest_subprocess("--replex-mode=record", *args).assert_outcomes(passed=8)
    pytester.runpytest_subprocess("--replex-mode=drift", *args).assert_outcomes(passed=8)
    entries = history.read(Path(pytester.path) / ".replex/history.jsonl")
    assert len(entries) == 8
    assert all(isinstance(e, CheckEntry) and e.verdict == "passed" for e in entries)


# ---------------------------------------------------------------------- CLI


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.replex]\nintercept_hosts = ["127.0.0.1"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "200")
    cfg = load_config(tmp_path)
    run_session(cfg, "order", "record", SAME)
    run_session(cfg, "other", "record", SAME)
    run_session(cfg, "order", "drift", SAME)
    run_session(cfg, "order", "drift", CHANGED)
    run_session(cfg, "other", "drift", SAME)
    return tmp_path


def invoke(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_history_command(project: Path) -> None:
    res = invoke("history")
    assert res.exit_code == 0
    assert "3 check(s) in the 7 days" in res.output
    assert "order" in res.output and "50% of 2" in res.output and "100% of 1" in res.output
    res = invoke("history", "order")
    assert "changed" in res.output and "fail: new_tools, tool_sequence" in res.output
    assert "test000 (main)" in res.output and SECRET not in res.output
    data = json.loads(invoke("history", "--json", "--limit", "1").output)
    assert data["limit"] == 1 and data["trend"]["checks"] == 3
    order = next(s for s in data["scenarios"] if s["name"] == "order")
    assert order["checks"] == 1 and order["failed"] == 1 and order["total_checks"] == 2
    assert "no drift checks recorded for nope" in invoke("history", "nope").output


def test_history_command_without_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert "no drift history at" in invoke("history").output
    monkeypatch.setenv("REPLEX_HISTORY", "off")
    res = CliRunner().invoke(cli, ["history"])
    assert res.exit_code == 1 and "disabled" in res.output


def test_approve_is_recorded(project: Path) -> None:
    assert invoke("approve", "order").exit_code == 0
    last = history.read(project / ".replex/history.jsonl")[-1]
    assert isinstance(last, DecisionEntry)
    assert (last.kind, last.name, last.via, last.git_sha) == ("accepted", "order", "cli", "test000")
    res = invoke("history", "order")
    assert "accepted" in res.output and "via cli" in res.output
    assert "as before" not in res.output
    run_session(load_config(project), "order", "drift", CHANGED)
    run_session(load_config(project), "order", "drift", CHANGED)
    assert "as before" in invoke("history", "order").output
    assert "accepted" in invoke("history").output


# ---------------------------------------------------------------------- console


def test_console_history_endpoints(project: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from replex.console.server import create_app, project_workspace, sample_workspace

    cfg = load_config(project)
    ws = project_workspace(cfg.cassette_path, cfg.candidate_path, None, cfg.history_path)
    off = project_workspace(cfg.cassette_path, cfg.candidate_path)
    client = TestClient(create_app([ws, sample_workspace()]), base_url="http://127.0.0.1:8765")
    data = client.get("/api/w/project/history").json()
    assert data["enabled"] and data["trend"]["checks"] == 3 and len(data["scenarios"]) == 2
    one = client.get("/api/w/project/history", params={"name": "order", "limit": 1}).json()
    assert [s["name"] for s in one["scenarios"]] == ["order"]
    assert one["scenarios"][0]["events"][0]["verdict"] == "failed"
    assert client.get("/api/w/project/history", params={"limit": 0}).status_code == 422
    assert client.get("/api/w/nope/history").status_code == 404

    act = {"x-replex-action": "1"}
    assert client.post("/api/w/project/scenarios/order/discard", headers=act).status_code == 200
    run_session(cfg, "order", "drift", CHANGED)
    assert client.post("/api/w/project/scenarios/order/accept", headers=act).status_code == 200
    kinds = [e.kind for e in history.read(cfg.history_path)]
    assert kinds[-3:] == ["rejected", "check", "accepted"]

    disabled = TestClient(create_app([off]), base_url="http://127.0.0.1:8765")
    assert disabled.get("/api/w/project/history").json()["enabled"] is False


def test_sample_history_comes_from_the_sample_runs() -> None:
    """`make sample` writes the sample history by running drift checks with prompts v1, v2."""
    from replex.console.server import SAMPLE

    entries = history.read(SAMPLE / "history.jsonl")
    names = {p.name.removesuffix(".replex.json") for p in (SAMPLE / "cassettes").glob("[!.]*")}
    assert {e.name for e in entries} == names and len(entries) == 2 * len(names)
    assert all(isinstance(e, CheckEntry) and e.model.startswith("mock-") for e in entries)
    assert all(e.git_sha is None and e.branch is None for e in entries)
    v2 = {e.name: e for e in entries[len(names) :] if isinstance(e, CheckEntry)}
    assert sorted(n for n, e in v2.items() if e.verdict == "failed") == [
        "refund_ineligible_openai",
        "refund_request",
    ]
    for name, entry in v2.items():
        candidate = json.loads((SAMPLE / "candidates" / f"{name}.replex.json").read_text())
        usage = candidate["usage"]
        assert entry.tokens == usage["input_tokens"] + usage["output_tokens"]
    assert not os.path.exists(SAMPLE / "history.jsonl.lock")
