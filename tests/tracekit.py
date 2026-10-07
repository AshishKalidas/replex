"""Tiny DSL for building traces in tests.

    make_trace("t", "model:Let me check", ("get_order", {"id": 1}), "model:Done")

A string ``"model:<text>"`` is a model step; ``"model>tool_a,tool_b"`` a model step that
requests tools; a tuple ``(name, args[, result])`` an executed tool step.
"""

from __future__ import annotations

from typing import Any

from replex.trace.model import ModelStep, ToolRequest, ToolStep, Trace, Usage


def make_trace(
    name: str, *spec: Any, tokens: int = 10, latency: int = 100, model: str = "mock-sonnet"
) -> Trace:
    steps: list[ModelStep | ToolStep] = []
    for item in spec:
        index = len(steps)
        if isinstance(item, (ModelStep, ToolStep)):
            steps.append(item.model_copy(deep=True, update={"index": index}))
        elif isinstance(item, str):
            kind, _, rest = item.partition(":") if ":" in item else item.partition(">")
            assert kind == "model", item
            if ">" in item:
                names = [n for n in item.split(">", 1)[1].split(",") if n]
                steps.append(
                    ModelStep(
                        index=index,
                        model=model,
                        tool_requests=[ToolRequest(name=n) for n in names],
                        stop_reason="tool_use",
                        usage=Usage(input_tokens=tokens, output_tokens=tokens),
                        latency_ms=latency,
                    )
                )
            else:
                steps.append(
                    ModelStep(
                        index=index,
                        model=model,
                        text=rest,
                        stop_reason="end_turn",
                        usage=Usage(input_tokens=tokens, output_tokens=tokens),
                        latency_ms=latency,
                    )
                )
        else:
            tool_name, args, *result = item
            steps.append(
                ToolStep(
                    index=index,
                    name=tool_name,
                    args=args,
                    result=result[0] if result else None,
                    latency_ms=1,
                )
            )
    models = [s for s in steps if isinstance(s, ModelStep)]
    usage = Usage()
    for m in models:
        usage = usage + m.usage
    final = models[-1].text if models else ""
    return Trace(
        name=name,
        provider="mock",
        wire_format="anthropic",
        model=model,
        steps=steps,
        final_output=final,
        usage=usage,
        total_latency_ms=sum(s.latency_ms for s in steps),
    )
