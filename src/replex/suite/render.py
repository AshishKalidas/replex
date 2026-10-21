"""The terminal summary of a suite run."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from replex.suite.runner import ScenarioResult

_STATUS = {
    "passed": "[green]passed[/green]",
    "failed": "[red]FAILED[/red]",
    "error": "[red]ERROR[/red]",
}


def _first_line(text: str, width: int = 70) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= width else line[: width - 1] + "…"


def _note(result: ScenarioResult) -> str:
    if result.error or result.failures:
        return _first_line(result.error or result.failures[0])
    report = result.drift_report
    if report is None:
        return ""
    if report.failures:
        return "policy: " + ", ".join(dict.fromkeys(f.check for f in report.failures))
    return f"{len(report.warnings)} drift warning(s)" if report.warnings else ""


def render_results(results: Sequence[ScenarioResult], mode: str, console: Console) -> None:
    """Print a table of the scenarios, the details of each problem, and the totals."""
    table = Table(title=f"replex run ({mode})", title_justify="left")
    for col in ("scenario", "result", "outcome", "steps", "tools", "notes"):
        table.add_column(col, justify="right" if col == "steps" else "left")
    for r in results:
        table.add_row(
            escape(r.name),
            _STATUS[r.status],
            (r.outcome or "").replace("_", " "),
            "" if r.steps is None else str(r.steps),
            escape(" → ".join(r.tools)),
            escape(_note(r)),
        )
    console.print(table)
    for r in results:
        if r.problems:
            console.print(f"\n[bold]{escape(r.name)}[/bold]")
            for problem in r.problems:
                console.print(escape(problem), soft_wrap=True)
    counts = Counter(r.status for r in results)
    console.print(
        f"\n{len(results)} scenario(s): [green]{counts['passed']} passed[/green], "
        f"[red]{counts['failed']} failed[/red], [red]{counts['error']} error(s)[/red]"
    )
