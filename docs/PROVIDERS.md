# Providers

RepleX has two strictly separate layers:

- The **capture layer** (the `httpx`/`httpx2` transport and `@replex.tool`) knows nothing about
  providers. It records raw HTTP exchanges and tool calls.
- The **interpretation layer** (normalisers, traces, diff) turns recorded exchanges into
  provider-neutral model steps. It never touches the network.

So recording and replay work for any JSON API called through `httpx` or `httpx2`. A normaliser only adds meaning:
which model ran, what it said, which tools it asked for and how many tokens it used.

## Supported wire formats

| Normaliser | Wire format | Detected by | Used for |
|---|---|---|---|
| `AnthropicNormaliser` | Anthropic Messages API, `POST /v1/messages` | URL path, a `"type": "message"` response, or a stream starting with `message_start` | Claude (e.g. `claude-sonnet-5-5`) |
| `OpenAICompatNormaliser` | OpenAI-compatible chat completions, `POST …/chat/completions` | URL path, a `chat.completion` response, or a stream of `chat.completion.chunk` | OpenAI and compatible endpoints (e.g. MiMo 2.6; the model id comes from your configuration) |
| `RawNormaliser` | Anything else | Fallback | Exposes each exchange as a model step without tool semantics, and says so in `trace.notes` |

Both real normalisers handle **streaming and non-streaming** responses. For streams they
reassemble the server-sent events: text deltas, tool-call argument fragments (Anthropic
`input_json_delta`, OpenAI `tool_calls[].function.arguments` chunks), stop reasons and usage.
They cover text-only replies, single and parallel tool calls, multi-step runs and error
responses (a model step with `error` set and `stop_reason: "error"`); the test suite has a case
for each, per wire format.

Remember to add the provider's host to `intercept_hosts`; only those hosts are recorded:

```toml
[tool.replex]
intercept_hosts = ["api.anthropic.com", "api.openai.com", "llm.internal.example.com"]
```

## The mock provider

`replex.testing.MockProvider` is a small local HTTP server that speaks **both** wire formats,
streaming and not, driven by a script. It exists so the test suite, the example and these docs
run with no API key. Every response carries an `x-replex-mock: true` header, and cassettes
recorded from it are labelled `"provider": "mock"`, so a mock recording can never pass for a
real one.

```python
from replex.testing import MockProvider, MockReply, MockToolCall

script = [
    MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
    MockReply(text="Your order has shipped."),
]
with MockProvider(script) as mock:
    client = anthropic.Anthropic(base_url=mock.url, api_key="test")
    # or: openai.OpenAI(base_url=mock.openai_base_url, api_key="test")
```

Instead of a list you can pass a **responder**, a function from a `MockRequest` (wire format,
model, system prompt, messages, tool names, `called_tools`, `tool_results`, `last_user_text`) to
a `MockReply`. The example agent's simulated model is a responder that follows the system
prompt, which is how the v2 prompt produces different behaviour with no real model. A reply can
also be an error (`MockReply(error=MockError(status=529, type="overloaded_error"))`) or set
explicit token counts. `mock.requests` lists what the mock received.

## Adding a normaliser

Subclass `replex.Normaliser`, implement `matches` and `model_step`, and register it. It is tried
before the built-in normalisers.

```python
import replex
from replex import ModelStep, Normaliser, ToolRequest, Usage


class AcmeNormaliser(Normaliser):
    """Acme's API: POST /v2/generate -> {"output": ..., "calls": [...], "usage": {...}}."""

    name = "acme"

    def matches(self, exchange):
        return self.path(exchange).endswith("/v2/generate")

    def model_step(self, exchange, index):
        req, rsp = self.request_body(exchange), self.response_body(exchange)
        usage = rsp.get("usage", {})
        return ModelStep(
            index=index,
            model=str(req.get("model", "unknown")),
            system=req.get("instructions"),
            input_messages_digest=self.digest(req.get("turns", [])),
            input_messages=req.get("turns", []),
            text=rsp.get("output", ""),
            tool_requests=[
                ToolRequest(id=c.get("id"), name=c["tool"], args=c.get("input", {}))
                for c in rsp.get("calls", [])
            ],
            stop_reason=rsp.get("finish", "unknown"),
            usage=Usage(input_tokens=usage.get("in", 0), output_tokens=usage.get("out", 0)),
            latency_ms=exchange.duration_ms,
            wire_format=self.name,
        )


replex.register_normaliser(AcmeNormaliser())   # e.g. in conftest.py
```

Helpers on the base class: `path(exchange)`, `request_body(exchange)`,
`response_body(exchange)`, `stream_data(exchange)` (the JSON payloads of a streamed response,
in order) and `digest(messages)` (a short stable hash). Pass `first=False` to
`register_normaliser` to try yours after the built-in ones.

Because the capture layer stores raw exchanges, a normaliser registered later applies to
cassettes recorded before it existed: `replex.load_trace`, `replex.compare` and your tests pick
it up as soon as it is registered. The `replex` CLI and console only know the built-in
normalisers, so there such cassettes show as raw exchanges.

Test a normaliser by recording a few exchanges from the real API (or building them by hand
for a unit test), calling `replex.load_trace(cassette)` and asserting on the steps.
