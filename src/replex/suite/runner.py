"""Running a suite: each scenario inside a session, then its expectations and the policy."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from replex.config import Config, check_ci_guard, load_config
from replex.drift.compare import DriftReport
from replex.drift.output import drift_failure_message
from replex.errors import ReplexError
from replex.session import Session
from replex.suite.model import Scenario, Suite, SuiteError

Status = Literal["passed", "failed", "error"]


@dataclass
class ScenarioResult:
    """What happened when one scenario ran."""

    name: str
    status: Status
    outcome: str | None = None
    failures: list[str] = field(default_factory=list)
    error: str | None = None
    drift_report: DriftReport | None = None
    written_path: Path | None = None
    steps: int | None = None
    tools: list[str] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        """Every reason this scenario did not pass, most fundamental first."""
        found = [self.error] if self.error else []
        found += self.failures
        if self.drift_report is not None and not self.drift_report.passed:
            found.append(drift_failure_message(self.name, self.drift_report, self.written_path))
        return found

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready summary."""
        report = self.drift_report
        return {
            "name": self.name,
            "status": self.status,
            "outcome": self.outcome,
            "steps": self.steps,
            "tools": self.tools,
            "failures": self.failures,
            "error": self.error,
            "file": str(self.written_path) if self.written_path else None,
            "drift": report.to_dict() if report is not None else None,
        }


def import_agent(spec: str, search_path: Path) -> Callable[..., Any]:
    """Import ``package.module:function``, with ``search_path`` on ``sys.path``."""
    module_name, _, attr = spec.partition(":")
    root = str(search_path.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        target: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise SuiteError(f"cannot import agent {spec!r}: {exc}") from exc
    for part in attr.split("."):
        try:
            target = getattr(target, part)
        except AttributeError:
            raise SuiteError(f"cannot import agent {spec!r}: no attribute {part!r}") from None
    if not callable(target):
        raise SuiteError(f"agent {spec!r} is not callable")
    return target  # type: ignore[no-any-return]


async def _wait(awaitable: Any) -> Any:
    return await awaitable


def call_agent(agent: Callable[..., Any], value: Any, kwargs: dict[str, Any]) -> Any:
    """Call the agent with a scenario's input; an awaitable result is run to completion."""
    result = agent(value, **kwargs)
    if inspect.isawaitable(result):
        coroutine: Coroutine[Any, Any, Any] = (
            result if inspect.iscoroutine(result) else _wait(result)
        )
        return asyncio.run(coroutine)
    return result


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def run_scenario(
    suite: Suite,
    scenario: Scenario,
    agent: Callable[..., Any],
    config: Config,
    *,
    overwrite: bool = False,
) -> ScenarioResult:
    """Run one scenario in a session and evaluate its expectations.

    Expectation failures count as the run's error only in the modes that write a baseline
    (``record`` and ``rewrite``), so a failing run never replaces one. In ``drift`` mode the
    candidate is still compared, so both kinds of problem are reported together.
    """
    session = Session(scenario.name, config=config, overwrite=overwrite)
    try:
        session.start()
    except ReplexError as exc:
        return ScenarioResult(scenario.name, "error", error=_describe(exc))
    failures: list[str] = []
    error: BaseException | None = None
    try:
        call_agent(agent, scenario.input, suite.agent_kwargs(scenario))
        failures = suite.expectations(scenario).check(session.trace)
    except Exception as exc:
        error = exc
    except BaseException as exc:
        session.finish(exc)
        raise
    finish_with = error
    if finish_with is None and failures and session.mode in ("record", "rewrite"):
        finish_with = AssertionError("\n".join(failures))
    try:
        session.finish(finish_with)
    except ReplexError as exc:
        error = error or exc
    result = ScenarioResult(
        scenario.name,
        "passed",
        outcome=session.outcome,
        failures=failures,
        error=_describe(error) if error is not None else None,
        drift_report=session.drift_report,
        written_path=session.written_path,
        steps=session.trace.steps_count,
        tools=session.trace.tool_names,
    )
    if error is not None:
        result.status = "error"
    elif result.problems:
        result.status = "failed"
    return result


def suite_config(suite: Suite, mode: str | None = None) -> Config:
    """The configuration a suite runs under: the project's, with ``mode`` on top.

    The project is the nearest ``pyproject.toml`` at or above the suite file, so ``replex run``
    behaves the same from any working directory.
    """
    config = load_config(suite.path.parent.resolve(), mode=mode)
    if config.mode == "live":
        raise SuiteError(
            "replex run needs a mode that records or replays: replay, record, drift or rewrite"
        )
    check_ci_guard(config.mode)
    return config


def run_suite(
    suite: Suite,
    config: Config,
    *,
    only: Sequence[str] = (),
    overwrite: bool = False,
) -> list[ScenarioResult]:
    """Run the selected scenarios in file order and return their results."""
    scenarios = suite.select(only)
    agent = import_agent(suite.agent, suite.path.parent)
    return [run_scenario(suite, s, agent, config, overwrite=overwrite) for s in scenarios]
