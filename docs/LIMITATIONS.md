# Limitations

## What replay proves, and what it does not

A replay runs **your code** against **recorded model behaviour**. It proves that, given the
same model responses, your agent still builds the same requests, calls the same tools with the
same arguments, parses the responses and produces the same result. That catches regressions in
your orchestration code, tool wiring, parsing and prompt assembly, quickly, for free and
deterministically.

It does **not** prove anything about the model:

- If the provider changes the model behind a name, replay still passes. Run a **drift** job to
  find out ([DRIFT.md](DRIFT.md), [CI.md](CI.md)).
- If you change the prompt, replay fails with `ReplayMismatch` rather than telling you whether
  the new prompt is better. That is deliberate: old responses say nothing about a new prompt.
  Use a drift run to see the new behaviour, then approve it.
- A recording is one sample of a non-deterministic model. A baseline that happened to take a
  good path does not mean the model always does. Record the scenarios that matter, and treat
  drift findings as signals to look at, tuned through the policy.
- `match: sequence` lets downstream code run against old responses after a request change. It
  tests that code, not the conversation: the responses no longer correspond to the requests.

## Only `httpx` and `httpx2` are captured

RepleX intercepts the clients of `httpx` and, when installed, `httpx2`. The official `anthropic`
and `openai` Python SDKs use one or the other depending on their version, as do many other
libraries. Not captured:

- `requests`, `urllib3`, `aiohttp` and other HTTP clients;
- gRPC and WebSocket transports;
- calls made from subprocesses or other languages.

In replay mode an uncaptured client is **not** blocked: RepleX does not see its traffic. If your
agent uses one, wrap that call in a `@replex.tool` so it is recorded and replayed as a tool.

Only hosts in `intercept_hosts` are recorded. A request to another host is passed through while
recording, and raises `NetworkBlocked` during replay unless the host is in `passthrough_hosts`.

## Tools must be decorated

Only functions decorated with `@replex.tool` (or wrapped with `replex.wrap_tools`) are recorded
and replayed. An undecorated tool runs for real during replay, side effects included.

Tool values must be JSON-serialisable to replay faithfully. RepleX converts Pydantic models,
dataclasses, enums, dates, sets, tuples and bytes. Anything else is stored as its `repr` with a
type tag, the event is flagged `lossy`, and replay returns a `replex.UnserialisableValue`
(type name and `repr`) instead of the original object. If your agent needs the real object back,
return serialisable data from the tool. Note that a Pydantic model or dataclass comes back as
its JSON form (a dict) unless the tool's return annotation lets RepleX rebuild it.

Replay matches tool calls by name and normalised arguments. A tool called with an argument that
changes every run (a timestamp, a random id) will not match its recording; keep such values out
of tool arguments, or generate them inside the tool.

## Non-deterministic request bodies

Strict matching compares the normalised request body. Anything your code puts in the request
that changes from run to run breaks matching:

- timestamps, request ids, nonces: list them in `ignore_request_fields` (dotted paths);
- dictionary or set ordering that varies: key order is normalised, but list order is not;
- values derived from the clock, randomness or the environment inside prompts: make them
  injectable in tests, or strip them with a normaliser function
  (`replex.session(..., normalisers=[fn])`).

The `ReplayMismatch` message shows the first differing JSON path, which is usually enough to
find the volatile field.

## Concurrency and ordering

Concurrent requests replay correctly under strict matching, since any unused recording with an
equal match key can serve a request. Under `match: sequence`, or when two concurrent requests
are byte-identical but expect different responses, the order of completion decides which
response each one gets.

Sessions do not nest, and one session is active per process at a time. With `pytest-xdist`,
each worker runs its own session; two tests must not record the same cassette name in one run.

## Redaction is pattern-based

Default rules cover credentials, bearer tokens, emails and phone numbers. Names, addresses and
other personal data in free text are not detected; add `redact_paths` or `redact_patterns` for
fields that carry them ([REDACTION.md](REDACTION.md)). Replay returns the redacted tokens, not
the original values.

## Other limits

- Streaming is replayed with the recorded event order and content, not the recorded timing.
- Latency is recorded and reported but never enforced: it depends on the machine and network.
- The console runs locally. It reviews runs and can accept or discard a latest run in your
  project; it does not record runs or edit recordings.
- Supported wire formats are Anthropic Messages and OpenAI-compatible chat completions; other
  APIs are recorded and replayed but appear as raw exchanges unless you add a normaliser
  ([PROVIDERS.md](PROVIDERS.md)).
