"""pytest plugin: ``@pytest.mark.replex`` and the ``replex_session`` fixture.

Registered through the ``pytest11`` entry point, so installing replex is enough. Tests without
the marker (and without the fixture) are untouched.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from replex.cassette.storage import sanitise_name
from replex.config import MODES, Config, check_ci_guard, load_config
from replex.consistency.runs import prune_runs
from replex.errors import ConfigError, DriftDetected, RecordingRefused
from replex.session import Session

_SESSION_KEY = pytest.StashKey[Session]()
_CONFIG_KEY = pytest.StashKey[Config]()
_PROP = "replex"
_RUN_FIXTURE = "_replex_run"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("replex", "RepleX record-and-replay for AI agents")
    group.addoption(
        "--replex-mode",
        choices=list(MODES),
        default=None,
        help="replay (default), record, drift, rewrite or live",
    )
    group.addoption("--replex-dir", default=None, help="cassette directory (baselines)")
    group.addoption("--replex-policy", default=None, help="drift policy YAML file")
    group.addoption(
        "--replex-overwrite",
        action="store_true",
        default=False,
        help="allow record mode to overwrite existing cassettes",
    )
    group.addoption(
        "--replex-report",
        default=None,
        metavar="PATH",
        help="write a drift report for this run (.html, .md or .json)",
    )
    group.addoption(
        "--replex-repeat",
        type=int,
        default=None,
        metavar="N",
        help="drift mode: run each RepleX test N times and report how consistent it is",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "replex(cassette=None, match=None, tools=None, policy=None): record and replay this "
        "test's model and tool calls with RepleX",
    )
    try:
        resolved = load_config(
            config.rootpath,
            mode=config.getoption("--replex-mode"),
            cassette_dir=config.getoption("--replex-dir"),
            policy=config.getoption("--replex-policy"),
        )
    except ConfigError as exc:
        raise pytest.UsageError(str(exc)) from exc
    try:
        check_ci_guard(resolved.mode)
    except RecordingRefused as exc:
        raise pytest.UsageError(str(exc)) from exc
    repeat = config.getoption("--replex-repeat")
    if repeat is not None and (repeat < 1 or resolved.mode != "drift"):
        raise pytest.UsageError("--replex-repeat=N needs N >= 1 and --replex-mode=drift")
    config.stash[_CONFIG_KEY] = resolved
    config.pluginmanager.register(_Summary(config), "replex-summary")


def _replex_config(config: pytest.Config) -> Config:
    return config.stash[_CONFIG_KEY]


@pytest.hookimpl(trylast=True)
def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """With ``--replex-repeat=N``, run every test that uses RepleX N times.

    ``trylast`` puts the repetition id last in the test id (``test_x[case-run-02]``), so it can
    be removed again when the cassette name is derived from the test id.
    """
    repeat = metafunc.config.getoption("--replex-repeat")
    uses_replex = (
        "replex_session" in metafunc.fixturenames
        or metafunc.definition.get_closest_marker("replex")
    )
    if repeat is None or not uses_replex:
        return
    metafunc.fixturenames.append(_RUN_FIXTURE)
    metafunc.parametrize(
        _RUN_FIXTURE, range(1, repeat + 1), indirect=True, ids=lambda n: f"run-{n:02d}"
    )


@pytest.fixture
def _replex_run(request: pytest.FixtureRequest) -> int:
    """The repetition number under ``--replex-repeat``."""
    number: int = request.param
    return number


def _run_number(item: pytest.Item) -> int | None:
    callspec = getattr(item, "callspec", None)
    return callspec.params.get(_RUN_FIXTURE) if callspec is not None else None


def _default_name(item: pytest.Item, run_number: int | None) -> str:
    test_id = item.nodeid.split("::", 1)[-1]
    if run_number is not None:
        test_id = re.sub(
            rf"(\[|-)run-{run_number:02d}\]$", lambda m: "]" if m[1] == "-" else "", test_id
        )
    return sanitise_name(test_id)


def _start(item: pytest.Item) -> Session:
    existing = item.stash.get(_SESSION_KEY, None)
    if existing is not None:
        return existing
    marker = item.get_closest_marker("replex")
    kwargs: dict[str, Any] = dict(marker.kwargs) if marker else {}
    unknown = set(kwargs) - {"cassette", "match", "tools", "policy"}
    if unknown:
        raise pytest.UsageError(f"unknown replex marker argument(s): {sorted(unknown)}")
    run_number = _run_number(item)
    name = kwargs.get("cassette") or _default_name(item, run_number)
    repeat = item.config.getoption("--replex-repeat")
    if run_number is not None:
        prune_runs(_replex_config(item.config).candidate_path, name, keep=repeat)
    session = Session(
        name,
        config=_replex_config(item.config),
        match=kwargs.get("match"),
        tools=kwargs.get("tools"),
        policy=kwargs.get("policy"),
        overwrite=bool(item.config.getoption("--replex-overwrite")),
        run_number=run_number,
    )
    session.start()
    item.stash[_SESSION_KEY] = session
    return session


@pytest.fixture(autouse=True)
def _replex_marker(request: pytest.FixtureRequest) -> Iterator[None]:
    """Start a session for tests carrying ``@pytest.mark.replex``."""
    item = request.node
    if isinstance(item, pytest.Item) and item.get_closest_marker("replex"):
        _start(item)
    yield
    _close(request.node, None)


@pytest.fixture
def replex_session(request: pytest.FixtureRequest) -> Iterator[Session]:
    """The active RepleX session. ``replex_session.trace`` is available after the agent ran."""
    session = _start(request.node)
    yield session
    _close(request.node, None)


def _close(item: Any, exc: BaseException | None) -> Session | None:
    session: Session | None = item.stash.get(_SESSION_KEY, None) if hasattr(item, "stash") else None
    if session is not None:
        session.finish(exc)
    return session


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    try:
        result = yield
    except BaseException as exc:
        _close(item, exc)
        raise
    session = _close(item, None)
    if session is not None and session.drift_report is not None:
        report = session.drift_report
        item.add_report_section("call", "replex drift", report.to_markdown())
        if not report.passed:
            from replex.drift.output import drift_failure_message

            raise DriftDetected(drift_failure_message(session.name, report, session.written_path))
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    session = item.stash.get(_SESSION_KEY, None)
    if session is not None and report.when == "call" and session.outcome:
        data: dict[str, Any] = {"name": session.name, "outcome": session.outcome}
        if session.drift_report is not None:
            data["warnings"] = len(session.drift_report.warnings)
            data["findings"] = [
                {"check": f.check, "severity": f.severity, "message": f.message}
                for f in session.drift_report.findings
            ]
        report.user_properties.append((_PROP, json.dumps(data)))
    return report


class _Summary:
    """Collects outcomes (also from xdist workers) and prints the RepleX summary section."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.counts: Counter[str] = Counter()
        self.drift_names: list[str] = []
        self.sampled_names: list[str] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when != "call":
            return
        for key, value in report.user_properties:
            if key != _PROP:
                continue
            data = json.loads(str(value))
            self.counts[data["outcome"]] += 1
            self.counts["warnings"] += int(data.get("warnings", 0))
            if data["outcome"].startswith("drift"):
                self.drift_names.append(data["name"])
            elif data["outcome"] == "sampled":
                self.sampled_names.append(data["name"])

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if not self.counts:
            return
        c = self.counts
        terminalreporter.section("RepleX")
        terminalreporter.write_line(
            f"mode: {_replex_config(self.config).mode} | replayed: {c['replayed']} | "
            f"recorded: {c['recorded']} | drift passed: {c['drift_passed']} | "
            f"drift failed: {c['drift_failed']} | warnings: {c['warnings']}"
            + (f" | repeated runs: {c['sampled']}" if c["sampled"] else "")
        )
        if self.sampled_names:
            _write_consistency(_replex_config(self.config), self.sampled_names, terminalreporter)
        path = self.config.getoption("--replex-report")
        if path and self.drift_names:
            from replex.drift.output import write_drift_report

            written = write_drift_report(_replex_config(self.config), self.drift_names, Path(path))
            terminalreporter.write_line(f"drift report: {written}")

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        # Worker processes (xdist) report through user_properties; nothing to do here.
        return None


def _write_consistency(config: Config, names: list[str], terminalreporter: Any) -> None:
    from replex.consistency import analyse_runs
    from replex.consistency.render import summary_lines

    terminalreporter.write_line("consistency (details: replex consistency):")
    for name in sorted(set(names)):
        report = analyse_runs(config.candidate_path, name, baseline_dir=config.cassette_path)
        if report is not None:
            for line in summary_lines(report):
                terminalreporter.write_line(f"  {line}")
