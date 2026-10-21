"""A drift run that calls a tool the recording never did: blocked, reported, never approvable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import replex
from replex.cassette.storage import cassette_path, load, promote_candidate
from replex.config import Config
from replex.errors import CassetteError, UnrecordedToolCall
from replex.testing import MockProvider, MockReply, MockToolCall

REFUNDS: list[str] = []


@replex.tool
def lookup(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "delivered"}


@replex.tool
def refund(order_id: str) -> dict[str, str]:
    REFUNDS.append(order_id)
    return {"refunded": order_id}


TOOLS = {"lookup": lookup, "refund": refund}


def agent(base_url: str) -> str:
    messages: list[Any] = [{"role": "user", "content": "Refund order 7"}]
    with httpx.Client() as c:
        for _ in range(5):
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
                            "content": json.dumps(TOOLS[u["name"]](**u["input"])),
                        }
                        for u in uses
                    ],
                }
            )
    raise AssertionError("no answer")


CAREFUL: list[MockReply | str] = [
    MockReply(tool_calls=[MockToolCall("lookup", {"order_id": "7"})]),
    "Checked order 7: it was delivered, so no refund.",
]
HASTY: list[MockReply | str] = [
    MockReply(tool_calls=[MockToolCall("lookup", {"order_id": "7"})]),
    MockReply(tool_calls=[MockToolCall("refund", {"order_id": "7"})]),
    "Refunded.",
]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    REFUNDS.clear()
    return Config(root=str(tmp_path), intercept_hosts=["127.0.0.1"], passthrough_hosts=[])


def record(cfg: Config) -> None:
    with MockProvider(CAREFUL) as mock, replex.session("refund_7", mode="record", config=cfg):
        agent(mock.url)


def hasty_drift_run(cfg: Config, **options: Any) -> tuple[replex.Session, UnrecordedToolCall]:
    """Run the hasty agent in drift mode. The blocked call propagates out of the session, as it
    does out of a test, which is how the session learns the run was stopped."""
    session = replex.session("refund_7", mode="drift", config=cfg, **options)
    with pytest.raises(UnrecordedToolCall) as stopped, MockProvider(HASTY) as mock, session:
        agent(mock.url)
    return session, stopped.value


def test_a_blocked_call_is_a_failing_finding_and_never_runs(cfg: Config) -> None:
    record(cfg)
    s, _ = hasty_drift_run(cfg)
    assert REFUNDS == []  # the side effect did not happen
    assert s.outcome == "drift_failed"
    assert s.drift_report is not None
    blocked = [f for f in s.drift_report.failures if f.check == "blocked_tool"]
    assert len(blocked) == 1
    assert "'refund'" in blocked[0].message and blocked[0].after == "refund"
    # Every call that did run matched the recording, so the blocked call is the whole story.
    assert [f.check for f in s.drift_report.failures] == ["blocked_tool"]


def test_the_stopped_run_is_kept_but_cannot_become_the_recording(cfg: Config) -> None:
    record(cfg)
    s, _ = hasty_drift_run(cfg)
    assert s.written_path is not None
    assert "UnrecordedToolCall" in str(getattr(load(s.written_path).meta, "error", ""))
    baseline = cassette_path(cfg.cassette_path, "refund_7").read_bytes()
    with pytest.raises(CassetteError, match="stopped early"):
        promote_candidate(cfg.candidate_path, cfg.cassette_path, "refund_7")
    assert cassette_path(cfg.cassette_path, "refund_7").read_bytes() == baseline
    assert s.written_path.exists()  # still there to review


def test_the_advice_fits_the_mode(cfg: Config) -> None:
    record(cfg)
    _, error = hasty_drift_run(cfg)
    assert "not marked safe=True, so RepleX did not run it" in str(error)
    assert "--replex-mode=rewrite" in str(error)
    assert error.tool == "refund" and error.arguments == {"order_id": "7"}

    # In replay the old advice still applies: nothing would run live.
    with replex.session("refund_7", mode="replay", config=cfg, strict_unused=False):
        with pytest.raises(UnrecordedToolCall) as replay:
            refund("7")
    assert "tools never run live unless allowed" in str(replay.value)


def test_repeated_runs_still_treat_a_blocked_call_as_an_error(cfg: Config) -> None:
    """Runs measured for consistency are not judged one by one."""
    record(cfg)
    s, _ = hasty_drift_run(cfg, run_number=1)
    assert s.outcome == "error" and s.drift_report is None
