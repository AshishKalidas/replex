# pytest plugin

Installing `replex` registers the plugin through the `pytest11` entry point; there is nothing
to enable. Tests without the marker or the fixture are untouched.

## Marker and fixture

```python
import pytest

@pytest.mark.replex(cassette="refund_request")
def test_refund_request(replex_session):
    answer = run_agent("I want a refund for order 1042")
    replex_session.trace.assert_no_tool("delete_*")
    assert "refund" in answer.lower()
```

**Marker arguments** (all optional):

| Argument | Meaning |
|---|---|
| `cassette` | Cassette name. Defaults to the test's node id after the file, sanitised (`test_refund[fast]` → `test_refund_fast`) |
| `match` | `strict` or `sequence` for this test ([CONCEPTS.md](CONCEPTS.md#matching)) |
| `tools` | `replay`, `live` or `auto` for this test |
| `policy` | Drift policy file for this test |

An unknown argument is a usage error, so typos do not pass silently.

**Fixture** `replex_session` yields the active `replex.Session`. `replex_session.trace` holds the
trace of what ran so far (call it after the agent has run); `replex_session.mode` tells you the
resolved mode, which is handy for choosing a backend:

```python
@pytest.fixture
def model_url(replex_session):
    if replex_session.mode == "replay":
        return "http://127.0.0.1:9"        # nothing listens: proof that replay is offline
    return os.environ["MODEL_URL"]
```

Using the fixture without the marker also starts a session, named after the test. Parametrised
tests can carry a different cassette per case with `pytest.param(..., marks=pytest.mark.replex(cassette=...))`.

## Command-line options

| Option | Effect |
|---|---|
| `--replex-mode=MODE` | `replay` (default), `record`, `drift`, `rewrite` or `live` |
| `--replex-dir=DIR` | Baseline cassette directory |
| `--replex-policy=FILE` | Drift policy file |
| `--replex-overwrite` | Let `record` replace existing cassettes |
| `--replex-report=PATH` | After a drift run, write a report for the drifted cassettes; `.html` (single file), `.md` or `.json` by suffix |
| `--replex-repeat=N` | Drift mode only: run each RepleX test N times and report how consistent it is ([CONSISTENCY.md](CONSISTENCY.md)) |

## What each mode does in a test run

- **replay**: every marked test is served from its cassette. A missing cassette fails with
  `CassetteNotFound` and tells you how to record it. A changed request fails with
  `ReplayMismatch`; recordings the test never requested fail with `UnusedRecording`.
- **record**: tests run live and write their cassettes. Existing cassettes are kept unless you
  pass `--replex-overwrite` (`CassetteExists`).
- **drift**: tests run live, write a **candidate** to `candidate_dir`, and compare it with the
  baseline under the policy. The Markdown drift report is attached to the test report (shown
  with `-rA` or on failure as the "replex drift" section). A `fail` finding fails the test with
  `DriftDetected`, listing the findings, the candidate path and the approve command. Warnings
  do not fail; they are counted in the summary.
- **rewrite**: tests run live and replace their baselines. Use it to re-record on purpose.
- **live**: RepleX stays out of the way.

Every run with marked tests ends with a summary section:

```text
==================================== RepleX ====================================
mode: drift | replayed: 0 | recorded: 0 | drift passed: 2 | drift failed: 2 | warnings: 1
```

## Configuration

Settings come from `[tool.replex]` in the nearest `pyproject.toml`, then `REPLEX_*` environment
variables, then pytest options, then marker or `session()` arguments (highest). Relative paths
resolve against the directory holding `pyproject.toml`.

```toml
[tool.replex]
cassette_dir = "tests/cassettes"
candidate_dir = ".replex/candidates"
mode = "replay"
match = "strict"
# tools = "replay"                        # default: replay in replay mode, auto in drift
intercept_hosts = ["api.anthropic.com"]
passthrough_hosts = ["localhost", "127.0.0.1"]
ignore_request_fields = ["metadata.request_id"]
redact_patterns = []                      # see docs/REDACTION.md
redact_paths = []
policy = "replex.policy.yaml"
history = ".replex/history.jsonl"         # false turns drift history off (docs/HISTORY.md)
strict_unused = true
prices = {}                               # $ per million tokens; see docs/COST.md
```

| Environment variable | Setting |
|---|---|
| `REPLEX_MODE` | `mode` |
| `REPLEX_CASSETTE_DIR` | `cassette_dir` |
| `REPLEX_CANDIDATE_DIR` | `candidate_dir` |
| `REPLEX_MATCH` | `match` |
| `REPLEX_TOOLS` | `tools` |
| `REPLEX_POLICY` | `policy` |
| `REPLEX_HISTORY` | `history` (`off` disables it) |
| `REPLEX_ALLOW_RECORD_IN_CI` | `1` lets `record` / `rewrite` run when `CI` is set |
| `REPLEX_GIT_SHA` | Overrides the git sha stored in `meta.git_sha` and the drift history (empty string: none) |
| `REPLEX_GIT_BRANCH` | Overrides the branch stored in the drift history (empty string: none) |

Unknown keys and invalid values are rejected with a `ConfigError` naming the field. To load the
same configuration outside pytest: `replex.load_config()`.

## pytest-xdist

The plugin works with `pytest -n auto`. Each test writes only its own cassette, and every write
is atomic, so workers never produce a torn file. Outcomes travel back to the controller in the
test reports, so the summary counts are correct across workers. Two tests that **record** the
same cassette name in one run would race; give them different names.

## CI recipes

**Replay on every push** (no secrets needed, the default mode):

```yaml
- run: pip install -e ".[dev]"
- run: pytest                      # replay: blocked network, no key
- run: replex redact --check tests/cassettes
```

**Drift check on demand or nightly**, with a key, uploading the report:

```yaml
- run: pytest --replex-mode=drift --replex-report=replex-report.html
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
- uses: actions/upload-artifact@v4
  if: always()
  with: {name: replex-report, path: replex-report.html}
```

**Guard against accidental recording**: with `CI` set (GitHub Actions sets it), `record` and
`rewrite` are refused, so a misconfigured job cannot silently replace baselines. Recording is a
local, reviewed action: record, inspect with `replex show` or the console, commit.

A complete workflow, including the Markdown job summary, is in [CI.md](CI.md).
