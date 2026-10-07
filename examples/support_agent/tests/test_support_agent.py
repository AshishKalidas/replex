"""The support agent's regression suite.

Tests assert *invariants* (no destructive tools, a well-formed decision, the right outcome for
the order). The exact behaviour (which tools, in which order, with which arguments) is pinned
by the committed cassettes and checked by the drift policy in replex.policy.yaml.
"""

from __future__ import annotations

from typing import Any

import pytest
from support_agent import AgentConfig, run_agent
from support_agent import tools as agent_tools
from support_agent.scenarios import SCENARIOS_BY_NAME

import replex
from replex import Session

OFFLINE_URL = "http://127.0.0.1:9"

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "refund_id", "message"],
    "properties": {"decision": {"enum": ["refund_requested", "refund_denied", "info"]}},
}

SCENARIOS = [
    pytest.param(
        SCENARIOS_BY_NAME["refund_request"].question,
        "anthropic",
        False,
        "refund_requested",
        marks=pytest.mark.replex(cassette="refund_request"),
        id="refund_request",
    ),
    pytest.param(
        SCENARIOS_BY_NAME["order_status"].question,
        "anthropic",
        True,
        "info",
        marks=pytest.mark.replex(cassette="order_status_stream"),
        id="order_status_stream",
    ),
    pytest.param(
        SCENARIOS_BY_NAME["refund_ineligible"].question,
        "openai",
        False,
        "refund_denied",
        marks=pytest.mark.replex(cassette="refund_ineligible_openai"),
        id="refund_ineligible_openai",
    ),
    pytest.param(
        "Can you tell me the status of order 1043?",
        "openai",
        True,
        "info",
        marks=pytest.mark.replex(cassette="order_status_openai_stream"),
        id="order_status_openai_stream",
    ),
]


@pytest.mark.parametrize(("question", "provider", "stream", "expected"), SCENARIOS)
def test_support_agent(
    replex_session: Session,
    agent_config: Any,
    question: str,
    provider: str,
    stream: bool,
    expected: str,
) -> None:
    result = run_agent(question, agent_config(provider, stream))

    trace = replex_session.trace
    trace.assert_no_tool("delete_*")
    trace.assert_max_steps(10)
    trace.assert_final_output(json_schema=DECISION_SCHEMA)
    trace.assert_called("get_order", times=1)
    assert result.decision is not None
    assert result.decision["decision"] == expected
    if expected != "refund_requested":
        trace.assert_no_tool("create_refund_request")

    if replex_session.mode == "replay":
        # Tools were replayed, not executed: no refund was created again.
        assert agent_tools.REFUNDS == []
        assert replex_session.tool_executions == 0


def test_refund_procedure_v1_is_pinned(request: pytest.FixtureRequest) -> None:
    """The same cassette can back several tests. This one uses the session API directly and
    always replays, pinning the v1 refund procedure whatever mode the suite runs in."""
    config = replex.load_config(request.config.rootpath)
    agent = AgentConfig(
        provider="anthropic",
        base_url=OFFLINE_URL,
        api_key="test-key-not-real",
        model="mock-sonnet",
        prompt_version="v1",
    )
    with replex.session("refund_request", mode="replay", config=config) as s:
        run_agent(SCENARIOS_BY_NAME["refund_request"].question, agent)
    s.trace.assert_tool_sequence(["get_order", "check_refund_policy", "create_refund_request"])
    s.trace.assert_called("create_refund_request", with_args={"order_id": "1042", "amount": 129.0})
    assert agent_tools.REFUNDS == []
