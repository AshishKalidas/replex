"""Drift history: an append-only log of drift checks and of the decisions taken on them.

Every completed drift comparison appends one JSON line to the history file
(``.replex/history.jsonl`` by default, ``[tool.replex] history``). Accepting a latest run
(``replex approve`` or the console) and rejecting one (the console) append a decision line.
Lines hold names, counts and numbers only: findings are reduced to their check names, so no
message, argument or output text (and therefore no secret) ever reaches the file.

Writers hold an exclusive lock on a sibling ``.lock`` file while they append, so parallel
pytest-xdist workers never interleave or lose lines. The file is rotated to ``<name>.1`` when
an append would take it past :data:`MAX_BYTES`, keeping at most two generations on disk.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import subprocess
import sys
import warnings
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator
from rich.console import Console
from rich.table import Table

from replex.config import Config
from replex.drift.compare import DriftReport
from replex.session import _git_sha, _now

#: Size at which the history file is rotated. The current and one rotated file are kept, so a
#: project's history never takes more than twice this on disk (several thousand checks).
MAX_BYTES = 1_000_000

#: Window of :func:`trend`, in days.
TREND_DAYS = 7

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class _Entry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: Literal[1] = 1
    at: str
    name: str
    git_sha: str | None = None
    branch: str | None = None

    @field_validator("at")
    @classmethod
    def _timestamp(cls, v: str) -> str:
        _parse(v)
        return v


class CheckEntry(_Entry):
    """One drift comparison of a scenario's latest run with its recording."""

    kind: Literal["check"] = "check"
    verdict: Literal["passed", "failed"]
    model: str
    baseline_model: str
    #: Number of ``fail`` findings per check name (``{"tool_sequence": 1}``).
    fail: dict[str, int] = Field(default_factory=dict)
    #: Number of ``warn`` findings per check name.
    warn: dict[str, int] = Field(default_factory=dict)
    #: The latest run's tool calls, in order.
    tools: list[str] = Field(default_factory=list)
    steps: int
    tokens: int
    latency_ms: int


class DecisionEntry(_Entry):
    """A reviewer accepted the latest run as the new recording, or rejected it."""

    kind: Literal["accepted", "rejected"]
    via: Literal["cli", "console"]


Entry = Annotated[CheckEntry | DecisionEntry, Field(discriminator="kind")]
_ENTRY: TypeAdapter[CheckEntry | DecisionEntry] = TypeAdapter(Entry)


# ---------------------------------------------------------------------- building entries


def git_branch(root: str | os.PathLike[str]) -> str | None:
    """The checked-out branch (``REPLEX_GIT_BRANCH`` overrides; empty means none)."""
    value = os.environ.get("REPLEX_GIT_BRANCH")
    if value is not None:
        return value or None
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _counts(checks: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for check in checks:
        counts[check] = counts.get(check, 0) + 1
    return counts


def check_entry(
    report: DriftReport,
    *,
    git_sha: str | None = None,
    branch: str | None = None,
    at: str | None = None,
) -> CheckEntry:
    """The history line for a drift report. Finding messages are not kept, only check names."""
    cand = report.candidate
    return CheckEntry(
        at=at or _now(),
        name=report.name,
        git_sha=git_sha,
        branch=branch,
        verdict="passed" if report.passed else "failed",
        model=cand.model,
        baseline_model=report.baseline.model,
        fail=_counts(f.check for f in report.failures),
        warn=_counts(f.check for f in report.warnings),
        tools=list(cand.tool_names),
        steps=cand.steps,
        tokens=cand.input_tokens + cand.output_tokens,
        latency_ms=cand.latency_ms,
    )


# ---------------------------------------------------------------------- writing


def rotated_path(path: Path) -> Path:
    """Where the previous generation of a history file is kept."""
    return path.with_name(path.name + ".1")


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    with open(path.with_name(path.name + ".lock"), "a+b") as handle:
        _lock(handle.fileno())
        try:
            yield
        finally:
            _unlock(handle.fileno())


def append(path: Path, entry: CheckEntry | DecisionEntry, *, max_bytes: int = MAX_BYTES) -> None:
    """Append ``entry`` to the history file at ``path``, rotating it first if it is full."""
    line = (entry.model_dump_json(exclude_none=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        if path.is_file() and path.stat().st_size + len(line) > max_bytes:
            os.replace(path, rotated_path(path))
        with open(path, "ab") as handle:
            handle.write(line)


def record(path: Path | None, entry: CheckEntry | DecisionEntry) -> None:
    """Append ``entry`` unless history is disabled (``path`` is ``None``).

    History is a record of the run, not part of its result: a file that cannot be written is
    reported as a warning and never fails the drift check or the decision.
    """
    if path is None:
        return
    try:
        append(path, entry)
    except OSError as exc:
        warnings.warn(f"RepleX could not write drift history to {path}: {exc}", stacklevel=2)


def record_check(config: Config, report: DriftReport, *, git_sha: str | None) -> None:
    """Record a completed drift comparison in the configured history file."""
    path = config.history_path
    if path is not None:
        record(path, check_entry(report, git_sha=git_sha, branch=git_branch(config.root)))


def record_decision(
    path: Path | None,
    kind: Literal["accepted", "rejected"],
    name: str,
    *,
    via: Literal["cli", "console"],
    root: str | os.PathLike[str],
) -> None:
    """Record that the latest run of ``name`` was accepted or rejected."""
    if path is None:
        return
    record(
        path,
        DecisionEntry(
            kind=kind,
            at=_now(),
            name=name,
            via=via,
            git_sha=_git_sha(str(root)),
            branch=git_branch(root),
        ),
    )


# ---------------------------------------------------------------------- reading


def read(path: Path | None) -> list[CheckEntry | DecisionEntry]:
    """Every readable entry, oldest first (the rotated file, then the current one).

    Lines that do not parse (a write cut short by a crash, or a newer format) are skipped.
    """
    if path is None:
        return []
    entries: list[CheckEntry | DecisionEntry] = []
    for file in (rotated_path(path), path):
        try:
            lines = file.read_bytes().splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            with contextlib.suppress(ValidationError):
                entries.append(_ENTRY.validate_json(line))
    return entries


# ---------------------------------------------------------------------- summaries


class ScenarioHistory(BaseModel):
    """One scenario's recent history: its last checks and the decisions taken since."""

    name: str
    #: Checks in the window (the last ``limit`` checks of this scenario).
    checks: int
    passed: int
    failed: int
    #: Share of the window's checks that passed, ``None`` before the first check.
    pass_rate: float | None
    #: All checks ever recorded for this scenario (that are still in the file).
    total_checks: int
    last_check_at: str | None
    #: When a check last found behaviour that fails the policy.
    last_change_at: str | None
    last_decision: DecisionEntry | None
    #: The window's checks and every decision since the first of them, oldest first.
    events: list[Entry]


class Trend(BaseModel):
    """What happened across all scenarios in the :data:`TREND_DAYS` days to the last entry."""

    days: int = TREND_DAYS
    since: str | None = None
    until: str | None = None
    checks: int = 0
    passed: int = 0
    failed: int = 0
    accepted: int = 0
    rejected: int = 0
    scenarios: int = 0


def _parse(at: str) -> _dt.datetime:
    return _dt.datetime.strptime(at, _TIME_FORMAT).replace(tzinfo=_dt.timezone.utc)


def scenario_history(
    name: str, entries: Sequence[CheckEntry | DecisionEntry], limit: int
) -> ScenarioHistory:
    """Summarise ``name``'s last ``limit`` (at least 1) checks from ``entries`` (oldest first)."""
    own = [e for e in entries if e.name == name]
    positions = [i for i, e in enumerate(own) if isinstance(e, CheckEntry)]
    checks = [e for e in own if isinstance(e, CheckEntry)]
    window = checks[-limit:]
    events = own[positions[-len(window)] :] if window else own
    passed = sum(1 for c in window if c.verdict == "passed")
    changes = [c for c in checks if c.verdict == "failed"]
    decisions = [e for e in own if isinstance(e, DecisionEntry)]
    return ScenarioHistory(
        name=name,
        checks=len(window),
        passed=passed,
        failed=len(window) - passed,
        pass_rate=passed / len(window) if window else None,
        total_checks=len(checks),
        last_check_at=checks[-1].at if checks else None,
        last_change_at=changes[-1].at if changes else None,
        last_decision=decisions[-1] if decisions else None,
        events=events,
    )


def trend(entries: Sequence[CheckEntry | DecisionEntry], days: int = TREND_DAYS) -> Trend:
    """Counts over the ``days`` days that end with the most recent entry."""
    if not entries:
        return Trend(days=days)
    until = max(_parse(e.at) for e in entries)
    since = until - _dt.timedelta(days=days)
    recent = [e for e in entries if _parse(e.at) > since]
    checks = [e for e in recent if isinstance(e, CheckEntry)]
    passed = sum(1 for c in checks if c.verdict == "passed")
    return Trend(
        days=days,
        since=min(e.at for e in recent),
        until=until.strftime(_TIME_FORMAT),
        checks=len(checks),
        passed=passed,
        failed=len(checks) - passed,
        accepted=sum(1 for e in recent if e.kind == "accepted"),
        rejected=sum(1 for e in recent if e.kind == "rejected"),
        scenarios=len({e.name for e in recent}),
    )


class HistorySummary(BaseModel):
    """What ``replex history --json`` prints and the console's history endpoint returns."""

    limit: int
    trend: Trend
    scenarios: list[ScenarioHistory]


def summary(
    entries: Sequence[CheckEntry | DecisionEntry], *, name: str | None = None, limit: int = 20
) -> HistorySummary:
    """The trend and each scenario's last ``limit`` checks, most recently active first.

    With ``name``, only that scenario is included (when it has any entries).
    """
    last_seen: dict[str, str] = {}
    for e in entries:
        last_seen[e.name] = max(e.at, last_seen.get(e.name, e.at))
    names = sorted(last_seen, key=lambda n: (last_seen[n], n), reverse=True)
    if name is not None:
        names = [n for n in names if n == name]
    return HistorySummary(
        limit=limit,
        trend=trend(entries),
        scenarios=[scenario_history(n, entries, limit) for n in names],
    )


# ---------------------------------------------------------------------- terminal output

_MARKS = {
    "passed": "[green]✓[/green]",
    "failed": "[red]✗[/red]",
    "accepted": "[cyan]A[/cyan]",
    "rejected": "[yellow]R[/yellow]",
}
_LEGEND = "✓ passed   ✗ behaviour changed   A accepted   R rejected"


def _when(at: str | None) -> str:
    return at[:16].replace("T", " ") if at else "-"


def _mark(entry: CheckEntry | DecisionEntry) -> str:
    return _MARKS[entry.verdict if isinstance(entry, CheckEntry) else entry.kind]


def _rate(h: ScenarioHistory) -> str:
    return "-" if h.pass_rate is None else f"{h.pass_rate:.0%} of {h.checks}"


def _findings(entry: CheckEntry) -> str:
    def names(counts: dict[str, int]) -> str:
        return ", ".join(f"{k} (x{n})" if n > 1 else k for k, n in sorted(counts.items()))

    sides = (("fail", entry.fail), ("warn", entry.warn))
    parts = [f"{label}: {names(counts)}" for label, counts in sides if counts]
    return "; ".join(parts) or "[dim]none[/dim]"


def trend_line(t: Trend) -> str:
    """One sentence about the trend, for the terminal."""
    if not t.checks and not (t.accepted or t.rejected):
        return "no drift checks yet"
    return (
        f"{t.checks} check(s) in the {t.days} days to {_when(t.until)}: {t.passed} passed, "
        f"{t.failed} changed behaviour; {t.accepted} accepted, {t.rejected} rejected"
    )


def render_history(data: HistorySummary, console: Console, *, detailed: bool = False) -> None:
    """Print a table of scenarios, or with ``detailed`` a timeline of each one's events."""
    if not detailed:
        console.print(trend_line(data.trend))
        table = Table(title="drift history", title_justify="left")
        for col in ("scenario", "pass rate", "recent", "last check", "last change", "decision"):
            table.add_column(col)
        for h in data.scenarios:
            decision = h.last_decision
            table.add_row(
                h.name,
                _rate(h),
                " ".join(_mark(e) for e in h.events),
                _when(h.last_check_at),
                _when(h.last_change_at),
                f"{decision.kind} {_when(decision.at)}" if decision else "-",
            )
        console.print(table)
        console.print(f"[dim]{_LEGEND}[/dim]")
        return
    for h in data.scenarios:
        title = f"{h.name}: {h.passed} of {h.checks} recent check(s) passed"
        table = Table(title=title, title_justify="left")
        for col in ("when", "event", "findings", "tools", "model", "commit"):
            table.add_column(col)
        previous: list[str] | None = None
        for e in h.events:
            commit = " ".join(x for x in (e.git_sha, f"({e.branch})" if e.branch else None) if x)
            if isinstance(e, DecisionEntry):
                event = f"{_mark(e)} {e.kind} [dim]via {e.via}[/dim]"
                table.add_row(_when(e.at), event, "", "", "", commit)
                continue
            label = "passed" if e.verdict == "passed" else "changed"
            same = previous is not None and e.tools == previous
            previous = e.tools
            model = e.model if e.model == e.baseline_model else f"{e.baseline_model} → {e.model}"
            table.add_row(
                _when(e.at),
                f"{_mark(e)} {label}",
                _findings(e),
                "[dim]as before[/dim]" if same else " → ".join(e.tools) or "[dim]no tools[/dim]",
                model,
                commit,
            )
        console.print(table)
