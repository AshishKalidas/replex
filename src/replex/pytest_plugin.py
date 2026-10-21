"""pytest plugin: ``@pytest.mark.replex`` and the ``replex_session`` fixture.

Registered through the ``pytest11`` entry point, so installing replex is enough. Tests without
the marker (and without the fixture) are untouched.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from replex.cassette.storage import sanitise_name
from replex.config import MODES, Config, check_ci_guard, load_config
from replex.errors import ConfigError, DriftDetected, RecordingRefused
from replex.session import Session

_SESSION_KEY = pytest.StashKey[Session]()
_CONFIG_KEY = pytest.StashKey[Config]()
_PROP = "replex"


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
    config.stash[_CONFIG_KEY] = resolved
    config.pluginmanager.register(_Summary(config), "replex-summary")


def _replex_config(config: pytest.Config) -> Config:
    return config.stash[_CONFIG_KEY]


def _start(item: pytest.Item) -> Session:
    existing = item.stash.get(_SESSION_KEY, None)
    if existing is not None:
        return existing
    marker = item.get_closest_marker("replex")
    kwargs: dict[str, Any] = dict(marker.kwargs) if marker else {}
    unknown = set(kwargs) - {"cassette", "match", "tools", "policy"}
    if unknown:
        raise pytest.UsageError(f"unknown replex marker argument(s): {sorted(unknown)}")
    name = kwargs.get("cassette") or sanitise_name(item.nodeid.split("::", 1)[-1])
    session = Session(
        name,
        config=_replex_config(item.config),
        match=kwargs.get("match"),
        tools=kwargs.get("tools"),
        policy=kwargs.get("policy"),
        overwrite=bool(item.config.getoption("--replex-overwrite")),
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

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if not self.counts:
            return
        c = self.counts
        terminalreporter.section("RepleX")
        terminalreporter.write_line(
            f"mode: {_replex_config(self.config).mode} | replayed: {c['replayed']} | "
            f"recorded: {c['recorded']} | drift passed: {c['drift_passed']} | "
            f"drift failed: {c['drift_failed']} | warnings: {c['warnings']}"
        )
        path = self.config.getoption("--replex-report")
        if path and self.drift_names:
            from replex.drift.output import write_drift_report

            written = write_drift_report(_replex_config(self.config), self.drift_names, Path(path))
            terminalreporter.write_line(f"drift report: {written}")

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        # Worker processes (xdist) report through user_properties; nothing to do here.
        return None
