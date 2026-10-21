# Concepts

RepleX records what an agent does (model calls over HTTP, and tool calls) into a **cassette**,
turns it into a provider-neutral **trace**, and lets you replay, assert on and compare traces.

| Term | Meaning |
|---|---|
| **Cassette** | One recorded run, a pretty-printed JSON file with stable key order |
| **Session** | The block in which recording or replay is active |
| **Exchange** | One HTTP request and its response |
| **Tool call** | One execution of a decorated tool: name, arguments, result or error |
| **Trace** | The provider-neutral view of a cassette: ordered model steps and tool steps |
| **Baseline** | The committed cassette that defines expected behaviour |
| **Candidate** | A fresh recording being compared with the baseline |
| **Drift** | A difference between baseline and candidate that the policy cares about |
| **Policy** | A YAML file stating which differences fail, warn or are ignored |

## Cassettes

A cassette lives at `<cassette_dir>/<name>.replex.json` (default `tests/cassettes`). Model
exchanges and tool calls share **one ordered `events` list**, so their interleaving is
preserved.

```json
{
  "schema_version": 1,
  "name": "refund_request",
  "created_at": "2026-10-07T09:00:00Z",
  "replex_version": "0.1.0",
  "meta": {"provider": "anthropic", "model": "claude-sonnet-5-5", "git_sha": "abc1234", "tags": []},
  "events": [
    {"index": 0, "type": "exchange",
     "request": {"method": "POST", "url": "https://api.anthropic.com/v1/messages",
                 "headers": {"x-api-key": "[REDACTED]"}, "body": {"...": "..."}, "match_key": "sha256:..."},
     "response": {"status": 200, "headers": {}, "body": {"...": "..."}, "stream": null},
     "duration_ms": 812},
    {"index": 1, "type": "tool_call", "name": "get_order", "args": {"order_id": "1042"},
     "result": {"status": "delivered"}, "error": null, "duration_ms": 3}
  ],
  "usage": {"input_tokens": 1418, "output_tokens": 93}
}
```

- **Streaming**: a streamed response stores its server-sent events, in order, under
  `response.stream` (with `body: null`) and replays them as a stream with the same event order
  and content.
- **Deterministic output**: sorted keys, two-space indent, trailing newline. Saving the same
  cassette twice is byte-identical, so cassettes diff cleanly in pull requests.
- **Atomic writes**: written to a temporary file in the same directory, then renamed. A crash
  never leaves a half-written cassette, and `pytest-xdist` workers never collide.
- **Validation**: loading validates with Pydantic. An unknown `schema_version` raises
  `UnsupportedSchemaVersion` with the version found and the versions supported.
- **Provenance**: `meta.provider` is `mock` for anything recorded from RepleX's mock provider,
  otherwise the wire format (`anthropic`, `openai`, `raw`). Real recordings are never relabelled.

Inspect cassettes from the terminal:

```bash
replex ls tests/cassettes            # name, provider, model, steps, tool calls, tokens, cost, date
replex show refund_request           # step by step (--full for untruncated values, --json)
```

## Traces

A `Trace` is built from a cassette by a provider **normaliser** chosen from the request URL
and body shape (see [PROVIDERS.md](PROVIDERS.md)):

```python
class ModelStep:   # one model call
    index: int; model: str; system: str | None
    input_messages_digest: str          # hash of the messages sent
    text: str                           # assistant text returned
    tool_requests: list[ToolRequest]    # id, name, args
    stop_reason: str; usage: Usage; latency_ms: int
    cost: float | None                  # US dollars; None when the model has no known price

class ToolStep:    # one tool execution
    index: int; name: str; args: dict; result: Any; error: str | None; latency_ms: int

class Trace:
    name: str; provider: str; model: str
    steps: list[ModelStep | ToolStep]
    final_output: str                   # text of the last model step that produced text
    usage: Usage; total_latency_ms: int
    cost: float | None                  # total in US dollars, see COST.md
    unpriced_models: list[str]          # why the cost is unknown, when it is
```

Get one from a session (`s.trace`), from a cassette (`replex.load_trace(path)`) or in a test
(`replex_session.trace`). Assertions raise `AssertionError` with the actual sequence in the
message:

| Assertion | Example |
|---|---|
| `assert_tool_sequence(names, mode=)` | `mode="exact"` (default), `"subsequence"` (in order, gaps allowed) or `"same_set"` |
| `assert_called(name, times=None, with_args=None)` | `assert_called("create_refund_request", with_args={"amount": 129.0})` (subset match) |
| `assert_no_tool(pattern)` | `assert_no_tool("delete_*")` (glob) |
| `assert_max_steps(n)`, `assert_max_tokens(n)` | budget guards |
| `assert_final_output(contains=, matches=, json_schema=)` | substring, `re.search` regex, or JSON Schema |

Properties: `tool_names`, `steps_count`, `model_calls`, `tool_calls`.

## Modes

| Mode | Network | Tools | Behaviour |
|---|---|---|---|
| `replay` (default) | Blocked | replayed | Serve recorded responses. A missing cassette or an unmatched request fails |
| `record` | Allowed | live | Run live and write the baseline. Refuses to overwrite unless `--replex-overwrite` |
| `drift` | Allowed | `auto` | Run live, write a candidate, compare with the baseline, fail on `fail` findings |
| `rewrite` | Allowed | live | Run live and replace the baseline (approve by re-recording) |
| `live` | Allowed | live | RepleX does nothing; tests run normally |

When the `CI` environment variable is set, `record` and `rewrite` (and `replex approve`) are
refused with `RecordingRefused` unless `REPLEX_ALLOW_RECORD_IN_CI=1`. CI must never silently
create baselines.

### The session API

The pytest plugin is a thin layer over sessions; you can use them directly anywhere:

```python
import replex

with replex.session("refund_request", mode="record") as s:
    answer = run_agent("I want a refund for order 1042")

s.trace.assert_tool_sequence(["get_order", "check_refund_policy", "create_refund_request"])
```

`async with replex.session(...)` works the same way. Sessions do not nest: starting one inside
another raises `SessionActive`. `replex.current_session()` returns the active one. In `drift`
mode, `s.drift_report` holds the comparison afterwards; `s.tool_executions` counts tool calls
that actually executed (zero in a full replay).

## HTTP capture

While a session is active RepleX installs its transport into the clients of `httpx` and, when
it is installed, `httpx2` (**automatic mode**), so SDK clients are intercepted with no code
changes. Current releases of the official Anthropic and OpenAI SDKs use `httpx2`; older
releases use `httpx`. The test suite drives both real SDKs, sync, async and streaming, through
a record and an offline replay, and drives plain `httpx` clients through every capture path. The patch is removed when the
session ends, including on exceptions.

If you prefer to be explicit, pass a RepleX client to the SDK and set `patch_httpx=False`. Use
the library the SDK expects: `httpx2` for current SDKs, `httpx` (the default) for older ones.

```python
client = anthropic.Anthropic(http_client=replex.http_client(library="httpx2"))
client = openai.AsyncOpenAI(http_client=replex.async_http_client(library="httpx2"))
```

- Only hosts in `intercept_hosts` (default `api.anthropic.com`) are recorded.
- In `replay` mode a request to any other host raises `NetworkBlocked`, unless the host is in
  `passthrough_hosts` (default `localhost`, `127.0.0.1`).

## Matching

How a request during replay finds its recorded response is set by `match`:

- **`strict`** (default): method, URL path and the **normalised** JSON body must equal the
  recording. Any unused recorded exchange can match, so concurrent requests are fine.
- **`sequence`**: the nth request gets the nth recorded response, whatever its content. Use it
  to test downstream code against old responses after an intentional prompt change.

Normalisation before matching:

1. Drop volatile fields listed in `ignore_request_fields` (dotted paths; default
   `request_id`, `timestamp`, `metadata.request_id`, `metadata.timestamp`).
2. Parse JSON embedded in strings (tool arguments are often JSON text) and sort all keys.
3. Apply redaction, so a secret in the body matches its redacted recording.
4. Apply your own normaliser functions (`replex.session(..., normalisers=[fn])`), each taking
   and returning the body.

When nothing matches, `ReplayMismatch` names the request, the closest recording, the first
differing JSON path and a unified diff:

```text
replex.errors.ReplayMismatch: request #0 (POST /v1/messages) does not match the recording.

  cassette:   examples/support_agent/cassettes/refund_request.replex.json
  match mode: strict
  closest recorded request: event 0 (POST /v1/messages)

  First difference at $.system:
    recorded: "You are the customer support agent for Acme Outdoor.\nAlways look up…
    actual:   "You are the fast, friendly customer support agent for Acme Outdoor. Cu…

  Body diff (recorded -> actual):
    ...

  If this change is intentional, re-record the baseline:
    pytest --replex-mode=rewrite      (or: replex approve <name> after a drift run)
  To test downstream code against the old responses, use match='sequence'.
```

At the end of a replay, recorded exchanges that were never requested raise `UnusedRecording`
(disable with `strict_unused = false`): the agent made fewer calls than when it was recorded.

## Tools

```python
@replex.tool
def get_order(order_id: str) -> dict: ...

@replex.tool(safe=True)          # read-only: may run live during drift
async def search_docs(query: str) -> list[str]: ...

tools = replex.wrap_tools({"get_order": get_order_impl, "search_docs": search_impl})
```

Outside a session a decorated tool is a plain pass-through (about 70 ns per call). Inside one,
its name, JSON-serialised arguments (bound to parameter names), result or exception, and
duration are recorded. The `tools` setting decides what happens on replay:

| `tools` | Behaviour |
|---|---|
| `replay` (default in `replay`) | Return the recorded result for the same name and normalised arguments; never execute. A recorded exception is re-raised (same type when that type is already imported, else `ReplayedToolError`) |
| `live` (forced in `record`/`rewrite`) | Always execute |
| `auto` (default in `drift`) | Replay when name and arguments match a recording; otherwise execute if `safe=True`, else raise `UnrecordedToolCall` |

Values that are not JSON-serialisable are stored faithfully where possible (Pydantic models,
dataclasses, enums, dates, sets, tuples, bytes) and otherwise as
`{"__replex_repr__": "...", "__replex_type__": "module.Type"}`, with `lossy: true` on the event.
On replay such a value comes back as a `replex.UnserialisableValue` carrying the type name and
`repr`.

## Public API

Everything importable from `replex` is public; everything else is private.

| Name | Purpose |
|---|---|
| `session`, `Session`, `current_session` | Start and inspect sessions |
| `tool`, `wrap_tools`, `UnserialisableValue` | Tool capture |
| `http_client`, `async_http_client`, `ReplexTransport`, `AsyncReplexTransport` | Explicit HTTP capture |
| `Cassette`, `load_cassette` | The cassette model |
| `Trace`, `ModelStep`, `ToolStep`, `ToolRequest`, `Usage`, `load_trace` | The trace model |
| `diff`, `TraceDiff` | Aligned step diff of two traces |
| `compare`, `DriftReport`, `Finding`, `Policy`, `load_policy` | Drift ([DRIFT.md](DRIFT.md)) |
| `Pricing` | Model prices and the cost of traces ([COST.md](COST.md)) |
| `Config`, `load_config` | Configuration ([PYTEST.md](PYTEST.md#configuration)) |
| `Normaliser`, `register_normaliser` | Provider support ([PROVIDERS.md](PROVIDERS.md)) |
| `ReplexError` and subclasses | `CassetteError`, `CassetteExists`, `CassetteNotFound`, `UnsupportedSchemaVersion`, `ConfigError`, `PolicyError`, `ReplayMismatch`, `NetworkBlocked`, `UnusedRecording`, `UnrecordedToolCall`, `ReplayedToolError`, `RecordingRefused`, `SessionActive`, `NoActiveSession`, `DriftDetected` |
| `__version__` | Package version |
