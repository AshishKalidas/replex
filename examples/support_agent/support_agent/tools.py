"""The agent's tools, backed by a small in-memory store.

Each tool is decorated with ``@replex.tool``: during replay the recorded results are returned
and the functions never run, so ``create_refund_request`` cannot create a duplicate refund.
``search_docs`` has no side effects, so it is marked ``safe=True`` and may run live during a
drift check when the candidate searches for something new.
"""

from __future__ import annotations

from typing import Any

import replex

ORDERS: dict[str, dict[str, Any]] = {
    "1042": {
        "order_id": "1042",
        "customer_email": "jane.doe@example.com",
        "item": "Trail running shoes",
        "category": "footwear",
        "amount": 129.0,
        "currency": "EUR",
        "status": "delivered",
        "days_since_delivery": 12,
    },
    "1043": {
        "order_id": "1043",
        "customer_email": "sam.lee@example.com",
        "item": "Insulated water bottle",
        "category": "accessories",
        "amount": 24.5,
        "currency": "EUR",
        "status": "shipped",
        "days_since_delivery": None,
    },
    "2001": {
        "order_id": "2001",
        "customer_email": "ana.silva@example.com",
        "item": "GPS watch",
        "category": "electronics",
        "amount": 349.0,
        "currency": "EUR",
        "status": "delivered",
        "days_since_delivery": 45,
    },
}

REFUND_WINDOWS = {"footwear": 30, "accessories": 30, "electronics": 30}

DOCS = [
    "Refund window: items can be returned for a full refund within 30 days of delivery.",
    "Electronics must be unopened to qualify for a refund.",
    "Shipping: standard delivery takes 3-5 business days within the EU.",
    "Refunds are paid back to the original payment method within 5-7 business days.",
]

# Side effects the tests can inspect: replay must never append here.
REFUNDS: list[dict[str, Any]] = []


class OrderNotFound(Exception):
    """Raised when an order id does not exist."""


@replex.tool
def get_order(order_id: str) -> dict[str, Any]:
    """Look up an order by id."""
    order = ORDERS.get(order_id.strip().lstrip("#"))
    if order is None:
        raise OrderNotFound(f"order {order_id} not found")
    return dict(order)


@replex.tool
def check_refund_policy(category: str, days_since_delivery: int) -> dict[str, Any]:
    """Decide whether an order is still inside its refund window."""
    window = REFUND_WINDOWS.get(category, 14)
    eligible = days_since_delivery <= window
    reason = (
        f"within the {window}-day window"
        if eligible
        else f"outside the {window}-day window ({days_since_delivery} days since delivery)"
    )
    return {"eligible": eligible, "window_days": window, "reason": reason}


@replex.tool
def create_refund_request(order_id: str, amount: float, reason: str) -> dict[str, Any]:
    """Create a refund request (a side effect: it must never run during replay)."""
    refund = {
        "refund_id": f"RF-{order_id}",
        "order_id": order_id,
        "amount": amount,
        "reason": reason,
        "status": "requested",
    }
    REFUNDS.append(refund)
    return refund


@replex.tool(safe=True)
def search_docs(query: str) -> list[str]:
    """Keyword search over the help-centre articles."""
    words = {w for w in query.lower().split() if len(w) > 2}
    hits = [d for d in DOCS if words & set(d.lower().replace(":", " ").replace(".", " ").split())]
    return hits or DOCS[:1]


TOOLS = {
    f.__name__: f for f in (get_order, check_refund_policy, create_refund_request, search_docs)
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_order",
        "description": "Look up an order by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "check_refund_policy",
        "description": "Check whether an order is eligible for a refund.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "days_since_delivery": {"type": "integer"},
            },
            "required": ["category", "days_since_delivery"],
        },
    },
    {
        "name": "create_refund_request",
        "description": "Create a refund request for an order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "amount", "reason"],
        },
    },
    {
        "name": "search_docs",
        "description": "Search the help-centre articles.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


def reset() -> None:
    """Clear recorded side effects (between tests)."""
    REFUNDS.clear()
