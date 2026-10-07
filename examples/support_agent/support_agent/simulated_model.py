"""A deterministic stand-in for the model, served by RepleX's mock provider.

It reads the system prompt the way a real model would follow it: with prompt v1 it checks the
refund policy before refunding; with prompt v2 ("act right away") it searches the docs instead
and skips the policy check. That gives the example a realistic behavioural change to detect,
with no API key. Cassettes recorded from it are labelled ``provider: mock``; they are never
presented as recordings of Claude or MiMo.
"""

from __future__ import annotations

import json
import re
from typing import Any

from replex.testing import MockReply, MockRequest, MockToolCall


def _decision(decision: str, refund_id: str | None, message: str) -> MockReply:
    return MockReply(
        text=json.dumps({"decision": decision, "refund_id": refund_id, "message": message})
    )


def _last_result(req: MockRequest, name: str) -> Any:
    for tool, text in reversed(req.tool_results):
        if tool == name:
            return json.loads(text)
    return None


def respond(req: MockRequest) -> MockReply:
    """Decide the next reply from the conversation so far."""
    fast = "right away" in (req.system or "")
    question = req.last_user_text
    match = re.search(r"\b(\d{4})\b", question)
    order_id = match.group(1) if match else ""
    called = req.called_tools
    wants_refund = "refund" in question.lower() or "return" in question.lower()

    if "get_order" not in called:
        return MockReply(
            text=f"Let me look up order {order_id}." if not fast else "",
            tool_calls=[MockToolCall("get_order", {"order_id": order_id})],
        )
    order = _last_result(req, "get_order") or {}
    if "error" in order:
        return _decision(
            "info", None, f"I could not find order {order_id}. Could you check the number?"
        )
    item, status = order.get("item", "order"), order.get("status", "unknown")

    if not wants_refund:
        if fast:
            return _decision(
                "info", None, f"Good news! Your {item} (order {order_id}) is {status}."
            )
        return _decision("info", None, f"Your order {order_id} ({item}) is currently {status}.")

    days = order.get("days_since_delivery") or 0
    if not fast:
        if "check_refund_policy" not in called:
            return MockReply(
                tool_calls=[
                    MockToolCall(
                        "check_refund_policy",
                        {"category": order.get("category", ""), "days_since_delivery": days},
                    )
                ]
            )
        policy = _last_result(req, "check_refund_policy") or {}
        if not policy.get("eligible"):
            return _decision(
                "refund_denied",
                None,
                f"I'm sorry, order {order_id} is {policy.get('reason', 'not eligible')}, "
                "so I can't refund it.",
            )
    else:
        if "search_docs" not in called:
            return MockReply(tool_calls=[MockToolCall("search_docs", {"query": "refund window"})])
        if days > 30:
            return _decision(
                "refund_denied", None, f"Sorry! Order {order_id} is past our 30-day refund window."
            )

    if "create_refund_request" not in called:
        return MockReply(
            tool_calls=[
                MockToolCall(
                    "create_refund_request",
                    {
                        "order_id": order_id,
                        "amount": order.get("amount", 0.0),
                        "reason": "Customer requested a refund",
                    },
                )
            ]
        )
    refund = _last_result(req, "create_refund_request") or {}
    rid = refund.get("refund_id")
    if fast:
        return _decision(
            "refund_requested", rid, f"Done! Refund {rid} is on its way for your {item}."
        )
    return _decision(
        "refund_requested",
        rid,
        f"I've requested refund {rid} for your {item}. "
        "It reaches your original payment method in 5-7 business days.",
    )
