"""M5: session API and configuration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import replex
from replex.config import Config, check_ci_guard, find_pyproject, in_ci, load_config
from replex.errors import (
    CassetteExists,
    CassetteNotFound,
    ConfigError,
    NoActiveSession,
    RecordingRefused,
    SessionActive,
    UnusedRecording,
)
from replex.testing import MockProvider, MockReply, MockToolCall

EXECUTED: list[str] = []


@replex.tool
def get_order(order_id: str) -> dict[str, Any]:
    EXECUTED.append(order_id)
    return {"order_id": order_id, "status": "shipped", "email": "jane@example.com"}


def agent(base_url: str, question: str = "Where is order 1042?") -> str:
    messages: list[Any] = [{"role": "user", "content": question}]
    with httpx.Client(headers={"x-api-key": "sk-ant-api03-SECRETSECRETSECRET"}) as c:
        for _ in range(4):
            rsp = c.post(
                base_url + "/v1/messages",
                json={"model": "mock-sonnet", "max_tokens": 64, "messages": messages},
            ).json()
            messages.append({"role": "assistant", "content": rsp["content"]})
            uses = [b for b in rsp["content"] if b["type"] == "tool_use"]
            if not uses:
                return "".join(b["text"] for b in rsp["content"] if b["type"] == "text")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": u["id"],
                            "content": json.dumps(get_order(**u["input"])),
                        }
                        for u in uses
                    ],
                }
            )
    raise AssertionError("no answer")


def script(final: str = "Order 1042 has shipped.") -> list[MockReply | str]:
    return [MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]), final]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=str(tmp_path), intercept_hosts=["127.0.0.1"], passthrough_hosts=[])


@pytest.fixture(autouse=True)
def _clear() -> None:
    EXECUTED.clear()


def record(
    cfg: Config, name: str = "where_is_order", final: str = "Order 1042 has shipped.", **kw: Any
) -> replex.Session:
    with (
        MockProvider(script(final)) as mock,
        replex.session(name, mode=kw.pop("mode", "record"), config=cfg, **kw) as s,
    ):
        assert agent(mock.url) == final
    return s


# ------------------------------------------------------------------ record and replay


def test_record_writes_a_labelled_redacted_cassette(cfg: Config) -> None:
    s = record(cfg, tags=["smoke"])
    assert s.outcome == "recorded"
    assert s.written_path == Path(cfg.root) / "tests/cassettes/where_is_order.replex.json"
    text = s.written_path.read_text()
    # Release gate: no secrets on disk.
    assert "SECRETSECRET" not in text and "sk-ant" not in text
    assert "jane@example.com" not in text
    c = replex.load_cassette(s.written_path)
    assert c.meta.provider == "mock"
    assert c.meta.model == "mock-sonnet"
    assert c.meta.git_sha == "test000"
    assert c.meta.tags == ["smoke"]
    assert c.usage.input_tokens > 0
    assert [e.type for e in c.events] == ["exchange", "tool_call", "exchange"]
    assert s.trace.tool_names == ["get_order"]
    assert s.tool_executions == 1


def test_replay_is_offline_deterministic_and_executes_no_tools(cfg: Config) -> None:
    record(cfg)
    EXECUTED.clear()
    traces = []
    for _ in range(2):
        with replex.session("where_is_order", config=cfg) as s:
            assert s.mode == "replay"
            assert agent("http://127.0.0.1:9") == "Order 1042 has shipped."  # nothing listens there
        assert s.outcome == "replayed"
        assert s.tool_executions == 0
        traces.append(s.trace.to_json())
    assert EXECUTED == []
    assert traces[0] == traces[1]  # release gate: byte-identical replays
    assert replex.load_trace(
        Path(cfg.root) / "tests/cassettes/where_is_order.replex.json"
    ).tool_names == ["get_order"]


async def test_async_session(cfg: Config) -> None:
    record(cfg)

    async def async_agent() -> dict[str, Any]:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "http://127.0.0.1:9/v1/messages",
                json={
                    "model": "mock-sonnet",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "Where is order 1042?"}],
                },
            )
            return dict(r.json())

    async with replex.session("where_is_order", config=cfg, strict_unused=False) as s:
        body = await async_agent()
        assert replex.current_session() is s
    assert body["content"][0]["name"] == "get_order"


def test_record_refuses_to_overwrite(cfg: Config) -> None:
    record(cfg)
    with pytest.raises(CassetteExists, match="--replex-overwrite"):
        replex.session("where_is_order", mode="record", config=cfg).start()
    s = record(cfg, final="Shipped!", overwrite=True)
    assert s.outcome == "recorded"
    assert replex.load_trace(s.baseline_path).final_output == "Shipped!"


def test_replay_missing_cassette(cfg: Config) -> None:
    with pytest.raises(CassetteNotFound, match="Record it first"):
        replex.session("nope", config=cfg).start()


def test_rewrite_replaces_baseline_but_not_on_error(cfg: Config) -> None:
    record(cfg)
    s = record(cfg, final="New answer.", mode="rewrite")
    assert replex.load_trace(s.baseline_path).final_output == "New answer."
    before = s.baseline_path.read_text()
    with (
        pytest.raises(RuntimeError),
        MockProvider(script()) as mock,
        replex.session("where_is_order", mode="rewrite", config=cfg) as bad,
    ):
        agent(mock.url)
        raise RuntimeError("agent crashed")
    assert bad.outcome == "error"
    assert s.baseline_path.read_text() == before


def test_record_keeps_failed_runs_for_debugging(cfg: Config) -> None:
    with (
        pytest.raises(ValueError),
        MockProvider(script()) as mock,
        replex.session("bad_run", mode="record", config=cfg) as s,
    ):
        agent(mock.url)
        raise ValueError("wrong answer for jane@example.com")
    meta = replex.load_cassette(s.baseline_path).meta
    assert meta.error == "ValueError: wrong answer for [REDACTED_EMAIL_1]"


def test_unused_recordings_fail_replay(cfg: Config) -> None:
    record(cfg)
    with pytest.raises(UnusedRecording), replex.session("where_is_order", config=cfg):
        httpx.post(
            "http://127.0.0.1:9/v1/messages",
            json={
                "model": "mock-sonnet",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Where is order 1042?"}],
            },
        )


def test_drift_writes_candidate_and_compares(cfg: Config, tmp_path: Path) -> None:
    record(cfg)
    (tmp_path / "replex.policy.yaml").write_text(
        "version: 1\ndefaults:\n  final_output: {mode: exact, severity: fail}\n"
    )
    s = record(cfg, final="It is on its way.", mode="drift")
    assert s.outcome == "drift_failed"
    assert s.written_path == tmp_path / ".replex/candidates/where_is_order.replex.json"
    assert [f.check for f in s.drift_report.failures] == ["final_output"]
    assert s.tool_executions == 0  # get_order replayed from the baseline (tools=auto)
    same = record(cfg, mode="drift")
    assert same.outcome == "drift_passed"


def test_live_mode_does_nothing(cfg: Config) -> None:
    with MockProvider(script()) as mock, replex.session("x", mode="live", config=cfg) as s:
        assert agent(mock.url) == "Order 1042 has shipped."
        with pytest.raises(SessionActive):
            replex.session("y", mode="live", config=cfg).start()
    assert s.outcome == "live"
    assert EXECUTED == ["1042"]
    assert not (Path(cfg.root) / "tests/cassettes").exists()
    with pytest.raises(NoActiveSession):
        _ = s.trace


def test_sessions_do_not_nest(cfg: Config) -> None:
    record(cfg)
    with replex.session("where_is_order", config=cfg, strict_unused=False):
        with pytest.raises(SessionActive):
            replex.session("where_is_order", config=cfg).start()
    with pytest.raises(NoActiveSession):
        replex.current_session()


def test_trace_during_session(cfg: Config) -> None:
    with MockProvider(script()) as mock, replex.session("t", mode="record", config=cfg) as s:
        agent(mock.url)
        assert s.trace.tool_names == ["get_order"]
    record(cfg)
    with replex.session("where_is_order", config=cfg) as r:
        agent("http://127.0.0.1:9")
        assert r.trace.provider == "mock"


def test_unknown_mode(cfg: Config) -> None:
    with pytest.raises(ConfigError, match="unknown mode"):
        replex.session("x", mode="bogus", config=cfg)


def test_explicit_client_without_patching(cfg: Config) -> None:
    with (
        MockProvider(["hi"]) as mock,
        replex.session("explicit", mode="record", config=cfg, patch_httpx=False) as s,
    ):
        with replex.http_client() as c:
            c.post(mock.url + "/v1/messages", json={"model": "m", "messages": []})
        httpx.post(mock.url + "/v1/messages", json={"model": "m", "messages": []})  # not captured
    assert len(replex.load_cassette(s.baseline_path).exchanges) == 1


# ------------------------------------------------------------------ CI guard


@pytest.mark.parametrize(
    ("env", "refused"),
    [
        ({"CI": "true"}, True),
        ({"CI": "1", "REPLEX_ALLOW_RECORD_IN_CI": "1"}, False),
        ({"CI": "false"}, False),
        ({}, False),
    ],
)
def test_ci_guard(env: dict[str, str], refused: bool) -> None:
    for mode in ("record", "rewrite"):
        if refused:
            with pytest.raises(RecordingRefused, match="CI must never silently create baselines"):
                check_ci_guard(mode, env)
        else:
            check_ci_guard(mode, env)
    check_ci_guard("replay", env)
    check_ci_guard("drift", env)
    assert in_ci(env) is (env.get("CI") in ("true", "1"))


def test_session_respects_ci_guard(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(RecordingRefused):
        replex.session("x", mode="record", config=cfg).start()


# ------------------------------------------------------------------ configuration


def test_load_config_precedence(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.replex]\ncassette_dir = "cas"\nmode = "record"\nmatch = "sequence"\n'
        'intercept_hosts = ["api.example.com"]\nredact_patterns = [{name = "order", pattern = "ORD-\\\\d+"}]\n'
    )
    sub = tmp_path / "pkg"
    sub.mkdir()
    cfg = load_config(sub, env={})
    assert cfg.root == str(tmp_path)
    assert cfg.cassette_path == tmp_path / "cas"
    assert (cfg.mode, cfg.match) == ("record", "sequence")
    assert cfg.redact_patterns == [{"name": "order", "pattern": "ORD-\\d+"}]
    env_cfg = load_config(tmp_path, env={"REPLEX_MODE": "DRIFT", "REPLEX_CASSETTE_DIR": "/abs/dir"})
    assert env_cfg.mode == "drift"
    assert env_cfg.cassette_path == Path("/abs/dir")
    opt = load_config(tmp_path, env={"REPLEX_MODE": "drift"}, mode="replay", tools="live")
    assert (opt.mode, opt.tools) == ("replay", "live")
    assert find_pyproject(sub) == tmp_path / "pyproject.toml"


def test_load_config_without_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import replex.config as config_module

    monkeypatch.setattr(config_module, "find_pyproject", lambda start=None: None)
    cfg = load_config(tmp_path, env={})
    assert cfg.root == str(tmp_path.resolve())
    assert cfg.mode == "replay"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[tool.replex]\nmode = "sometimes"\n', "invalid RepleX configuration"),
        ("[tool.replex]\nunknown_key = 1\n", "unknown_key"),
        ("[tool.replex\n", "cannot read"),
        ("[tool]\nreplex = 3\n", "must be a table"),
    ],
)
def test_bad_config(tmp_path: Path, text: str, message: str) -> None:
    (tmp_path / "pyproject.toml").write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path, env={})


def test_bad_env_value(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    with pytest.raises(ConfigError, match="options"):
        load_config(tmp_path, env={"REPLEX_MATCH": "fuzzy"})


def test_session_kwargs_override_config(cfg: Config) -> None:
    s = replex.session(
        "x",
        config=cfg,
        cassette_dir="elsewhere",
        match="sequence",
        tools="live",
        policy="p.yaml",
        intercept_hosts=["h"],
        passthrough_hosts=["p"],
    )
    assert s.config.cassette_dir == "elsewhere"
    assert s.config.match == "sequence"
    assert s.config.tools == "live"
    assert s.config.policy_path == Path(cfg.root) / "p.yaml"
    assert repr(s) == "<replex.Session 'x' mode=replay outcome=None>"


def test_asyncio_run_inside_session(cfg: Config) -> None:
    @replex.tool
    async def ping(n: int) -> int:
        return n

    with replex.session("async_tools", mode="record", config=cfg) as s:
        assert asyncio.run(ping(3)) == 3
    with replex.session("async_tools", config=cfg) as r:
        assert asyncio.run(ping(3)) == 3
    assert s.trace.tool_names == r.trace.tool_names == ["ping"]
