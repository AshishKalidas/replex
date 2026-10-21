# Drift history

A drift check overwrites the previous candidate, so on its own it only answers "does the agent
behave as recorded right now?". The drift history answers the follow-up questions: when did this
scenario start behaving differently, how often does it pass, and what did the reviewer decide?

Every completed drift comparison, from `pytest --replex-mode=drift` or from
`replex.session(..., mode="drift")`, appends one line to the history file. Accepting a change
(`replex approve`, or **Accept** in the console) and rejecting one (**Reject** in the console)
append a decision line. Nothing else writes to it: replay, record and rewrite runs do not, and a
drift run whose agent raised is not a comparison, so it is not recorded either.

## Where it lives

```toml
[tool.replex]
history = ".replex/history.jsonl"   # the default, relative to the project root
# history = false                   # turn it off
```

`REPLEX_HISTORY` overrides the setting (`REPLEX_HISTORY=off` disables it). `.replex/` is meant to
stay out of version control; point `history` somewhere else if you want to keep it, for example
in a CI cache.

## What a line holds

One JSON object per line (`v` is the line format version):

```json
{"v":1,"at":"2026-10-21T09:15:02Z","name":"refund_request","git_sha":"13af83e","branch":"main",
 "kind":"check","verdict":"failed","model":"mock-sonnet","baseline_model":"mock-sonnet",
 "fail":{"tool_sequence":1,"new_tools":1},"warn":{},
 "tools":["get_order","search_docs","create_refund_request"],"steps":7,"tokens":1302,"latency_ms":1}
```

| Field | Meaning |
|---|---|
| `at` | When the check ran (UTC) |
| `name` | The scenario (cassette) name |
| `git_sha`, `branch` | The commit and branch checked out, when the project is a git repository |
| `kind` | `check`, `accepted` or `rejected` |
| `verdict` | `passed` or `failed` under the policy (checks only) |
| `model`, `baseline_model` | The model of the latest run and of the recording |
| `fail`, `warn` | How many `fail` / `warn` findings each policy check produced |
| `tools`, `steps`, `tokens`, `latency_ms` | The latest run's tool calls in order, and its totals |
| `via` | `cli` or `console` (decisions only) |

Lines never contain finding messages, tool arguments, results or model output, only check names,
tool names and numbers, so nothing the redactor would have to catch can reach the file.
`REPLEX_GIT_SHA` and `REPLEX_GIT_BRANCH` override the git details (an empty value stores none).

## Safe under pytest-xdist

Each append takes an exclusive lock on `history.jsonl.lock` (next to the file), then checks the
size, rotates if needed and writes the line in one call. Parallel xdist workers, or several
pytest runs at once, never interleave or lose lines. Readers skip any line that does not parse,
such as one cut short by a crash.

## Size limit

When an append would take the file past **1 MB** it is renamed to `history.jsonl.1`, replacing
any older one, and a new file is started. Reading combines both, so the history covers the most
recent 1 to 2 MB: several thousand checks. Nothing older is kept.

## `replex history`

```console
$ replex history
8 check(s) in the 7 days to 2026-10-21 09:15: 6 passed, 2 changed behaviour; 0 accepted, 0 rejected
 scenario                     pass rate   recent   last check         last change        decision
 refund_request               50% of 2    ✓ ✗      2026-10-21 09:15   2026-10-21 09:15   -
 refund_ineligible_openai     50% of 2    ✓ ✗      2026-10-21 09:15   2026-10-21 09:15   -
 order_status_stream          100% of 2   ✓ ✓      2026-10-21 09:15   -                  -
 order_status_openai_stream   100% of 2   ✓ ✓      2026-10-21 09:15   -                  -
✓ passed   ✗ behaviour changed   A accepted   R rejected
```

- `replex history NAME` shows one scenario as a timeline: each check with the policy checks that
  failed or warned, the tool sequence (or "as before" when it did not change), the model and
  the commit, and each decision.
- `--limit N` (default 20) is how many recent checks per scenario are shown and used for the
  pass rate. Decisions taken since the first of those checks are included.
- `--json` prints the same data for scripts: `trend`, then one object per scenario with
  `checks`, `passed`, `failed`, `pass_rate`, `total_checks`, `last_check_at`,
  `last_change_at`, `last_decision` and `events`.

## In the console

The Overview has a one-line trend under **Scenarios**, for example "8 checks in the 7 days to
21 Oct: 2 found changed behaviour." It covers the seven days up to the newest entry.

Each scenario page has a **History** section: the pass rate of its recent checks, a timeline
with one marker per check or decision (a circle when it behaved as recorded, a triangle when its
behaviour changed, a diamond for an accepted change, a square for a rejected one), and the same
events as a list, newest first. The list is the accessible equivalent of the timeline.

Both come from `GET /api/w/{workspace}/history?name=&limit=`, which returns the same data as
`replex history --json` plus `enabled`. A static HTML report has no history.

## The sample's history

The bundled sample workspace ships a history that was produced by running the example's drift
checks, offline against the mock provider, not written by hand. `make sample` runs
`examples/support_agent/sample_history.py`, which runs the example suite in drift mode twice:
with the original prompt (v1, every scenario behaves as recorded) and with the reworded one (v2,
the two refund flows change). Each check appends its own line, exactly as in a project. The git
details are left empty so the sample does not carry this repository's state.
