"""Behaviour consistency: repeated runs grouped by path, stored, reported and served."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from consistency_agent import agent, varying
from rich.console import Console
from tracekit import make_trace

import replex
from replex.cli import cli
from replex.config import Config, load_config
from replex.consistency import (
    analyse,
    analyse_runs,
    json_field,
    list_runs,
    prune_runs,
    run_path,
    runs_dir,
    scenarios_with_runs,
)
from replex.consistency.render import (
    consistency_markdown,
    render_consistency,
    summary_lines,
    verdict,
)
from replex.errors import ConfigError
from replex.testing import MockProvider
from replex.trace.model import Trace

pytest_plugins = ["pytester"]

POLICY = ("get_order", {}), ("check_refund_policy", {})
DOCS = ("get_order", {}), ("search_docs", {})


def runs_of(
    *paths: tuple[tuple[str, dict[str, Any]], ...], answers: list[str] | None = None
) -> list[Trace]:
    answers = answers or ["Done."] * len(paths)
    return [
        make_trace("refund", *path, f"model:{answer}", tokens=10 * (i + 1), latency=100 * (i + 1))
        for i, (path, answer) in enumerate(zip(paths, answers, strict=True))
    ]


# ------------------------------------------------------------------ analysis


def test_groups_runs_by_tool_path_most_common_first() -> None:
    runs = runs_of(DOCS, POLICY, POLICY, DOCS, POLICY)
    report = analyse(runs, baseline=runs[1])
    assert report.name == "refund" and report.runs == 5 and report.distinct_paths == 2
    first, second = report.variants
    assert first.tools == ["get_order", "check_refund_policy"]
    assert first.runs == ["run-02", "run-03", "run-05"] and first.count == 3
    assert first.share == pytest.approx(0.6) and first.matches_baseline
    assert second.tools == ["get_order", "search_docs"] and not second.matches_baseline
    assert report.most_common == first.tools
    assert report.baseline_tools == first.tools and report.baseline_share == pytest.approx(0.6)
    assert report.baseline_is_majority is True and not report.consistent


def test_ties_keep_first_seen_order_and_minority_baseline() -> None:
    runs = runs_of(DOCS, POLICY, (("get_order", {}),), POLICY, DOCS)
    report = analyse(runs, baseline=runs_of((("get_order", {}),))[0])
    assert [v.tools[-1] for v in report.variants] == [
        "search_docs",
        "check_refund_policy",
        "get_order",
    ]
    assert report.baseline_share == pytest.approx(0.2) and report.baseline_is_majority is False


def test_half_is_not_a_majority_and_unseen_baseline_has_zero_share() -> None:
    runs = runs_of(DOCS, POLICY)
    assert analyse(runs, baseline=runs[0]).baseline_is_majority is False
    unseen = analyse(runs, baseline=runs_of(((("delete_order", {})),))[0])
    assert unseen.baseline_share == 0 and not any(v.matches_baseline for v in unseen.variants)


def test_without_baseline_and_with_labels_and_name() -> None:
    report = analyse(runs_of(POLICY, POLICY), labels=["a", "b"], name="custom")
    assert report.consistent and report.name == "custom"
    assert report.variants[0].runs == ["a", "b"]
    assert report.baseline_tools is None and report.baseline_share is None
    assert report.baseline_is_majority is None


def test_spreads_of_tokens_latency_and_steps() -> None:
    report = analyse(runs_of(POLICY, POLICY, DOCS))
    assert (report.tokens.min, report.tokens.median, report.tokens.max) == (20, 40.0, 60)
    assert (report.latency_ms.min, report.latency_ms.max) == (102, 302)
    assert report.steps.min == report.steps.max == 3


def test_empty_runs_and_label_mismatch_are_errors() -> None:
    with pytest.raises(ValueError, match="no runs"):
        analyse([])
    with pytest.raises(ValueError, match="1 labels for 2 runs"):
        analyse(runs_of(POLICY, POLICY), labels=["only"])


def test_json_field_categorises_final_answers() -> None:
    decision = json_field("decision")
    answers = [
        '{"decision": "refund_requested"}',
        '```json\n{"decision": "refund_denied"}\n```',
        "Sure, done.",
        '{"other": 1}',
        '["decision"]',
        '{"decision": {"code": 2}}',
    ]
    assert [decision(t) for t in runs_of(*[POLICY] * 6, answers=answers)] == [
        "refund_requested",
        "refund_denied",
        None,
        None,
        None,
        '{"code": 2}',
    ]


def test_categories_split_a_path() -> None:
    answers = ['{"decision": "a"}', '{"decision": "b"}', '{"decision": "a"}']
    runs = runs_of(POLICY, POLICY, POLICY, answers=answers)
    report = analyse(runs, baseline=runs[1], categorise=json_field("decision"))
    assert report.categorised and report.distinct_paths == 2
    assert [(v.category, v.count, v.matches_baseline) for v in report.variants] == [
        ("a", 2, False),
        ("b", 1, True),
    ]
    assert report.baseline_category == "b" and report.baseline_is_majority is False
    data = json.loads(report.to_json())
    assert data["variants"][0]["category"] == "a" and data["tokens"]["max"] == 60


# ------------------------------------------------------------------ rendering


@pytest.mark.parametrize(
    ("paths", "baseline", "expected"),
    [
        ((POLICY, POLICY), None, "All 2 runs took the same path."),
        ((POLICY, POLICY), POLICY, "All 2 runs took the same path, the recorded one."),
        ((POLICY, POLICY), DOCS, "All 2 runs took the same path, not the recorded one."),
        ((POLICY, DOCS), None, "2 runs took 2 different paths."),
        (
            (POLICY, DOCS, POLICY),
            POLICY,
            "3 runs took 2 different paths. 2 of 3 took the recorded path (the majority).",
        ),
        (
            (POLICY, DOCS, DOCS),
            POLICY,
            "3 runs took 2 different paths. 1 of 3 took the recorded path (not a majority).",
        ),
        (
            (POLICY, DOCS),
            (("get_order", {}),),
            "2 runs took 2 different paths. None took the recorded path.",
        ),
    ],
)
def test_verdict_sentences(paths: Any, baseline: Any, expected: str) -> None:
    base = runs_of(baseline)[0] if baseline else None
    assert verdict(analyse(runs_of(*paths), baseline=base)) == expected


def test_summary_lines_markdown_and_terminal() -> None:
    runs = runs_of(POLICY, DOCS, POLICY)
    report = analyse(runs, baseline=runs[0])
    assert summary_lines(report) == [
        "refund: 3 runs took 2 different paths. 2 of 3 took the recorded path (the majority).",
        "  2/3  get_order → check_refund_policy  (recorded)",
        "  1/3  get_order → search_docs",
    ]
    md = consistency_markdown([report])
    assert (
        "### refund" in md
        and "| 2/3 | 67% | `get_order` → `check_refund_policy` | recorded |" in md
    )
    assert "tokens 20 to 60 (median 40) · latency 102 ms to 302 ms (median 202 ms) · steps 3" in md
    assert "No repeated runs found." in consistency_markdown([])

    console = Console(record=True, width=120)
    render_consistency(report, console)
    render_consistency(analyse(runs_of(()), name="quiet"), console)
    text = console.export_text()
    assert "refund  ·  3 runs" in text and "67%" in text and "recorded" in text
    assert "(no tools)" in text
    categorised = analyse(runs_of(POLICY, answers=['{"d": "x"}']), categorise=json_field("d"))
    assert summary_lines(categorised)[1] == "  1/1  get_order → check_refund_policy ⇒ x"


# ------------------------------------------------------------------ storage


def test_run_files_listing_and_pruning(tmp_path: Path) -> None:
    assert run_path(tmp_path, "refund", 3) == tmp_path / "runs/refund/run-03.replex.json"
    with pytest.raises(ValueError, match="start at 1"):
        run_path(tmp_path, "refund", 0)
    assert list_runs(tmp_path, "refund") == [] and scenarios_with_runs(tmp_path) == []
    for n in (2, 100, 1, 10):
        p = run_path(tmp_path, "refund", n)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
    (runs_dir(tmp_path, "refund") / "notes.txt").write_text("not a run")
    runs_dir(tmp_path, "empty").mkdir()
    assert [p.name for p in list_runs(tmp_path, "refund")] == [
        "run-01.replex.json",
        "run-02.replex.json",
        "run-10.replex.json",
        "run-100.replex.json",
    ]
    assert scenarios_with_runs(tmp_path) == ["refund"]
    prune_runs(tmp_path, "refund", keep=2)
    assert [p.name for p in list_runs(tmp_path, "refund")] == [
        "run-01.replex.json",
        "run-02.replex.json",
    ]


# ------------------------------------------------------------------ live runs, offline


@pytest.fixture
def repeated(tmp_path: Path) -> Config:
    """A recorded baseline and five repeated drift runs; the third and fifth skip the policy."""
    cfg = Config(root=str(tmp_path), intercept_hosts=["127.0.0.1"])
    with MockProvider(varying(set())) as mock, replex.session("refund", mode="record", config=cfg):
        agent(mock.url)
    with MockProvider(varying({3, 5})) as mock:
        for n in range(1, 6):
            with replex.session("refund", mode="drift", config=cfg, run_number=n) as s:
                agent(mock.url)
            assert s.outcome == "sampled" and s.drift_report is None
            assert s.written_path == run_path(cfg.candidate_path, "refund", n)
    return cfg


def test_repeated_session_runs_are_stored_and_analysed(repeated: Config) -> None:
    assert not (repeated.candidate_path / "refund.replex.json").exists()
    report = analyse_runs(repeated.candidate_path, "refund", baseline_dir=repeated.cassette_path)
    assert report is not None
    assert [(v.tools[-1], v.runs, v.matches_baseline) for v in report.variants] == [
        ("check_refund_policy", ["run-01", "run-02", "run-04"], True),
        ("search_docs", ["run-03", "run-05"], False),
    ]
    assert report.baseline_is_majority is True
    alone = analyse_runs(repeated.candidate_path, "refund", baseline_dir=repeated.path("none"))
    assert alone is not None and alone.baseline_share is None
    assert analyse_runs(repeated.candidate_path, "other") is None


def test_run_number_needs_drift_mode(tmp_path: Path) -> None:
    cfg = Config(root=str(tmp_path))
    with pytest.raises(ConfigError, match="needs mode='drift'"):
        replex.session("refund", mode="record", config=cfg, run_number=1)
    with pytest.raises(ConfigError, match="needs mode='drift'"):
        replex.session("refund", mode="drift", config=cfg, run_number=0)


def test_a_crashed_repeated_run_is_kept_as_an_error(repeated: Config) -> None:
    session = replex.session("refund", mode="drift", config=repeated, run_number=6)
    with pytest.raises(RuntimeError), session:
        raise RuntimeError("agent crashed")
    assert session.outcome == "error"
    assert run_path(repeated.candidate_path, "refund", 6).is_file()


# ------------------------------------------------------------------ CLI


def cli_run(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_cli_reports_consistency(repeated: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(repeated.root)
    (root / "pyproject.toml").write_text("[tool.replex]\n")
    monkeypatch.chdir(root)
    assert load_config().candidate_path == repeated.candidate_path

    res = cli_run("consistency")
    assert res.exit_code == 0
    assert "5 runs took 2 different paths. 3 of 5 took the recorded path (the majority)." in (
        res.output
    )
    assert "get_order → search_docs" in res.output

    data = json.loads(cli_run("consistency", "refund", "--json").output)
    assert data[0]["distinct_paths"] == 2 and data[0]["variants"][1]["runs"] == ["run-03", "run-05"]
    by_field = json.loads(cli_run("consistency", "--by-field", "decision", "--json").output)
    assert by_field[0]["variants"][0]["category"] == "refund_requested"
    assert "## RepleX consistency" in cli_run("consistency", "--markdown").output

    res = CliRunner().invoke(cli, ["consistency", "nope"])
    assert res.exit_code == 2 and "no repeated runs of 'nope'" in res.output
    empty = cli_run("consistency", "--candidates", str(root / "elsewhere"))
    assert "no repeated runs in" in empty.output


def test_cli_shows_several_scenarios(repeated: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    second = run_path(repeated.candidate_path, "again", 1)
    second.parent.mkdir(parents=True)
    second.write_text(run_path(repeated.candidate_path, "refund", 1).read_text())
    monkeypatch.chdir(repeated.root)
    res = cli_run("consistency", "--baselines", str(repeated.cassette_path))
    assert res.output.index("again") < res.output.index("refund  ·")
    assert "All 1 runs took the same path." in res.output


# ------------------------------------------------------------------ console


def test_console_serves_consistency(repeated: Config) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from replex.console.server import create_app, project_workspace

    app = create_app([project_workspace(repeated.cassette_path, repeated.candidate_path)])
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    data = client.get("/api/w/project/consistency/refund").json()
    assert data["name"] == "refund" and data["report"]["runs"] == 5
    assert data["report"]["variants"][0]["matches_baseline"] is True
    again = client.get("/api/w/project/consistency/refund").json()
    assert again == data
    assert client.get("/api/w/project/consistency/other").json()["report"] is None
    assert client.get("/api/w/project/consistency/..").status_code in (400, 404)
    assert client.get("/api/w/project/consistency/a%20b").status_code == 400

    run_path(repeated.candidate_path, "refund", 2).write_text("{}")
    assert client.get("/api/w/project/consistency/refund").status_code == 422


# ------------------------------------------------------------------ pytest --replex-repeat

REPEAT_PYPROJECT = """
[tool.pytest.ini_options]
asyncio_default_fixture_loop_scope = "function"

[tool.replex]
cassette_dir = "cassettes"
intercept_hosts = ["127.0.0.1"]
"""

REPEAT_TEST = """
import os, pytest
from consistency_agent import agent, varying
from replex.testing import MockProvider

SKIP = {int(n) for n in os.environ.get("SKIP_POLICY_ON", "").split(",") if n}
MOCK = MockProvider(varying(SKIP))

@pytest.fixture(scope="session")
def url():
    with MOCK as mock:
        yield mock.url

@pytest.mark.replex(cassette="refund")
def test_refund(replex_session, url):
    agent(url)

@pytest.mark.parametrize("case", ["a"])
def test_named_by_id(replex_session, url, case):
    agent(url)

def test_unnamed(replex_session, url):
    agent(url)

def test_plain():
    pass
"""


@pytest.fixture
def repeat_project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyprojecttoml(REPEAT_PYPROJECT)
    pytester.makepyfile(test_repeat=REPEAT_TEST)
    pytester.runpytest("--replex-mode=record", "test_repeat.py").assert_outcomes(passed=4)
    return pytester


def test_pytest_repeat_runs_each_scenario_n_times(
    repeat_project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKIP_POLICY_ON", "2")
    res = repeat_project.runpytest(
        "--replex-mode=drift", "--replex-repeat=3", "test_repeat.py", "-v"
    )
    res.assert_outcomes(passed=10)
    res.stdout.fnmatch_lines(
        [
            "*test_refund?run-01? PASSED*",
            "*test_named_by_id?a-run-03? PASSED*",
            "*repeated runs: 9*",
            "*consistency (details: replex consistency):*",
            "*refund: 3 runs took 2 different paths. 2 of 3 took the recorded path (the majority).",
            "*2/3  get_order → check_refund_policy  (recorded)",
            "*test_named_by_id_a: All 3 runs took the same path, the recorded one.",
            "*test_unnamed: All 3 runs took the same path, the recorded one.",
        ]
    )
    candidates = Path(repeat_project.path) / ".replex/candidates"
    assert scenarios_with_runs(candidates) == ["refund", "test_named_by_id_a", "test_unnamed"]
    assert not (candidates / "refund.replex.json").exists()

    repeat_project.runpytest(
        "--replex-mode=drift", "--replex-repeat=2", "-k", "refund"
    ).assert_outcomes(passed=2)
    assert [p.name for p in list_runs(candidates, "refund")] == [
        "run-01.replex.json",
        "run-02.replex.json",
    ]


def test_pytest_repeat_needs_drift_mode(repeat_project: pytest.Pytester) -> None:
    res = repeat_project.runpytest("--replex-repeat=3")
    assert res.ret == pytest.ExitCode.USAGE_ERROR
    res.stderr.fnmatch_lines(["*--replex-repeat=N needs N >= 1 and --replex-mode=drift*"])
    res = repeat_project.runpytest("--replex-mode=drift", "--replex-repeat=0")
    assert res.ret == pytest.ExitCode.USAGE_ERROR
