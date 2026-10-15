# Diff and drift

Replay proves your code still works against the **recorded** model. Drift answers the other
question: after a prompt, model or orchestration change, **did the agent's behaviour move**, and
by more than you allow?

A drift run executes the scenario live (`pytest --replex-mode=drift`), writes a **candidate**
cassette, and compares it with the **baseline** under a **policy**. Each check produces findings
with a severity: `fail` fails the test (and `replex drift` exits 1), `warn` is reported, `ignore`
switches the check off.

## Diff: aligning two runs

`replex.diff(a, b)` (CLI: `replex diff a.replex.json b.replex.json`) aligns the two step lists
with a longest-common-subsequence alignment keyed on step type and tool name, then labels each
aligned pair:

| Mark | Status | Meaning |
|---|---|---|
| `=` | `same` | Same step with the same content |
| `~` | `changed` | Same kind of step (same tool) with different arguments, text or tool requests; the field changes are listed |
| `-` | `removed` | Only in the baseline |
| `+` | `added` | Only in the candidate |

`diff(a, a)` is all `same` (a property test enforces it). The drift checks below use this
alignment, so an inserted step does not make every later step look different.

## The policy file

```yaml
# replex.policy.yaml
version: 1
defaults:
  tool_sequence: {mode: exact, severity: fail}
  tool_args: {mode: exact, severity: fail, ignore_keys: [request_id]}
  new_tools: {severity: fail}
  forbidden_tools: {patterns: ["delete_*", "send_payment"], severity: fail}
  final_output: {mode: similarity, threshold: 0.80, severity: warn}
  steps: {max_increase: 1, severity: warn}
  tokens: {max_increase_pct: 25, severity: warn}
overrides:
  refund_request:
    final_output: {mode: json_schema, schema: schemas/refund.json, severity: fail}
```

- `defaults` apply to every cassette; `overrides.<cassette name>` are deep-merged on top.
- Relative schema paths resolve against the policy file's directory.
- Unknown keys, bad modes and invalid overrides are rejected when the policy loads
  (`PolicyError`), not halfway through a run.
- Without a policy file, the defaults shown in each section below apply.

Run a comparison from Python with `replex.compare(baseline, candidate, policy)`; baseline and
candidate can be traces, cassettes or paths, and `policy` a `Policy`, a path or `None`.

## Checks

Every example compares against this baseline:

```text
get_order({"order_id": "1042", "request_id": "a"}) → check_refund_policy({"days": 12})
final output: {"decision": "refund_requested", "refund_id": "RF-1"}
```

### `tool_sequence`

Order of tool calls. Default `{mode: exact, severity: fail}`.

| Mode | Passes when |
|---|---|
| `exact` | The candidate calls the same tools in the same order |
| `subsequence` | The baseline's tools appear in order in the candidate; extra tools in between are allowed |
| `same_set` | The same set of tool names, order and repetition ignored |
| `ignore` | Always |

- **Pass** (`same_set`): candidate calls `check_refund_policy → get_order`.
- **Fail** (`exact`): candidate calls `get_order → search_docs`:

  ```text
  fail  tool_sequence: tool sequence changed (exact): ['get_order', 'check_refund_policy'] -> ['get_order', 'search_docs']
  ```

### `tool_args`

Arguments of **aligned** tool calls. Default `{mode: exact, severity: fail}`.

Options: `ignore_keys` (dotted paths dropped before comparing), `tolerance` (absolute tolerance
for numbers), and per-tool settings under `tools`:

```yaml
tool_args:
  mode: exact
  ignore_keys: [request_id]
  tools:
    create_refund_request: {tolerance: 0.01, ignore_keys: [reason]}
```

- **Pass** (`ignore_keys: [request_id]`): `get_order({"order_id": "1042", "request_id": "zz"})`.
- **Pass** (`tolerance: 0.5`): `check_refund_policy({"days": 12.4})`.
- **Fail**: `get_order({"order_id": "1043", ...})`:

  ```text
  fail  tool_args: arguments of 'get_order' changed
  ```

  The finding carries the before and after arguments, which the console and HTML report show as
  an inline diff.

### `new_tools`

Fails when the candidate calls a tool the baseline never called. Default `{severity: fail}`.
This catches a new capability (often a side effect) that a looser `tool_sequence` mode lets
through.

- **Pass**: the candidate only calls `get_order` and `check_refund_policy`, in any order.
- **Fail**: the candidate also calls `search_docs`:

  ```text
  fail  new_tools: candidate calls tool(s) absent from the baseline: ['search_docs']
  ```

### `forbidden_tools`

Glob patterns that must never appear in the candidate, whatever the baseline did. Default: no
patterns.

```yaml
forbidden_tools: {patterns: ["delete_*", "send_payment"], severity: fail}
```

- **Pass**: no called tool matches a pattern.
- **Fail**: the candidate calls `delete_order`:

  ```text
  fail  forbidden_tools: forbidden tool 'delete_order' was called (matches 'delete_*')
  ```

### `final_output`

The final answer. Default `{mode: similarity, threshold: 0.8, severity: warn}`.

| Mode | Passes when |
|---|---|
| `exact` | The strings are identical |
| `json_equal` | Both parse as JSON and are equal (key order and whitespace ignored) |
| `json_schema` | The candidate parses as JSON and validates against `schema` (inline mapping or file path) |
| `similarity` | Similarity ≥ `threshold` |
| `ignore` | Always |

JSON modes tolerate a surrounding Markdown code fence (```` ```json ... ``` ````). Schemas are
checked by a small built-in validator covering the common keywords; if the `jsonschema`
package is installed it is used instead.

Similarity is `difflib.SequenceMatcher` ratio by default. Install the `semantic` extra and set
`similarity_backend: embeddings` (optionally `embedding_model`, default `all-MiniLM-L6-v2`) to
use cosine similarity of local sentence embeddings instead.

- **Pass** (`json_equal`): `{"refund_id": "RF-1", "decision": "refund_requested"}` (keys reordered).
- **Pass** (`similarity`): "Your refund has been requested and *should* arrive in 5-7 business
  days." against "...and *will* arrive...".
- **Fail** (`exact`): the same keys reordered:

  ```text
  fail  final_output: final output changed (exact)
  ```

- **Fail** (`json_schema`): the candidate answers in prose:

  ```text
  fail  final_output: candidate final output is not valid JSON (json_schema)
  ```

- **Warn** (`similarity`): "Done! It's on its way." against the sentence above:

  ```text
  warn  final_output: final output similarity 0.27 is below 0.80
  ```

### `steps`

Maximum increase in the number of steps (model calls plus tool calls). Default
`{max_increase: 1, severity: warn}`. Fewer steps never trigger it.

- **Pass**: the candidate takes 5 or 6 steps where the baseline took 5.
- **Warn**: it takes 8:

  ```text
  warn  steps: step count rose from 5 to 8 (+3, allowed +1)
  ```

### `tokens`

Maximum percentage increase in total tokens (input plus output). Default
`{max_increase_pct: 25, severity: warn}`. Skipped when the baseline recorded no usage.

- **Pass**: 100 → 120 tokens (+20%).
- **Warn**: 100 → 180 tokens:

  ```text
  warn  tokens: tokens rose from 100 to 180 (+80%, allowed +25%)
  ```

### `latency`

Reported, never fails: wall-clock time depends on the network and the machine, not on your
change. Default `{severity: info}`; `severity: ignore` hides it.

```text
info  latency: latency 812 ms -> 1040 ms (+28%) (reported only)
```

## Reports

| Command | Output |
|---|---|
| `replex drift BASELINE CANDIDATE [--policy FILE]` | `rich` panel and aligned table; `--json` or `--markdown` instead. Exit code 1 on failures |
| `replex report BASELINE_DIR CANDIDATE_DIR --html out.html` | One self-contained HTML file for all pairs (also `--markdown`, `--json`; `--fail-on-drift` for a non-zero exit) |
| `pytest --replex-mode=drift --replex-report=out.html` | The same report for the cassettes in this run |
| `replex console` | The local console: an overview of every scenario, a review page per scenario, run timelines and a step-by-step compare |

The HTML report is the same console with the data inlined: it opens from a CI artifact or the
file system with no server and makes no network requests (a test inspects the file for
external URLs).

## The console

`replex console` serves two workspaces, switched from the top bar:

- **Project**: your own recordings, from `[tool.replex]` (or `replex console DIR --candidates
  DIR --policy FILE`). It is read from disk each time you return to the window, so a new
  recording or drift run shows up without restarting anything.
- **Sample**: a complete example that ships inside the package, so you can see the review flow
  before recording anything. `replex console --sample` opens it on its own; `--no-sample`
  hides it.

The **Overview** answers one question: does each scenario still behave as recorded? Each one
gets a verdict (behaviour changed, small differences, behaves as recorded, not re-run yet) and a
sentence describing the difference, such as "Stopped calling check_refund_policy and started
calling search_docs." The **review page** for a scenario adds the recorded and latest runs side
by side, the aligned step-by-step diff, and the raw policy findings.

## Approving new behaviour

If the drift is intended, open the scenario in the console and choose **Accept new behaviour**:
the latest run becomes the recording, and you commit the updated cassette. **Discard latest
run** deletes the candidate and keeps the recording. Both ask for confirmation, work only in
your project (never in the sample), and are refused when `CI` is set.

From the command line, promote the candidate with:

```bash
replex approve refund_request     # copies .replex/candidates/refund_request.replex.json over the baseline
git add tests/cassettes/refund_request.replex.json
```

Or re-record on purpose with `pytest --replex-mode=rewrite`. All of these are refused in CI unless
`REPLEX_ALLOW_RECORD_IN_CI=1`.
