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

## The GitHub Action: a behaviour review on every pull request

The repository is also a composite action that runs the steps above and reports on the pull
request itself. Copy this into `.github/workflows/agent-review.yml`:

```yaml
name: Agent review

on:
  pull_request:

permissions:
  contents: read

jobs:
  replay:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write      # only for the comment
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: AshishKalidas/replex@main     # pin a tag or commit SHA in real use
        with:
          mode: replay
          install: pip install -e ".[test]"
```

The action installs Python, RepleX (from the action's own checkout) and pytest, runs your
`install` command, then:

1. runs `pytest` (`pytest --replex-mode=drift` in drift mode) and records the exit code;
2. runs `replex report BASELINE CANDIDATES --compact` to write the HTML report, a JSON report
   and a comment-sized Markdown summary;
3. uploads the HTML and JSON reports (and, in drift mode, the candidates) as the
   `replex-report` artifact;
4. appends the summary to the job summary page;
5. posts the summary as a pull request comment, or edits its earlier comment in place: the
   comment starts with a hidden `<!-- replex-agent-review: WORKING-DIRECTORY -->` marker, so
   each project in a repository keeps exactly one comment however often the job re-runs;
6. fails the step if the tests failed, or if a scenario drifted and `fail-on-drift` is true.

| Input | Default | Meaning |
|---|---|---|
| `mode` | `replay` | `replay` (offline, from the recordings) or `drift` (live, compared with them) |
| `working-directory` | `.` | Project directory; pytest runs here and paths are relative to it |
| `baseline-dir` | `tests/cassettes` | Recorded cassettes |
| `candidate-dir` | `.replex/candidates` | Where drift runs write their candidates |
| `policy` | project setting | Drift policy file |
| `python-version` | `3.12` | Python to install |
| `install` | none | Command that installs your project and its test dependencies |
| `pytest-args` | none | Extra pytest arguments, split on whitespace |
| `fail-on-drift` | `true` | With `false`, a drift run that fails only because of drift passes |
| `comment` | `true` | Post or update the pull request comment |
| `title` | `RepleX behaviour review` | Heading of the summary and the comment |
| `artifact-name` | `replex-report` | Artifact name; make it unique per job in a matrix |
| `github-token` | `github.token` | Token for the comment, with `pull-requests: write` |

Outputs: `drifted` (`true` or `false`) and `report-path` (the HTML report on the runner).

**Replay mode** needs no secrets, so it is safe for pull requests from forks. Their
`GITHUB_TOKEN` is read-only, and the action skips the comment for them (and for any event that
is not `pull_request`); the job summary and the artifact still have the results. Nothing runs
live in replay mode, so the comment reports the test result and lists the recorded scenarios.
Do not switch to `pull_request_target` to get comments on forks: it would run fork code with
a writable token.

**Drift mode** runs live, so pass the provider key to the step and keep it out of pull requests
from forks, for example on a schedule:

```yaml
on:
  schedule:
    - cron: "0 3 * * *"
jobs:
  drift:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: AshishKalidas/replex@main
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        with:
          mode: drift
          install: pip install -e ".[test]"
          comment: "false"
```

The comment has one row per scenario with its verdict in plain words (behaves as recorded,
changed within policy, or behaviour changed), the findings of each changed scenario in a
collapsed block, and links to the run for the full report. GitHub limits a comment to 65,536
characters; a longer summary drops whole rows and blocks from the end and says so. Locally:

```bash
replex report tests/cassettes .replex/candidates --markdown comment.md --compact \
  --title "Agent review" --link https://example.com/full-report --max-chars 20000
```

[`.github/workflows/agent-review.yml`](../.github/workflows/agent-review.yml) runs the action
on this repository's example agent in replay mode, with a commented, opt-in nightly drift job.

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


