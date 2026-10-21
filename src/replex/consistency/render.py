"""Consistency reports for the terminal (rich), Markdown and one-line summaries."""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from replex.consistency.analysis import ConsistencyReport, PathVariant, Spread


def path_text(variant: PathVariant, *, code: bool = False) -> str:
    """A variant's tool path, ``get_order → check_refund_policy``, plus its category if any."""
    names = [f"`{t}`" if code else t for t in variant.tools]
    text = " → ".join(names) if names else "(no tools)"
    if variant.category is not None:
        text += f" ⇒ {variant.category}"
    return text


def verdict(report: ConsistencyReport) -> str:
    """One sentence on how consistent the runs were and where the recorded path stands."""
    n = report.runs
    if report.consistent:
        head = f"All {n} runs took the same path"
        if report.baseline_share is None:
            return f"{head}."
        return (
            f"{head}, the recorded one."
            if report.baseline_share
            else f"{head}, not the recorded one."
        )
    head = f"{n} runs took {report.distinct_paths} different paths."
    if report.baseline_share is None:
        return head
    taken = next((v.count for v in report.variants if v.matches_baseline), 0)
    if not taken:
        return f"{head} None took the recorded path."
    where = "the majority" if report.baseline_is_majority else "not a majority"
    return f"{head} {taken} of {n} took the recorded path ({where})."


def _spread(spread: Spread, unit: str = "") -> str:
    median = f"{spread.median:g}"
    if spread.min == spread.max:
        return f"{spread.min}{unit}"
    return f"{spread.min}{unit} to {spread.max}{unit} (median {median}{unit})"


def spread_line(report: ConsistencyReport) -> str:
    """Tokens, latency and steps across the runs, on one line."""
    return (
        f"tokens {_spread(report.tokens)} · latency {_spread(report.latency_ms, ' ms')} · "
        f"steps {_spread(report.steps)}"
    )


def summary_lines(report: ConsistencyReport) -> list[str]:
    """Plain-text lines for a pytest summary: the verdict, then one line per path."""
    lines = [f"{report.name}: {verdict(report)}"]
    for v in report.variants:
        mark = "  (recorded)" if v.matches_baseline else ""
        lines.append(f"  {v.count}/{report.runs}  {path_text(v)}{mark}")
    return lines


def render_consistency(report: ConsistencyReport, console: Console) -> None:
    """Print one report: verdict, a table of paths, and the spread of the numbers."""
    console.print(Text.assemble((report.name, "bold"), f"  ·  {report.runs} runs"))
    style = "green" if report.consistent and report.baseline_share != 0 else "yellow"
    console.print(Text(verdict(report), style=style))
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("runs", justify="right")
    table.add_column("share", justify="right")
    table.add_column("path")
    table.add_column("")
    for v in report.variants:
        table.add_row(
            f"{v.count}/{report.runs}",
            f"{v.share:.0%}",
            path_text(v),
            Text("recorded", style="dim") if v.matches_baseline else "",
        )
    console.print(table)
    console.print(Text(spread_line(report), style="dim"))


def consistency_markdown(reports: Sequence[ConsistencyReport]) -> str:
    """A Markdown section per scenario (for pull requests and job summaries)."""
    out = ["## RepleX consistency", ""]
    if not reports:
        out.append("No repeated runs found.")
    for r in reports:
        out += [
            f"### {r.name}",
            "",
            verdict(r),
            "",
            "| Runs | Share | Path | |",
            "|---:|---:|---|---|",
        ]
        for v in r.variants:
            mark = "recorded" if v.matches_baseline else ""
            out.append(
                f"| {v.count}/{r.runs} | {v.share:.0%} | {path_text(v, code=True)} | {mark} |"
            )
        out += ["", spread_line(r), ""]
    return "\n".join(out).rstrip() + "\n"
