"""replex.suite.yaml replays the same cassettes as test_support_agent.py, offline."""

from __future__ import annotations

from pathlib import Path

from support_agent import tools as agent_tools

from replex.cassette.storage import list_cassettes, name_from_path
from replex.suite import load_suite, run_suite, suite_config

SUITE = Path(__file__).resolve().parent.parent / "replex.suite.yaml"


def test_suite_replays_the_committed_cassettes() -> None:
    suite = load_suite(SUITE)
    results = run_suite(suite, suite_config(suite, "replay"))
    assert [(r.name, r.status, r.outcome) for r in results] == [
        (s.name, "passed", "replayed") for s in suite.scenarios
    ]
    cassettes = list_cassettes(SUITE.parent / "cassettes")
    assert {s.name for s in suite.scenarios} == {name_from_path(p) for p in cassettes}
    assert agent_tools.REFUNDS == []
