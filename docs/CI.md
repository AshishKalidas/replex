# RepleX in CI

Two jobs, with different jobs to do:

| Job | When | Needs secrets | Proves |
|---|---|---|---|
| **Replay** | Every push and pull request | No | Your code still works against the recorded model behaviour, and no cassette leaks a secret |
| **Drift** | Manually, nightly, or on prompt/model changes | Yes (provider API key) | The live model still behaves the way the baselines say |

Replay is fast, free and deterministic, so it can block merges. Drift calls the real model, so
it costs money and can be flaky; run it on demand or on a schedule, and read its report.

## The workflow

`.github/workflows/replex.yml`:

```yaml
name: RepleX

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:            # "Run workflow" button for an on-demand drift check
  schedule:
    - cron: "0 3 * * *"         # nightly drift check

jobs:
  replay:
    if: github.event_name == 'push' || github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12", cache: pip}
      - run: pip install -e ".[dev]"

      # Replay is the default mode: responses come from the cassettes, any other host raises
      # NetworkBlocked, and no key is configured, so nothing can reach the real API.
      - name: Replay agent tests
        run: pytest

      - name: No secrets in committed cassettes
        run: replex redact --check tests/cassettes

  drift:
    if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'
    runs-on: ubuntu-latest
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12", cache: pip}
      - run: pip install -e ".[dev]"

      # Run every scenario live, write candidates and compare them with the baselines.
      # Don't stop on failure: the report is the point of this job.
      - name: Drift check
        id: drift
        continue-on-error: true
        run: pytest --replex-mode=drift

      - name: Build reports
        if: always()
        run: |
          replex report tests/cassettes .replex/candidates \
            --html replex-report.html --markdown replex-report.md
          cat replex-report.md >> "$GITHUB_STEP_SUMMARY"

      - name: Upload HTML report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: replex-report
          path: |
            replex-report.html
            .replex/candidates/

      - name: Fail on drift
        if: steps.drift.outcome == 'failure'
        run: |
          echo "::error::Agent behaviour drifted. Download the replex-report artifact."
          exit 1
```

What each piece does:

- **`pytest`** in the replay job runs in `replay` mode (the default). A missing cassette, a
  changed request or an unexpected tool call fails the test with an explanation.
- **`replex redact --check`** exits 1 if any committed cassette contains a credential or
  configured personal data ([REDACTION.md](REDACTION.md)).
- **`pytest --replex-mode=drift`** runs live and fails any test whose candidate violates a
  `fail` check of the policy ([DRIFT.md](DRIFT.md)).
- **`replex report`** writes one self-contained HTML file covering every pair (it opens
  straight from the downloaded artifact, no server) and a Markdown summary that the job
  appends to `$GITHUB_STEP_SUMMARY`, so the findings table is on the run's summary page.
- The candidates are uploaded too: if the new behaviour is right, download them, copy them
  over the baselines (or run the drift locally and `replex approve <name>`), and commit.

If you prefer a single step, `replex report ... --fail-on-drift` exits 1 when any pair fails
its policy, and `pytest --replex-mode=drift --replex-report=replex-report.html` writes the HTML
report for the drifted cassettes at the end of the test run.

## Tools during drift runs

In drift mode tools default to `auto`: a call whose name and arguments match the baseline is
replayed; a new call executes only if the tool is marked `@replex.tool(safe=True)`, and
otherwise fails with `UnrecordedToolCall`. A drift job therefore never triggers side effects
(refunds, emails, writes) that the baseline did not already contain. If your read-only tools
need credentials or services, provide them to the drift job, or leave them unmarked so that
new calls fail visibly.

## CI must never create baselines

GitHub Actions sets `CI=true`. With `CI` set, `record` and `rewrite` modes and
`replex approve` are refused with `RecordingRefused`, so a misconfigured job cannot replace
your baselines with whatever the model said tonight. Baselines are recorded locally, reviewed
in the pull request as a cassette diff, and committed. If a job is genuinely meant to record,
set `REPLEX_ALLOW_RECORD_IN_CI=1` on it explicitly.

## This repository's own CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs lint, `mypy --strict`, the test
suite with its 90% coverage gate, the example's replay tests, the redaction check and
`make demo` on Python 3.10 to 3.13; the console's vitest suite and build (checking the
committed build is current); and builds the wheel and the Docker image.
