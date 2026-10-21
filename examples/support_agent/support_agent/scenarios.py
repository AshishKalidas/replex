"""Customer messages used by the tests, the demo and the live-recording script."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    name: str
    question: str


SCENARIOS = [
    Scenario(
        "refund_request",
        "Hi, I'd like a refund for order 1042 please, the shoes don't fit. "
        "My ticket is TCK-482910, "
        "you can reach me at jane.doe@example.com.",
    ),
    Scenario("order_status", "Where is my order 1043?"),
    Scenario(
        "refund_ineligible", "I want to return order 2001 for a refund, the watch is too big."
    ),
    Scenario("order_status_rephrased", "Can you tell me the status of order 1043?"),
]

SCENARIOS_BY_NAME = {s.name: s for s in SCENARIOS}
