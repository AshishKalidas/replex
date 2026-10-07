"""Anthropic Messages API (``POST /v1/messages``), streaming and non-streaming."""

from __future__ import annotations

import json
from typing import Any

from replex.cassette.model import ExchangeEvent
from replex.providers.base import Normaliser
from replex.trace.model import ModelStep, ToolRequest, Usage


def _system_text(system: Any) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(system)


def _parse_args(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"__raw__": raw}
    return value if isinstance(value, dict) else {"__value__": value}


class AnthropicNormaliser(Normaliser):
    """Messages API: text and ``tool_use`` content blocks, SSE event reassembly."""

    name = "anthropic"

    def matches(self, exchange: ExchangeEvent) -> bool:
        if self.path(exchange).endswith("/v1/messages"):
            return True
        body = self.response_body(exchange)
        if body.get("type") == "message":
            return True
        data = self.stream_data(exchange)
        return bool(data) and data[0].get("type") == "message_start"

    def model_step(self, exchange: ExchangeEvent, index: int) -> ModelStep:
        req = self.request_body(exchange)
        messages = req.get("messages", [])
        step = ModelStep(
            index=index,
            model=str(req.get("model", "unknown")),
            system=_system_text(req.get("system")),
            input_messages_digest=self.digest(messages),
            input_messages=messages if isinstance(messages, list) else [messages],
            latency_ms=exchange.duration_ms,
            streamed=exchange.response.stream is not None,
            wire_format=self.name,
        )
        if exchange.response.stream is not None:
            return self._from_stream(step, exchange)
        return self._from_message(step, exchange)

    def _from_message(self, step: ModelStep, exchange: ExchangeEvent) -> ModelStep:
        body = self.response_body(exchange)
        if exchange.response.status >= 400 or body.get("type") == "error":
            err = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
            step.stop_reason = "error"
            step.error = (
                f"{err.get('type', 'http_error')}: "
                f"{err.get('message', f'HTTP {exchange.response.status}')}"
            )
            return step
        return self._apply_blocks(step, body.get("content", []), body)

    def _apply_blocks(
        self, step: ModelStep, blocks: list[Any], message: dict[str, Any]
    ) -> ModelStep:
        texts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                args = block.get("input")
                step.tool_requests.append(
                    ToolRequest(
                        id=block.get("id"),
                        name=str(block.get("name", "")),
                        args=args if isinstance(args, dict) else {"__value__": args},
                    )
                )
        step.text = "".join(texts)
        step.model = str(message.get("model") or step.model)
        step.stop_reason = str(message.get("stop_reason") or "unknown")
        usage = message.get("usage") or {}
        step.usage = Usage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
        return step

    def _from_stream(self, step: ModelStep, exchange: ExchangeEvent) -> ModelStep:
        message: dict[str, Any] = {}
        blocks: dict[int, dict[str, Any]] = {}
        partial: dict[int, list[str]] = {}
        usage: dict[str, Any] = {}
        for data in self.stream_data(exchange):
            kind = data.get("type")
            if kind == "message_start":
                message = dict(data.get("message") or {})
                usage.update(message.get("usage") or {})
            elif kind == "content_block_start":
                i = int(data.get("index", len(blocks)))
                blocks[i] = dict(data.get("content_block") or {})
                partial[i] = []
            elif kind == "content_block_delta":
                i = int(data.get("index", 0))
                delta = data.get("delta") or {}
                block = blocks.setdefault(i, {"type": "text", "text": ""})
                if delta.get("type") == "text_delta":
                    block["text"] = str(block.get("text", "")) + str(delta.get("text", ""))
                elif delta.get("type") == "input_json_delta":
                    partial.setdefault(i, []).append(str(delta.get("partial_json", "")))
            elif kind == "content_block_stop":
                i = int(data.get("index", 0))
                if blocks.get(i, {}).get("type") == "tool_use":
                    raw = "".join(partial.get(i, []))
                    blocks[i]["input"] = _parse_args(raw) if raw else blocks[i].get("input", {})
            elif kind == "message_delta":
                delta = data.get("delta") or {}
                if delta.get("stop_reason"):
                    message["stop_reason"] = delta["stop_reason"]
                usage.update(data.get("usage") or {})
            elif kind == "error":
                err = data.get("error") or {}
                step.stop_reason = "error"
                step.error = f"{err.get('type', 'error')}: {err.get('message', '')}"
        if step.error:
            return step
        message["usage"] = usage
        return self._apply_blocks(step, [blocks[i] for i in sorted(blocks)], message)
