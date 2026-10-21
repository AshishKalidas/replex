"""Agents for the suite tests: a scripted mock model, sync, async and plain awaitable."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Coroutine, Generator
from typing import Any

import httpx

import replex
from replex.testing import MockProvider, MockReply, MockToolCall

OFFLINE_URL = "http://127.0.0.1:9"


@replex.tool
def get_order(order_id: str) -> dict[str, Any]:
    return {"order_id": order_id, "status": "delivered"}


@replex.tool
def check_refund_policy(order_id: str) -> dict[str, Any]:
    return {"eligible": True}


@replex.tool(safe=True)
def search_docs(query: str) -> list[str]:
    return ["Refunds are accepted within 30 days."]


@replex.tool
def delete_order(order_id: str) -> dict[str, Any]:
    return {"deleted": order_id}


TOOLS = {f.__name__: f for f in (get_order, check_refund_policy, search_docs, delete_order)}

ANSWER = '{"decision": "refund_requested", "order_id": "1042"}'
SCRIPTS: dict[str, list[MockReply | str]] = {
    "v1": [
        MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
        MockReply(tool_calls=[MockToolCall("check_refund_policy", {"order_id": "1042"})]),
        ANSWER,
    ],
    "v2": [
        MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
        MockReply(tool_calls=[MockToolCall("search_docs", {"query": "refund window"})]),
        ANSWER,
    ],
    "destructive": [
        MockReply(tool_calls=[MockToolCall("delete_order", {"order_id": "1042"})]),
        "Deleted.",
    ],
}


def _request(messages: list[Any]) -> dict[str, Any]:
    return {"model": "mock-sonnet", "messages": messages}


def _next(messages: list[Any], content: list[dict[str, Any]]) -> str | None:
    """Append the model's turn and the tool results; return the answer once there is one."""
    messages.append({"role": "assistant", "content": content})
    uses = [b for b in content if b["type"] == "tool_use"]
    if not uses:
        return str(content[0]["text"])
    results = [
        {
            "type": "tool_result",
            "tool_use_id": u["id"],
            "content": json.dumps(TOOLS[u["name"]](**u["input"])),
        }
        for u in uses
    ]
    messages.append({"role": "user", "content": results})
    return None


def _loop(url: str, question: Any) -> str:
    messages: list[Any] = [{"role": "user", "content": json.dumps(question)}]
    with httpx.Client() as client:
        while True:
            body = client.post(url + "/v1/messages", json=_request(messages)).json()
            answer = _next(messages, body["content"])
            if answer is not None:
                return answer


async def _async_loop(url: str, question: Any) -> str:
    messages: list[Any] = [{"role": "user", "content": json.dumps(question)}]
    async with httpx.AsyncClient() as client:
        while True:
            response = await client.post(url + "/v1/messages", json=_request(messages))
            answer = _next(messages, response.json()["content"])
            if answer is not None:
                return answer


def _model(script: str) -> MockProvider | None:
    """The mock model to talk to, or ``None`` when the session replays."""
    if replex.current_session().mode == "replay":
        return None
    return MockProvider(SCRIPTS[script]).start()


def agent(question: Any, script: str = "v1") -> str:
    """A sync agent: the scripted model answers, tools run through RepleX."""
    mock = _model(script)
    try:
        return _loop(mock.url if mock else OFFLINE_URL, question)
    finally:
        if mock:
            mock.stop()


async def async_agent(question: Any, script: str = "v1") -> str:
    """The same agent, async."""
    mock = _model(script)
    try:
        return await _async_loop(mock.url if mock else OFFLINE_URL, question)
    finally:
        if mock:
            mock.stop()


class _Deferred:
    def __init__(self, coroutine: Coroutine[Any, Any, str]) -> None:
        self._coroutine = coroutine

    def __await__(self) -> Generator[Any, None, str]:
        return self._coroutine.__await__()


def deferred_agent(question: Any, script: str = "v1") -> Awaitable[str]:
    """Returns an awaitable that is not a coroutine."""
    return _Deferred(async_agent(question, script))


def hasty_agent(question: Any, script: str = "v1") -> str:
    """Stops after the first model call, leaving the rest of a recording unused."""
    mock = _model(script)
    try:
        url = (mock.url if mock else OFFLINE_URL) + "/v1/messages"
        messages = [{"role": "user", "content": json.dumps(question)}]
        return httpx.post(url, json=_request(messages)).text
    finally:
        if mock:
            mock.stop()


def crashing_agent(question: Any) -> str:
    raise RuntimeError("the agent fell over")


NOT_CALLABLE = 42
