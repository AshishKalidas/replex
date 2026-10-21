"""A tiny refund agent whose mock model does not always take the same path.

Shared by the consistency tests and the pytest project they generate with pytester.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any

import httpx

import replex
from replex.testing import MockReply, MockRequest, MockToolCall


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


def varying(skip_policy_on: set[int]) -> Callable[[MockRequest], MockReply]:
    """A model that checks the refund policy, except in the conversations numbered here."""
    conversations = itertools.count(1)
    current = 0

    def respond(req: MockRequest) -> MockReply:
        nonlocal current
        if not req.called_tools:
            current = next(conversations)
            return MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})])
        if len(req.called_tools) == 1:
            if current in skip_policy_on:
                return MockReply(tool_calls=[MockToolCall("search_docs", {"query": "refunds"})])
            return MockReply(tool_calls=[MockToolCall("check_refund_policy", {"order_id": "1042"})])
        return MockReply(text=json.dumps({"decision": "refund_requested"}))

    return respond


def agent(url: str) -> None:
    """Run one conversation against the model at ``url``."""
    messages: list[Any] = [{"role": "user", "content": "refund 1042"}]
    with httpx.Client() as c:
        for _ in range(6):
            body = {"model": "mock-sonnet", "messages": messages}
            rsp = c.post(url + "/v1/messages", json=body).json()
            messages.append({"role": "assistant", "content": rsp["content"]})
            uses = [b for b in rsp["content"] if b["type"] == "tool_use"]
            if not uses:
                return
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": u["id"],
                    "content": json.dumps(TOOLS[u["name"]](**u["input"])),
                }
                for u in uses
            ]
            messages.append({"role": "user", "content": results})
