# Scenario suites

A suite describes your scenarios in one YAML file: the agent to call, the inputs, and what each
run must satisfy. `replex run` then records, replays and checks them for drift, with no test
code. Suites use the same sessions, cassettes, configuration and policy as the
[pytest plugin](PYTEST.md), so the two work side by side on the same recordings.

## The suite file

```yaml
# replex.suite.yaml
version: 1
agent: support_agent.suite:answer      # package.module:function

kwargs: {provider: anthropic}          # passed to every call (optional)

expect:                                # checks for every scenario (optional)
  no_tools: ["delete_*"]
  max_steps: 10
  final_output: {json_schema: schemas/refund.json}

scenarios:
  - name: refund_request               # also the cassette name
    input: Hi, I'd like a refund for order 1042 please.
    expect:
      tools: [get_order, check_refund_policy, create_refund_request]

  - name: order_status_stream
    input: Where is my order 1043?
    kwargs: {stream: true}             # merged over the suite's kwargs
    expect:
      tools: {sequence: [get_order], mode: subsequence}
      final_output: {json_schema: schemas/refund.json, contains: '"decision": "info"'}
```

| Key | Meaning |
|---|---|
| `version` | `1` (optional) |
| `agent` | The callable to run, as `package.module:function` (a dotted attribute path after the colon works too) |
| `kwargs` | Keyword arguments for every call |
| `expect` | Expectations applied to every scenario |
| `scenarios` | At least one scenario |
| `scenarios[].name` | Unique; it is the cassette name, so letters, digits, `_`, `.` and `-` only |
| `scenarios[].input` | The first argument to the agent: a string or any structured YAML value, passed as is |
| `scenarios[].kwargs` | Keyword arguments for this scenario, merged over the suite's |
| `scenarios[].expect` | Expectations for this scenario; each key it sets replaces the suite's value for that key |

Each scenario calls `agent(input, **kwargs)` inside `replex.session(name)`. The agent may be
sync or async (a coroutine function, or anything returning an awaitable). Its return value is
not used: expectations look at the trace, and its final output is the model's last answer. The
suite file's directory is put on `sys.path`, so modules next to it import without installing.

The agent is responsible for reaching the model in each mode, exactly as a pytest test is.
`replex.current_session().mode` tells it which mode is running.

## Expectations

Each expectation maps onto a trace assertion ([CONCEPTS.md](CONCEPTS.md#traces)), and its
failure message is the assertion's, with the steps that actually ran.

| Key | Assertion |
|---|---|
| `tools: [a, b]` | `assert_tool_sequence([a, b])` (exact) |
| `tools: {sequence: [a, b], mode: subsequence}` | `mode` is `exact`, `subsequence` or `same_set` |
| `no_tools: ["delete_*"]` | `assert_no_tool` for each glob (a single string is accepted) |
| `max_steps: 10` | `assert_max_steps(10)` |
| `max_tokens: 5000` | `assert_max_tokens(5000)` |
| `final_output: {contains:, matches:, json_schema:}` | `assert_final_output`; `matches` is a `re.search` pattern, `json_schema` an inline mapping or a file path relative to the suite file |

Every expectation of a scenario is evaluated, so one run lists all of its failures.

Expectations should usually be invariants: what must hold whatever the wording. The exact
behaviour is pinned by the cassette and checked by the drift policy.

## Running

```bash
replex run                                   # ./replex.suite.yaml, configured mode (replay)
replex run path/to/suite.yaml --mode record  # record every scenario
replex run --mode drift --only refund_request --report replex-report.html
replex run --json                            # machine-readable results
```

| Option | Effect |
|---|---|
| `--mode` | `replay`, `record`, `drift` or `rewrite`. Default: the configured mode |
| `--only NAME` | Run only this scenario; repeat for several |
| `--overwrite` | Let `record` replace existing cassettes |
| `--report PATH` | After a drift run, write a report; `.html` (single file), `.md` or `.json` by suffix |
| `--json` | Print the results as JSON instead of the table |

Configuration comes from the nearest `pyproject.toml` at or above the suite file, then the
`REPLEX_*` environment variables, then `--mode` ([PYTEST.md](PYTEST.md#configuration)).
`live` mode is refused, since nothing would be captured. When `CI` is set, `record` and
`rewrite` are refused unless `REPLEX_ALLOW_RECORD_IN_CI=1`, before any scenario runs.

The command prints one row per scenario (result, session outcome, steps, tools and a short
note), then the full message of every problem, then the totals. It exits 1 if any scenario
failed or errored.

## What each mode does

- **replay**: served from the cassettes, offline. A missing cassette, a changed request or an
  unused recording is an error for that scenario; the others still run.
- **record**: runs live and writes the cassettes; existing ones are kept unless `--overwrite`.
  A run whose expectations fail is still written, with the failure in `meta.error`.
- **drift**: runs live, writes candidates to `candidate_dir` and compares them with the
  baselines under the policy. A `fail` finding fails the scenario and the message names the
  candidate and the `replex approve` command. Failed expectations do not stop the comparison:
  both are reported.
- **rewrite**: runs live and replaces the baselines, unless the agent raised or an expectation
  failed; then the baseline is kept.

A scenario whose agent raises is an error, whatever the mode; the session ends as it would in
a failing test.

## The example

`examples/support_agent/replex.suite.yaml` covers the example's four scenarios and replays the
same cassettes as its pytest suite. Its agent entry point, `support_agent.suite:answer`, points
the agent at a dead address in replay and at RepleX's mock provider in every other mode, so the
whole loop runs offline with no key:

```bash
make suite                                   # replay, from the repository root
cd examples/support_agent
PROMPT_VERSION=v2 replex run --mode drift    # the reworded prompt drifts on two scenarios
replex console                               # review the candidates
```
