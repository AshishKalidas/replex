"""OpenAI-compatible chat completions (``POST .../chat/completions``).

Used for OpenAI itself and for the many providers that expose the same wire format, such as
MiMo. Streaming chunks are reassembled, including ``tool_calls`` argument fragments.
"""

from __future__ import annotations

from typing import Any

from replex.cassette.model import ExchangeEvent
from replex.providers.anthropic import _parse_args
from replex.providers.base import Normaliser
from replex.trace.model import ModelStep, ToolRequest, Usage


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") in ("text", "output_text")
        )
    return str(content)


class OpenAICompatNormaliser(Normaliser):
    """Chat completions: ``message.content`` and ``message.tool_calls``."""

    name = "openai"

    def matches(self, exchange: ExchangeEvent) -> bool:
        if self.path(exchange).endswith("/chat/completions"):
            return True
        if self.response_body(exchange).get("object") == "chat.completion":
            return True
        data = self.stream_data(exchange)
        return bool(data) and data[0].get("object") == "chat.completion.chunk"

    def model_step(self, exchange: ExchangeEvent, index: int) -> ModelStep:
        req = self.request_body(exchange)
        messages = req.get("messages", [])
        messages = messages if isinstance(messages, list) else [messages]
        system_parts = [
            _content_text(m.get("content"))
            for m in messages
            if isinstance(m, dict) and m.get("role") in ("system", "developer")
        ]
        step = ModelStep(
            index=index,
            model=str(req.get("model", "unknown")),
            system="\n".join(system_parts) if system_parts else None,
            input_messages_digest=self.digest(messages),
            input_messages=messages,
            latency_ms=exchange.duration_ms,
            streamed=exchange.response.stream is not None,
            wire_format=self.name,
        )
        if exchange.response.stream is not None:
            return self._from_stream(step, exchange)
        return self._from_completion(step, exchange)

    def _from_completion(self, step: ModelStep, exchange: ExchangeEvent) -> ModelStep:
        body = self.response_body(exchange)
        if exchange.response.status >= 400 or "error" in body:
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            assert isinstance(err, dict)
            step.stop_reason = "error"
            step.error = (
                f"{err.get('type') or err.get('code') or 'http_error'}: "
                f"{err.get('message', f'HTTP {exchange.response.status}')}"
            )
            return step
        choices = body.get("choices") or [{}]
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        step.text = _content_text(message.get("content"))
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments", "")
            step.tool_requests.append(
                ToolRequest(
                    id=call.get("id"),
                    name=str(fn.get("name", "")),
                    args=args if isinstance(args, dict) else _parse_args(str(args)),
                )
            )
        step.model = str(body.get("model") or step.model)
        step.stop_reason = str(choice.get("finish_reason") or "unknown")
        step.usage = self._usage(body.get("usage"))
        step.reported_cost = self._reported_cost(body.get("usage"))
        return step

    @staticmethod
    def _usage(usage: Any) -> Usage:
        if not isinstance(usage, dict):
            return Usage()
        return Usage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    @staticmethod
    def _reported_cost(usage: Any) -> float | None:
        """The cost some providers (OpenRouter) put in ``usage.cost``, in US dollars."""
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            return float(cost)
        return None

    def _from_stream(self, step: ModelStep, exchange: ExchangeEvent) -> ModelStep:
        texts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        for data in self.stream_data(exchange):
            if "error" in data:
                err = data["error"] if isinstance(data["error"], dict) else {}
                step.stop_reason = "error"
                step.error = f"{err.get('type', 'error')}: {err.get('message', '')}"
                return step
            if data.get("model"):
                step.model = str(data["model"])
            if data.get("usage"):
                step.usage = self._usage(data["usage"])
                step.reported_cost = self._reported_cost(data["usage"])
            for choice in data.get("choices") or []:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    texts.append(_content_text(delta["content"]))
                for frag in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        int(frag.get("index", len(calls))), {"id": None, "name": "", "args": []}
                    )
                    if frag.get("id"):
                        slot["id"] = frag["id"]
                    fn = frag.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"].append(fn["arguments"])
                if choice.get("finish_reason"):
                    step.stop_reason = str(choice["finish_reason"])
        step.text = "".join(texts)
        for i in sorted(calls):
            slot = calls[i]
            step.tool_requests.append(
                ToolRequest(
                    id=slot["id"], name=slot["name"], args=_parse_args("".join(slot["args"]))
                )
            )
        return step
