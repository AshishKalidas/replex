# Walkthrough: a prompt change that breaks an agent

This walkthrough uses the example in [`examples/support_agent/`](../examples/support_agent). It
runs entirely offline with no API key. The short version is one command:

```bash
make demo
```

The rest of this page explains what that command does and how you would work through the same
situation in your own project.

## The agent

A small order-support agent for a fictional outdoor shop. It speaks either the Anthropic
Messages API or the OpenAI-compatible chat completions API, over plain `httpx`, streaming or
not, and has four tools:

| Tool | Decorator | What it does |
|---|---|---|
| `get_order(order_id)` | `@replex.tool` | Look up an order (customer email, category, days since delivery) |
| `check_refund_policy(category, days_since_delivery)` | `@replex.tool` | Is the order inside its refund window? |
| `create_refund_request(order_id, amount, reason)` | `@replex.tool` | **Side effect**: creates a refund |
| `search_docs(query)` | `@replex.tool(safe=True)` | Read-only help-centre search |

It answers with a JSON decision: `{"decision": "refund_requested" | "refund_denied" | "info",
"refund_id": ..., "message": ...}`.

The system prompt lives in `support_agent/prompts.py` in two versions:

- **v1** says: always look up the order, and *before creating any refund, always call
  `check_refund_policy`*.
- **v2** is a "small wording change" for friendliness and speed: *if you need policy details,
  use `search_docs`, then act right away without extra checks*.

The example has no real model in its tests. `support_agent/simulated_model.py` is a responder
for RepleX's [mock provider](PROVIDERS.md#the-mock-provider) that follows whichever prompt it
is given, so the prompt change produces a genuine behaviour change. Its cassettes are labelled
`"provider": "mock"`.

## The tests

`tests/test_support_agent.py` runs four scenarios, each pinned by a committed cassette:

| Cassette | Wire format | Streamed | Expected decision |
|---|---|---|---|
| `refund_request` | Anthropic | no | `refund_requested` |
| `order_status_stream` | Anthropic | yes | `info` |
| `refund_ineligible_openai` | OpenAI-compatible | no | `refund_denied` |
| `order_status_openai_stream` | OpenAI-compatible | yes | `info` |

The tests assert **invariants** that must hold whatever the model does: no `delete_*` tool, at
most 10 steps, a final answer matching the decision schema, `get_order` called once, the right
decision for the order. The **exact behaviour** (which tools, in which order, with which
arguments) is pinned by the cassettes and judged by the drift policy, `replex.policy.yaml`.

The refund scenario's customer message contains an email address and a ticket number. Open
`cassettes/refund_request.replex.json` and you will find `[REDACTED_EMAIL_1]` and
`[REDACTED_TICKET_1]` instead: the email by a default rule, the ticket by the custom rule in
the example's `pyproject.toml` ([REDACTION.md](REDACTION.md)).

## Step 1: replay

```bash
cd examples/support_agent
pytest
```

```text
s........                                                                [100%]
==================================== RepleX ====================================
mode: replay | replayed: 4 | recorded: 0 | drift passed: 0 | drift failed: 0 | warnings: 0
8 passed, 1 skipped in 0.44s
```

In replay mode the agent is pointed at `http://127.0.0.1:9`, where nothing listens: every
model response comes from the cassette. Tools are not executed either; the test asserts that no
refund was created and that `replex_session.tool_executions == 0`. (The skipped test replays
real-model cassettes from `cassettes/live/`, which only exist once the owner records them.)

## Step 2: change the prompt and replay again

Switch to the v2 prompt and run the same replay:

```bash
PROMPT_VERSION=v2 pytest
```

Replay now fails immediately, because the agent sends a different request than the one recorded:

```text
E   replex.errors.ReplayMismatch: request #0 (POST /v1/messages) does not match the recording.

      cassette:   examples/support_agent/cassettes/refund_request.replex.json
      match mode: strict
      closest recorded request: event 0 (POST /v1/messages)

      First difference at $.system:
        recorded: "You are the customer support agent for Acme Outdoor.\nAlways look up the order…
        actual:   "You are the fast, friendly customer support agent for Acme Outdoor. Customers…

      If this change is intentional, re-record the baseline:
        pytest --replex-mode=rewrite      (or: replex approve <name> after a drift run)
      To test downstream code against the old responses, use match='sequence'.
```

That is replay doing its job: the recorded responses were produced for a different prompt, so
they no longer prove anything. To learn what the new prompt actually *does*, run a drift check.

## Step 3: drift check

```bash
PROMPT_VERSION=v2 pytest --replex-mode=drift
```

In drift mode the agent runs live (here: against the mock model), each run is saved as a
candidate in `.replex/candidates/`, and compared with its baseline under the policy:

```text
FAILED tests/test_support_agent.py::test_support_agent[refund_request]
FAILED tests/test_support_agent.py::test_support_agent[refund_ineligible_openai]
==================================== RepleX ====================================
mode: drift | replayed: 0 | recorded: 0 | drift passed: 2 | drift failed: 2 | warnings: 1
```

The two order-status scenarios pass: v2 words its answers differently but takes the same
path. The two refund scenarios fail. Every individual test assertion still passed (the v2 agent
still produces valid decisions and never deletes anything), so a conventional test suite would
have let this change through.

## Step 4: read the report

```bash
replex report cassettes .replex/candidates --html .replex/replex-report.html
```

Open the file in a browser; it needs no server. For one pair in the terminal:

```bash
replex drift cassettes/refund_ineligible_openai.replex.json \
             .replex/candidates/refund_ineligible_openai.replex.json
```

| Severity | Check | Step | Finding |
|---|---|---|---|
| ❌ fail | `tool_sequence` | 3 | tool sequence changed (exact): ['get_order', 'check_refund_policy'] -> ['get_order', 'search_docs'] |
| ❌ fail | `new_tools` | 3 | candidate calls tool(s) absent from the baseline: ['search_docs'] |
| ⚠️ warn | `final_output` | 4 | final output similarity 0.73 is below 0.80 |
| 💬 info | `latency` |  | latency 1 ms -> 2 ms (+100%) (reported only) |

Read it top-down:

1. **`tool_sequence` (fail)**: the v2 agent no longer calls `check_refund_policy`. It looks the
   policy up in the docs instead and decides by itself.
2. **`new_tools` (fail)**: `search_docs` was never part of this flow.
3. **`final_output` (warn)**: the wording changed. The policy only warns on wording; for
   `refund_request` it instead requires the answer to match `schemas/refund.json`, which v2
   still does.

The aligned step diff shows exactly where the runs part ways:

```text
~  model → get_order               model → get_order
=  get_order({"order_id": "2001"})  get_order({"order_id": "2001"})
~  model → check_refund_policy     model → search_docs
-  check_refund_policy({...})
+                                  search_docs({"query": "refund window"})
~  model: {"decision": ...}        model: {"decision": ...}
```

For `refund_request` the consequence is worse: v2 calls `create_refund_request` **without
checking eligibility**. It happens to be eligible in this scenario, but the safety check the
business relies on is gone. This is the regression that would otherwise reach production.

The console tells the same story in plain language. From `examples/support_agent`:

```bash
replex console
```

It opens on an overview that says two scenarios need review, with one sentence each about what
changed. Each scenario's review page shows the two runs side by side and step by step:

![Reviewing refund_request](images/console-scenario.png)

![The aligned step-by-step diff](images/console-compare.png)

## Step 5: decide

**The change is wrong** (it is, here): fix the prompt, run the drift check again until it
passes, and delete `.replex/`. The baselines never changed.

**The change is right**: accept it. In the console, answer **Yes, accept the new behaviour** on
each scenario's review page. Or from the command line, promote the candidates to baselines. Either
way, commit them, so the pull request shows the behaviour change as a cassette diff for review:

```bash
replex approve refund_request
replex approve refund_ineligible_openai
git add cassettes/ && git commit -m "Adopt v2 support prompt"
```

```text
approved refund_request: cassettes/refund_request.replex.json now holds the new baseline (commit it)
```

From then on, replay pins the new behaviour. (With a real model you can also re-record on
purpose with `pytest --replex-mode=rewrite`.)

## What `make demo` runs

`examples/support_agent/demo.py` performs steps 1, 3 and 4 with no API key, clearing any
`REPLEX_*` and `CI` variables first so your environment cannot change the outcome:

1. `pytest` in replay mode, which must pass;
2. `pytest --replex-mode=drift` with `PROMPT_VERSION=v2`, which must detect drift;
3. `replex report`, which writes `.replex/replex-report.html` and `.replex/replex-report.md`
   and prints the path.

It exits non-zero if replay fails or if the v2 prompt unexpectedly passes the drift check.

## Recording real cassettes

The committed cassettes come from the mock model. To record the same scenarios from real models
(written to `cassettes/live/<provider>/`, secrets redacted before anything touches disk):

```bash
export ANTHROPIC_API_KEY=...
make record-claude            # Claude Sonnet 5.5 (override with CLAUDE_MODEL)

export MIMO_API_KEY=... MIMO_BASE_URL=... MIMO_MODEL=...
make record-mimo              # MiMo 2.6 through its OpenAI-compatible endpoint
```

Once recorded, `tests/test_live_cassettes.py` replays them offline like any other cassette.
These recordings are only ever made by the repository owner with their own keys.
