"""Replays cassettes recorded from real models (cassettes/live/), if the owner recorded any.

Recordings are made with `make record-claude` / `make record-mimo`; each cassette stores how
it was produced in its tags, so it can be replayed offline here with no API key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support_agent import AgentConfig, run_agent
from support_agent.scenarios import SCENARIOS_BY_NAME

import replex

LIVE = Path(__file__).resolve().parents[1] / "cassettes" / "live"
FILES = sorted(LIVE.glob("*/*.replex.json"))


@pytest.mark.skipif(
    not FILES, reason="no live cassettes recorded yet (see cassettes/live/README.md)"
)
@pytest.mark.parametrize("path", FILES, ids=[f"{p.parent.name}/{p.stem}" for p in FILES])
def test_live_cassette_replays(path: Path) -> None:
    cassette = replex.load_cassette(path)
    tags = dict(t.split("=", 1) for t in cassette.meta.tags if "=" in t)
    scenario = SCENARIOS_BY_NAME[tags["scenario"]]
    cfg = AgentConfig(
        provider="openai" if tags["wire"] == "openai" else "anthropic",
        base_url=tags["base_url"],
        model=cassette.meta.model,
        prompt_version=tags.get("prompt", "v1"),
        stream=tags.get("stream") == "true",
    )
    with replex.session(
        cassette.name,
        mode="replay",
        cassette_dir=path.parent,
        intercept_hosts=[tags["host"]],
        passthrough_hosts=[],
    ) as s:
        result = run_agent(scenario.question, cfg)
    assert result.answer
    assert s.tool_executions == 0
