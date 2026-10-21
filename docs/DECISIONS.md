# Decisions

Design choices worth knowing about, one line each: decision, reason.

## Capture and replay

- **Session state is process-global, not a context variable.** Sessions do not nest, and tool calls from worker threads must still see the session; xdist workers are separate processes anyway.
- **`tools` defaults by mode: `replay` in replay, `auto` in drift, forced `live` in record and rewrite.** Each mode then does the safe thing with no configuration.
- **Strict matching serves any unused recording with an equal match key, not only the next one.** Concurrent requests (e.g. parallel tool turns) replay correctly whatever their completion order.
- **Embedded JSON strings are parsed before matching.** Tool-call arguments travel as JSON text, and key order inside them should not break a match.
- **Default `ignore_request_fields`: `request_id`, `timestamp`, `metadata.request_id`, `metadata.timestamp`.** These are the volatile fields SDKs and agents most often add.
- **`ReplayMismatch` reports the closest unused recording and the first differing JSON path before the full diff.** The first difference is usually the whole story (a changed system prompt).
- **Hosts not in `intercept_hosts` pass through untouched while recording, and are blocked in replay unless in `passthrough_hosts`.** Recording should not change behaviour; replay must not touch the network.
- **Tool arguments are bound to parameter names (defaults applied) before recording.** `get_order("1")` and `get_order(order_id="1")` are the same call.
- **On replay, a tool's return annotation is used to rebuild Pydantic models and dataclasses.** Agents get back the type they returned when recording.
- **A recorded tool exception is re-raised as its original type when that type is already imported, else as `ReplayedToolError`.** Agent code that catches specific exceptions keeps working, and a cassette can never cause an import.
- **`record` keeps a cassette even if the test raised, noting the error in `meta.error`; `rewrite` and `drift` do not replace a baseline from a crashed run.** A failed first recording is still worth inspecting; an existing baseline must never be replaced by a crash.
- **The mock provider uses a fixed `created` timestamp and deterministic ids.** Recording the same scenario twice gives byte-identical cassettes.

## Cassettes and redaction

- **Provenance comes from an `x-replex-mock` response header: `meta.provider` is `mock` when present, else the wire format name.** A mock recording can never be mistaken for a real one.
- **Query parameters are redacted by the same name rules as headers.** Some APIs take `?api_key=` or `?access_token=`.
- **`redact_patterns` accepts a bare regex (`CUSTOM`) or a `{name, pattern}` table, and `redact_paths` replaces whole values with `FIELD` tokens.** Named tokens keep cassettes readable; paths handle data no regex describes.
- **Replay redacts live requests with a forked redactor and adopts its numbering only on a match.** Token numbers stay identical to the recording even when several candidates are tried.
- **`replex redact` only checks (`--check`); it does not rewrite cassettes.** Rewriting would change match keys and silently invalidate recordings; re-record instead.
- **The check flags a sensitive header whose value is not `[REDACTED]`, even if it looks harmless.** Header names are a stronger signal than value shapes.

## Traces, diff and drift

- **`final_output` is the last model step's text, or the last non-empty text before it.** Agents often end with a short closing turn after the real answer.
- **JSON output modes tolerate a surrounding Markdown code fence.** Models frequently wrap JSON in ```` ```json ````.
- **JSON Schema is checked by a small built-in validator, or by `jsonschema` when installed.** Keeps the core install to the five allowed runtime dependencies.
- **`tool_args` compares executed tool steps; it falls back to the model's tool requests only when neither run executed any decorated tool.** Avoids double-reporting while still covering agents whose tools are not decorated.
- **`tokens` is skipped when the baseline recorded no usage.** A percentage of zero is meaningless.
- **`steps` counts model and tool steps and ignores decreases.** Doing less is not a regression this check should flag.
- **A model with no known price makes the run's cost unknown, never estimated, and a step with no recorded tokens costs nothing.** A partial total or a guessed price would understate what a change costs.
- **A cost the provider reported in the response (`usage.cost`) wins over the price table.** It already includes cache, routing and tier discounts the table cannot know.
- **Model names are matched after dropping provider prefixes, `@version` and date suffixes, and writing dots as dashes.** One table entry then covers the first-party, OpenRouter and dated names of a model.
- **The `cost` check reports the change as `info` when it is within the limit, and nothing when neither run has a known cost.** Teams change prompts and models to save money, so the saving is worth seeing; mock recordings stay free of noise.
- **The `cost` check defaults to a 25% allowance with severity `warn`, like `tokens`.** Cost usually moves with tokens, and the two should not disagree by default.
- **`latency` findings have severity `info` and cannot be set to `fail` or `warn`.** Timing depends on the network and the machine, not on the change under test.
- **A drift run whose agent raised keeps its candidate but is not compared (outcome `error`).** A crash is a test failure, not behavioural drift.
- **`replex approve` copies the candidate over the baseline, then deletes the candidate, and is refused in CI.** Approval is a local, reviewed act, and a stale candidate should not be approved twice.
- **The `judge` output mode records each judgement as a RepleX cassette named by a hash of the whole judge request.** Re-checking the same pair is offline, free and repeatable, and any change to the model, criteria, prompt or answers asks again.
- **Judge calls use the capture layer directly, not a session, so recording a judgement is not refused in CI.** A judgement is a cache of a verdict, not a baseline; a nightly drift job must be able to judge new answers.
- **A judge reply is validated strictly, and only valid verdicts are stored.** A malformed reply or HTTP error becomes a finding with the check's severity and is retried next time, never a silent pass or a cached failure.
- **An equivalent verdict is an `info` finding carrying the judge's reason.** Reviewers can see why a reworded answer passed.
- **`replex report` exits 0 unless `--fail-on-drift` is given.** It is usually a reporting step after the tests already decided pass or fail.
- **The GitHub Action comments only on `pull_request` events from the same repository, never via `pull_request_target`.** A fork's code must never run with a writable token or a secret.
- **The action keeps one comment per project, found by a hidden marker naming its working directory, and edits it in place.** Re-runs should not bury a pull request in comments.
- **The compact Markdown summary truncates by dropping whole rows and blocks, never mid-table.** A cut-off table or an unclosed `<details>` renders worse than a shorter comment.

## Console and report

- **The built console is committed under `src/replex/console/static/`, and CI checks it matches the source.** `pip install` and the wheel then need no Node toolchain.
- **The HTML report inlines the same React app and the data as JSON in one file.** One codebase for both, and the report works from a CI artifact with no server.
- **`replex console` binds 127.0.0.1 and has a `--host` option used only inside containers.** The console has no authentication; a container must listen on its own interface, and compose publishes it on the host's loopback only.
- **The console only answers requests whose Host header is a loopback name.** Binding to 127.0.0.1 alone does not stop DNS rebinding from a web page.
- **The console serves two workspaces: your project and a sample that ships in the package.** A first-time user sees the whole review flow before recording anything, and real mode stays one click away.
- **The console can accept or discard a candidate in your project, and nothing else.** That is the decision a review ends in; recording stays in pytest. The sample is read-only.
- **Write requests need an `x-replex-action` header and are refused in CI.** A cross-origin page cannot send the header without a CORS preflight the server never approves, and CI must never replace baselines.
- **The console uses plain words: "recorded run" and "latest run", "behaviour changed" and "behaves as recorded".** The CLI and files keep baseline and candidate; people reviewing a change should not need that vocabulary.
- **The console re-reads its workspace when the window regains focus, and keeps the current view on screen while it does.** New recordings appear without a reload or a flash of loading states.
- **The console's design is light-only.** One carefully tuned theme rather than two half-tuned ones.
- **Long text diffs fall back to a whole before/after replacement when the texts share less than half their words.** A word-level diff of two unrelated answers is unreadable confetti.

## Example, packaging and tooling

- **Both `httpx` and `httpx2` are captured, with stream classes that subclass both libraries' bases and responses built by the request's own library.** The official SDKs moved to `httpx2`, while older SDK releases and plenty of other code still use `httpx`; supporting only one would leave many users uncaptured.
- **The real Anthropic and OpenAI SDKs are dev dependencies, and tests record and replay through them.** Capture of the SDKs is the product's central promise; only the SDKs themselves can prove it.
- **The example agent uses raw `httpx` rather than the provider SDKs.** No extra dependencies, and it shows that capture works below any SDK.
- **The example is its own pytest project with its own `[tool.replex]`.** Its settings (cassette dir, mock host, custom redaction rule) do not leak into the library's tests.
- **The example's simulated model is a mock-provider responder that follows the system prompt.** The v1 → v2 prompt change produces a genuine behaviour change offline.
- **Live recordings go to `cassettes/live/<provider>/` and replay through a separate, skip-if-empty test.** Real cassettes are kept apart from mock ones and are only made by the owner.
- **Claude recordings default to `claude-sonnet-5-5` (overridable with `CLAUDE_MODEL`); the MiMo model id always comes from `MIMO_MODEL`.** MiMo ids change between releases and endpoints, so it is never hard-coded.
- **A Dockerfile and compose file provide the demo, the console and the full test gate in containers.** Reproducible runs on machines without the right Python versions; they add no runtime features.
- **The overhead benchmark reports the median of five runs, in nanoseconds.** Single runs are noisy on laptops and CI machines.
