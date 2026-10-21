# Behaviour consistency

A model can answer the same input in different ways, and an agent built on it can take
different paths: check the refund policy on one run, search the docs instead on the next. One
recording shows one of those paths. Before you trust it as the expected behaviour, it helps to
know how often the agent actually takes it.

RepleX measures this by running a scenario several times live, grouping the runs by the tools
they called (in order), and reporting:

- how many **distinct paths** the runs took, and the share of runs on each one;
- the **most common path**;
- whether the **recorded path** (the baseline's) is what a **majority** of runs do;
- the spread (lowest, median, highest) of **tokens**, **latency** and **steps**.

## Running a scenario several times

Repetition is part of a drift run, so tools behave as they do in drift checks (`tools = "auto"`
by default: recorded calls are replayed, new calls run only for `safe=True` tools).

```bash
pytest --replex-mode=drift --replex-repeat=10
```

Every test that uses RepleX (the `replex` marker on the test function, or the `replex_session`
fixture) runs ten times, with ids such as `test_refund[run-03]`. Each run is stored as its own
cassette and is **not** judged by the policy on its own: repeated runs are a measurement, so a
run that takes a different path does not fail the test. The cassette name is the same as
without repetition. The pytest summary ends with one block per scenario:

```text
mode: drift | replayed: 0 | recorded: 0 | drift passed: 0 | drift failed: 0 | warnings: 0 | repeated runs: 10
consistency (details: replex consistency):
  refund_request: 10 runs took 2 different paths. 7 of 10 took the recorded path (the majority).
    7/10  get_order → check_refund_policy → create_refund_request  (recorded)
    3/10  get_order → search_docs → create_refund_request
```

`--replex-repeat` is refused outside drift mode. From Python, pass `run_number` to a drift
session; each number writes one run:

```python
for n in range(1, 11):
    with replex.session("refund_request", mode="drift", run_number=n):
        run_agent("I want a refund for order 1042")
```

### Where runs are stored

```text
.replex/candidates/
  refund_request.replex.json          the latest single drift run (reviewed and approved as usual)
  runs/
    refund_request/
      run-01.replex.json              repeated runs, one cassette each
      run-02.replex.json
      ...
```

Repeated runs never replace the single latest run that the console reviews and `replex approve`
promotes. Running again with fewer repetitions removes the higher-numbered runs left from
before, so the directory always holds one coherent set.

## Reading the result

```bash
replex consistency                       # every scenario with repeated runs
replex consistency refund_request        # one scenario
replex consistency --by-field decision   # also split paths by a field of a JSON answer
replex consistency --json                # or --markdown, for CI job summaries
```

```text
refund_request  ·  5 runs
All 5 runs took the same path, the recorded one.
runs  share  path
 5/5   100%  get_order → check_refund_policy → create_refund_request  recorded
tokens 4711 to 4728 (median 4714) · latency 5756 ms to 6292 ms (median 6064 ms) · steps 7
```

`--by-field` groups by outcome as well as path: with `--by-field decision`, a run that calls the
same tools but answers `{"decision": "refund_denied"}` instead of `refund_requested` counts as a
different path. Answers that are not JSON, or lack the field, form their own group.

"The majority" means more than half of the runs. When the recorded path is not the majority,
the recording is a minority behaviour: drift checks against it will flag changes that are just
the model's usual variation. Record again, or accept a run that took the most common path.

In the console, the scenario page has a **How consistent is it?** section whenever a scenario
has repeated runs. It lists each path in plain words ("7 of 10 runs: Get order › Check refund
policy › Create refund request"), marks the recorded path, and shows the spread of tokens, time
and steps. The data comes from `GET /api/w/<workspace>/consistency/<name>`.

From Python, `replex.consistency.analyse(traces, baseline=...)` works on any list of traces,
and `analyse_runs(candidate_dir, name, baseline_dir=...)` on stored runs; both return a
`ConsistencyReport`.

## A real measurement

The example support agent ran against **Claude Haiku 5.5** (`anthropic/claude-haiku-5.5`,
through OpenRouter's OpenAI-compatible API, prompt v1, not streamed). Each scenario was recorded
once, then run five more times with `run_number` 1 to 5. Tools ran live (`tools="live"`); they
are in-memory fakes in the example, reset before every run.

| Scenario | Runs | Distinct paths | Path (all runs) | Decision | Tokens | Latency |
|---|---:|---:|---|---|---|---|
| `refund_request` | 5 | 1 | `get_order` → `check_refund_policy` → `create_refund_request` | `refund_requested` (5/5) | 4,711 to 4,728 | 5.8 s to 6.3 s |
| `refund_ineligible` | 5 | 1 | `get_order` → `check_refund_policy` | `refund_denied` (5/5) | 3,152 to 3,177 | 4.3 s to 4.8 s |

Both scenarios were fully consistent: every run took the recorded path and reached the recorded
decision. The wording of the answer changed from run to run (five different messages for the
same denial), which is why the example's policy compares final answers by similarity or schema
rather than exactly. Token counts varied by under 1%, latency by about 10%.

Five runs is a small sample: it shows that the recorded path is the usual one, not that other
paths never happen. Use more runs for scenarios where a rare path would be costly. The runs were
made with the session API and are not committed; the test suite measures consistency offline
against a mock model whose replies vary between runs.
