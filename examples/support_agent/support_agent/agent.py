"""A small tool-calling support agent over raw httpx.

It speaks the Anthropic Messages API (Claude Sonnet 5.5) and the OpenAI-compatible chat
completions API (MiMo 2.6, or any compatible endpoint), streaming or not. It uses plain httpx
on purpose: RepleX works below any SDK or framework, at the HTTP and tool level, so the same
recording works whether you call the API through an SDK, LangChain or your own loop.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from support_agent.prompts import PROMPTS
from support_agent.tools import TOOL_SCHEMAS, TOOLS

Provider = Literal["anthropic", "openai"]


@dataclass
class AgentConfig:
    """How to reach the model. ``from_env`` reads the same settings from the environment."""

    provider: Provider = "anthropic"
    base_url: str = "https://api.anthropic.com"
    api_key: str = ""
    model: str = "claude-sonnet-5-5"
    prompt_version: str = "v1"
    stream: bool = False
    max_steps: int = 8
    timeout: float = 60.0

    @classmethod
    def from_env(cls, **overrides: Any) -> AgentConfig:
        env = os.environ
        provider: Provider = (
            "openai" if env.get("SUPPORT_AGENT_PROVIDER") == "openai" else "anthropic"
        )
        values: dict[str, Any] = {
            "provider": provider,
            "prompt_version": env.get("PROMPT_VERSION", "v1"),
            "stream": env.get("SUPPORT_AGENT_STREAM", "") == "1",
        }
        if provider == "anthropic":
            values.update(
                base_url=env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                api_key=env.get("ANTHROPIC_API_KEY", ""),
                model=env.get("SUPPORT_AGENT_MODEL", "claude-sonnet-5-5"),
            )
        else:
            values.update(
                base_url=env.get("MIMO_BASE_URL", ""),
                api_key=env.get("MIMO_API_KEY", ""),
                model=env.get("MIMO_MODEL", ""),
            )
        values.update(overrides)
        return cls(**values)


@dataclass
class AgentResult:
    """The final answer plus the tool calls the agent made, for convenience."""

    answer: str
    tool_calls: list[str] = field(default_factory=list)
    steps: int = 0

    @property
    def decision(self) -> dict[str, Any] | None:
        """The parsed JSON decision, if the answer is JSON."""
        text = self.answer.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


def _run_tool(name: str, args: dict[str, Any]) -> str:
    tool = TOOLS.get(name)
    if tool is None:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        return json.dumps(tool(**args))
    except Exception as exc:  # report tool errors to the model, as real agents do
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _sse(response: httpx.Response) -> Iterator[Any]:
    """Yield JSON payloads of a server-sent event stream."""
    for line in response.iter_lines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                yield json.loads(data)


# ------------------------------------------------------------------ Anthropic Messages API


def _anthropic_turn(
    client: httpx.Client, cfg: AgentConfig, messages: list[Any]
) -> list[dict[str, Any]]:
    body = {
        "model": cfg.model,
        "max_tokens": 1024,
        "system": PROMPTS[cfg.prompt_version],
        "tools": TOOL_SCHEMAS,
        "messages": messages,
    }
    headers = {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"}
    url = cfg.base_url.rstrip("/") + "/v1/messages"
    if not cfg.stream:
        response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        return list(response.json()["content"])
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, list[str]] = {}
    with client.stream("POST", url, json={**body, "stream": True}, headers=headers) as response:
        response.raise_for_status()
        for event in _sse(response):
            kind = event.get("type")
            if kind == "content_block_start":
                blocks[event["index"]] = dict(event["content_block"])
                partial[event["index"]] = []
            elif kind == "content_block_delta":
                delta = event["delta"]
                if delta["type"] == "text_delta":
                    blocks[event["index"]]["text"] += delta["text"]
                elif delta["type"] == "input_json_delta":
                    partial[event["index"]].append(delta["partial_json"])
            elif kind == "content_block_stop" and blocks[event["index"]]["type"] == "tool_use":
                raw = "".join(partial[event["index"]])
                blocks[event["index"]]["input"] = json.loads(raw) if raw else {}
    return [blocks[i] for i in sorted(blocks)]


def _run_anthropic(client: httpx.Client, cfg: AgentConfig, question: str) -> AgentResult:
    messages: list[Any] = [{"role": "user", "content": question}]
    result = AgentResult(answer="")
    for _ in range(cfg.max_steps):
        content = _anthropic_turn(client, cfg, messages)
        result.steps += 1
        messages.append({"role": "assistant", "content": content})
        uses = [b for b in content if b.get("type") == "tool_use"]
        if not uses:
            result.answer = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            return result
        outputs = []
        for use in uses:
            result.tool_calls.append(use["name"])
            outputs.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use["id"],
                    "content": _run_tool(use["name"], use["input"]),
                }
            )
        messages.append({"role": "user", "content": outputs})
    raise RuntimeError(f"agent did not finish within {cfg.max_steps} model calls")


# ------------------------------------------------------------------ OpenAI-compatible API


def _openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOL_SCHEMAS
    ]


def _openai_turn(client: httpx.Client, cfg: AgentConfig, messages: list[Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"model": cfg.model, "messages": messages, "tools": _openai_tools()}
    headers = {"authorization": f"Bearer {cfg.api_key}"}
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    if not cfg.stream:
        response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        return dict(response.json()["choices"][0]["message"])
    text: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    stream_body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    with client.stream("POST", url, json=stream_body, headers=headers) as response:
        response.raise_for_status()
        for chunk in _sse(response):
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                for frag in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        frag["index"],
                        {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    slot["id"] = frag.get("id") or slot["id"]
                    fn = frag.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return message


def _run_openai(client: httpx.Client, cfg: AgentConfig, question: str) -> AgentResult:
    messages: list[Any] = [
        {"role": "system", "content": PROMPTS[cfg.prompt_version]},
        {"role": "user", "content": question},
    ]
    result = AgentResult(answer="")
    for _ in range(cfg.max_steps):
        message = _openai_turn(client, cfg, messages)
        result.steps += 1
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            result.answer = message.get("content") or ""
            return result
        for call in calls:
            name = call["function"]["name"]
            result.tool_calls.append(name)
            args = json.loads(call["function"]["arguments"] or "{}")
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": _run_tool(name, args)}
            )
    raise RuntimeError(f"agent did not finish within {cfg.max_steps} model calls")


def run_agent(question: str, config: AgentConfig | None = None) -> AgentResult:
    """Answer a customer message, calling tools as the model asks."""
    cfg = config or AgentConfig.from_env()
    with httpx.Client(timeout=cfg.timeout) as client:
        if cfg.provider == "openai":
            return _run_openai(client, cfg, question)
        return _run_anthropic(client, cfg, question)
