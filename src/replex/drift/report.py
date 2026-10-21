"""Rendering: terminal (rich), JSON, Markdown and the single-file HTML report."""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
from collections.abc import Sequence
from importlib import resources
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from replex._version import __version__
from replex.drift.diff import TraceDiff
from replex.pricing import format_usd
from replex.trace.model import ModelStep, ToolStep, Trace

if TYPE_CHECKING:
    from replex.drift.compare import DriftReport

_SEVERITY_STYLE = {"fail": "bold red", "warn": "yellow", "info": "dim"}
_STATUS_STYLE = {"same": "dim", "changed": "yellow", "added": "green", "removed": "red"}
_STATUS_MARK = {"same": "=", "changed": "~", "added": "+", "removed": "-"}
_MD_SEVERITY = {"fail": "❌ fail", "warn": "⚠️ warn", "info": "💬 info"}


def _short(value: Any, width: int = 70) -> str:
    text = (
        value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    )
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def step_label(step: ModelStep | ToolStep | None) -> str:
    """One-line description of a step."""
    if step is None:
        return ""
    if isinstance(step, ToolStep):
        suffix = f" ✗ {step.error}" if step.error else ""
        return f"{step.name}({_short(step.args, 40)}){suffix}"
    wants = ", ".join(r.name for r in step.tool_requests)
    if wants:
        return f"model → {wants}"
    return f"model: {_short(step.text, 50)!s}" if step.text else f"model ({step.stop_reason})"


# ---------------------------------------------------------------------- terminal


def _cost(amount: float | None, unpriced: list[str]) -> str:
    if amount is not None or not unpriced:
        return format_usd(amount)
    return f"cost unknown (no price for {', '.join(unpriced)})"


def render_trace(trace: Trace, console: Console, *, full: bool = False) -> None:
    """Step-by-step view of one trace."""
    head = Text.assemble(
        (trace.name, "bold"),
        f"  {trace.provider} / {trace.model}  ·  {trace.steps_count} steps  ·  "
        f"{trace.usage.total} tokens  ·  {_cost(trace.cost, trace.unpriced_models)}  ·  "
        f"{trace.total_latency_ms} ms",
    )
    console.print(head)
    width = None if full else 100
    for step in trace.steps:
        if isinstance(step, ToolStep):
            line = Text.assemble(
                (f"{step.index:>3} ", "dim"),
                ("tool  ", "cyan"),
                (step.name, "bold cyan"),
                f"({_short(step.args, width or 10_000)})",
            )
            console.print(line)
            if step.error:
                console.print(Text(f"        ✗ {step.error}", style="red"))
            else:
                console.print(
                    Text(f"        → {_short(step.result, width or 10_000)}", style="dim")
                )
        else:
            line = Text.assemble(
                (f"{step.index:>3} ", "dim"),
                ("model ", "magenta"),
                (f"{step.model} ", "bold magenta"),
                (f"[{step.stop_reason}] ", "dim"),
                (f"{step.usage.input_tokens}→{step.usage.output_tokens} tok", "dim"),
                (f"  {format_usd(step.cost)}" if step.cost is not None else "", "dim"),
            )
            console.print(line)
            if step.error:
                console.print(Text(f"        ✗ {step.error}", style="red"))
            if step.text:
                console.print(Text(f"        “{_short(step.text, width or 10_000)}”"))
            for r in step.tool_requests:
                console.print(
                    Text(f"        ⇢ {r.name}({_short(r.args, width or 10_000)})", style="cyan")
                )
    if trace.final_output:
        console.print(Text.assemble(("final output: ", "bold"), _short(trace.final_output, 200)))
    for note in trace.notes:
        console.print(Text(f"note: {note}", style="dim italic"))


def diff_table(d: TraceDiff, a: Trace, b: Trace) -> Table:
    """Aligned side-by-side diff as a rich table."""
    table = Table(show_header=True, header_style="bold", expand=True, show_lines=False)
    table.add_column("", width=1)
    table.add_column(f"baseline · {a.name}", ratio=1)
    table.add_column(f"candidate · {b.name}", ratio=1)
    table.add_column("changes", ratio=1)
    for e in d.entries:
        sa = a.steps[e.a_index] if e.a_index is not None else None
        sb = b.steps[e.b_index] if e.b_index is not None else None
        changes = "; ".join(
            f"{c.path}: {_short(c.before, 20)} → {_short(c.after, 20)}" for c in e.changes[:3]
        )
        style = _STATUS_STYLE[e.status]
        table.add_row(
            Text(_STATUS_MARK[e.status], style=style),
            Text(step_label(sa), style=style if e.status != "added" else ""),
            Text(step_label(sb), style=style if e.status != "removed" else ""),
            Text(changes, style="yellow"),
        )
    return table


def render_report(report: DriftReport, console: Console) -> None:
    """Terminal rendering of a drift report."""
    status = (
        Text("PASSED", style="bold green") if report.passed else Text("FAILED", style="bold red")
    )
    b, c = report.baseline, report.candidate
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column()
    summary.add_column()
    summary.add_row("", "baseline", "candidate")
    summary.add_row("model", f"{b.provider} / {b.model}", f"{c.provider} / {c.model}")
    summary.add_row("steps", str(b.steps), str(c.steps))
    summary.add_row("tools", " → ".join(b.tool_names) or "—", " → ".join(c.tool_names) or "—")
    summary.add_row(
        "tokens", str(b.input_tokens + b.output_tokens), str(c.input_tokens + c.output_tokens)
    )
    summary.add_row("cost", format_usd(b.cost), format_usd(c.cost))
    findings = Table(show_header=True, header_style="bold", expand=True)
    findings.add_column("severity", width=8)
    findings.add_column("check", width=16)
    findings.add_column("step", width=5, justify="right")
    findings.add_column("finding", ratio=1)
    for f in report.findings:
        findings.add_row(
            Text(f.severity, style=_SEVERITY_STYLE[f.severity]),
            f.check,
            "" if f.step_index is None else str(f.step_index),
            f.message,
        )
    parts: list[Any] = [summary, Text("")]
    parts.append(findings if report.findings else Text("No findings.", style="green"))
    title = Text.assemble("RepleX drift · ", (report.name, "bold"), " · ", status)
    console.print(
        Panel(
            Group(*parts),
            title=title,
            title_align="left",
            border_style="green" if report.passed else "red",
        )
    )
    if report.baseline_trace is not None and report.candidate_trace is not None:
        console.print(diff_table(report.diff, report.baseline_trace, report.candidate_trace))


# ---------------------------------------------------------------------- markdown


def _arrow(names: list[str]) -> str:
    return " → ".join(names) or "—"


def _md(value: Any, width: int = 120) -> str:
    return _short(value, width).replace("|", "\\|")


def report_markdown(report: DriftReport, *, heading_level: int = 3) -> str:
    """Markdown for one drift report."""
    h = "#" * heading_level
    icon = "✅" if report.passed else "❌"
    nf, nw = len(report.failures), len(report.warnings)
    counts = f"{nf} failure{'s' * (nf != 1)}, {nw} warning{'s' * (nw != 1)}"
    b, c = report.baseline, report.candidate
    lines = [
        f"{h} {icon} RepleX drift: `{report.name}` — {counts}",
        "",
        "| | Baseline | Candidate |",
        "|---|---|---|",
        f"| Model | {_md(b.provider)} / {_md(b.model)} | {_md(c.provider)} / {_md(c.model)} |",
        f"| Steps | {b.steps} | {c.steps} |",
        f"| Tools | {_md(_arrow(b.tool_names))} | {_md(_arrow(c.tool_names))} |",
        f"| Tokens | {b.input_tokens + b.output_tokens} | {c.input_tokens + c.output_tokens} |",
        f"| Cost | {_md(format_usd(b.cost))} | {_md(format_usd(c.cost))} |",
        f"| Final output | {_md(b.final_output, 80)} | {_md(c.final_output, 80)} |",
        "",
    ]
    if report.findings:
        lines += ["| Severity | Check | Step | Finding |", "|---|---|---|---|"]
        for f in report.findings:
            step = "" if f.step_index is None else str(f.step_index)
            lines.append(
                f"| {_MD_SEVERITY[f.severity]} | `{f.check}` | {step} | {_md(f.message, 200)} |"
            )
        lines.append("")
    if report.baseline_trace is not None and report.candidate_trace is not None:
        a, bt = report.baseline_trace, report.candidate_trace
        lines += [
            "<details><summary>Aligned steps</summary>",
            "",
            "| | Baseline | Candidate |",
            "|---|---|---|",
        ]
        for e in report.diff.entries:
            sa = a.steps[e.a_index] if e.a_index is not None else None
            sb = bt.steps[e.b_index] if e.b_index is not None else None
            lines.append(
                f"| `{_STATUS_MARK[e.status]}` | {_md(step_label(sa))} | {_md(step_label(sb))} |"
            )
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def reports_markdown(reports: Sequence[DriftReport], *, title: str = "RepleX drift report") -> str:
    """Markdown summary for several pairs (written to ``$GITHUB_STEP_SUMMARY`` in CI)."""
    failed = [r for r in reports if not r.passed]
    icon = "✅" if not failed else "❌"
    lines = [
        f"## {icon} {title}",
        "",
        f"{len(reports)} cassette(s) compared · {len(reports) - len(failed)} passed · "
        f"{len(failed)} failed · {sum(len(r.warnings) for r in reports)} warning(s)",
        "",
    ]
    for r in reports:
        lines.append(report_markdown(r))
    return "\n".join(lines)


GITHUB_COMMENT_LIMIT = 65_536
"""The maximum length of a GitHub issue or pull request comment body, in characters."""


def _verdict(report: DriftReport) -> tuple[str, str]:
    """An icon and a plain-language verdict for one scenario."""
    nf, nw = len(report.failures), len(report.warnings)
    warnings = f"{nw} warning{'s' * (nw != 1)}"
    if nf:
        extra = f", {warnings}" if nw else ""
        return "❌", f"Behaviour changed: {nf} failure{'s' * (nf != 1)}{extra}"
    if nw:
        return "⚠️", f"Changed within policy: {warnings}"
    return "✅", "Behaves as recorded"


def _finding_details(report: DriftReport) -> str:
    rows = [f for f in report.findings if f.severity != "info"]
    lines = [
        f"<details><summary><code>{html.escape(report.name)}</code>: "
        f"{html.escape(_verdict(report)[1])}</summary>",
        "",
        "| Severity | Check | Step | Finding |",
        "|---|---|---|---|",
    ]
    for f in rows:
        step = "" if f.step_index is None else str(f.step_index)
        lines.append(
            f"| {_MD_SEVERITY[f.severity]} | `{f.check}` | {step} | {_md(f.message, 200)} |"
        )
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def comment_markdown(
    reports: Sequence[DriftReport],
    *,
    title: str = "RepleX behaviour review",
    missing: Sequence[str] = (),
    new: Sequence[str] = (),
    link: str | None = None,
    max_chars: int = GITHUB_COMMENT_LIMIT,
) -> str:
    """A compact summary sized for a pull request comment.

    One table row per scenario with a plain-language verdict, the findings of each changed
    scenario in a collapsed block, and the scenarios that only one side has: ``missing`` names
    recordings with no latest run, ``new`` names latest runs with no recording. The text
    never exceeds ``max_chars``; whole rows and blocks are dropped from the end instead, and a
    closing note points to ``link`` (the full report) when one is given.
    """
    failed = [r for r in reports if r.failures]
    warned = [r for r in reports if r.warnings and not r.failures]
    icon = "❌" if failed else "⚠️" if warned else "✅" if reports else "💬"
    pointer = f"[full report]({link})" if link else "the full report"
    if reports:
        headline = (
            f"{len(failed)} of {len(reports)} scenario{'s' * (len(reports) != 1)} changed behaviour"
        )
        counts = (
            f"{len(reports)} compared · {len(reports) - len(failed) - len(warned)} as recorded · "
            f"{len(warned)} within policy · {len(failed)} changed"
        )
    else:
        headline = "no latest runs to compare"
        counts = (
            "No scenario has both a recording and a latest run. That is expected in replay "
            "mode, where the tests check the agent against its recordings without running it live."
        )
    if link:
        counts += f" · {pointer}" if reports else f" See the {pointer}."
    head = [f"## {icon} {title}: {headline}", "", counts, ""]
    blocks: list[str] = []
    if reports or new:
        blocks.append("| Scenario | Verdict | Steps | Tools (latest run) |\n|---|---|---|---|")
        for r in reports:
            steps = f"{r.baseline.steps} → {r.candidate.steps}"
            mark, verdict = _verdict(r)
            blocks.append(
                f"| `{_md(r.name)}` | {mark} {verdict} | {steps} | "
                f"{_md(_arrow(r.candidate.tool_names))} |"
            )
        for name in new:
            blocks.append(f"| `{_md(name)}` | 🆕 New scenario: no recording to compare | | |")
        blocks.append("")
    for r in failed + warned:
        blocks.append(_finding_details(r))
    if missing:
        names = ", ".join(f"`{_md(name)}`" for name in missing)
        blocks.append(
            f"<details><summary>{len(missing)} recorded scenario{'s' * (len(missing) != 1)} "
            f"not run live</summary>\n\n{names}\n\n</details>\n"
        )
    note = f"\n_Truncated to fit in a comment: every scenario is in {pointer}._\n"
    text = "\n".join(head) + "\n"
    for i, block in enumerate(blocks):
        reserve = len(note) if i < len(blocks) - 1 else 0
        if len(text) + len(block) + 1 + reserve > max_chars:
            return (text + note)[:max_chars]
        text += block + "\n"
    return text


# ---------------------------------------------------------------------- html


def _payload(reports: Sequence[DriftReport]) -> dict[str, Any]:
    pairs = []
    for r in reports:
        pairs.append(
            {
                "name": r.name,
                "report": r.to_dict(),
                "baseline": r.baseline_trace.model_dump(mode="json") if r.baseline_trace else None,
                "candidate": r.candidate_trace.model_dump(mode="json")
                if r.candidate_trace
                else None,
            }
        )
    return {
        "kind": "replex-report",
        "replex_version": __version__,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pairs": pairs,
    }


def _script_json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _static(name: str) -> str | None:
    try:
        node = resources.files("replex.console") / "static"
        for part in name.split("/"):
            node = node / part
        return node.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def html_report(reports: Sequence[DriftReport]) -> str:
    """One self-contained HTML file: the console's Compare and Drift views with data inlined.

    The file makes no network requests: scripts, styles and data are all inline.
    """
    data = _script_json(_payload(reports))
    data_tag = f'<script id="replex-data" type="application/json">{data}</script>'
    index = _static("index.html")
    js = _static("assets/app.js")
    css = _static("assets/app.css")
    if index is None or js is None:
        return _fallback_html(reports, data_tag)
    page = re.sub(r'<script[^>]*src="[^"]*app\.js"[^>]*></script>', "", index)
    page = re.sub(r'<link[^>]*href="[^"]*app\.css"[^>]*>', "", page)
    page = re.sub(r'<link[^>]*rel="icon"[^>]*>', "", page)
    inline = f"<style>{css or ''}</style>" if css else ""
    script = '<script type="module">' + js.replace("</script", "<\\/script") + "</script>"
    page = page.replace("</head>", f"{inline}</head>", 1)
    return page.replace("</body>", f"{data_tag}{script}</body>", 1)


def _fallback_html(reports: Sequence[DriftReport], data_tag: str) -> str:
    """Plain HTML used when the console bundle is not built (e.g. a bare source checkout)."""
    rows = []
    for r in reports:
        for f in r.findings or []:
            rows.append(
                f"<tr><td>{html.escape(r.name)}</td><td>{f.severity}</td><td>{f.check}</td>"
                f"<td>{'' if f.step_index is None else f.step_index}</td>"
                f"<td>{html.escape(f.message)}</td></tr>"
            )
        if not r.findings:
            rows.append(f"<tr><td>{html.escape(r.name)}</td><td colspan=4>no findings</td></tr>")
    return (
        "<!doctype html><html><head><meta charset=utf-8><title>RepleX report</title></head><body>"
        "<h1>RepleX drift report</h1><table border=1 cellpadding=4><tr><th>Cassette</th>"
        "<th>Severity</th><th>Check</th><th>Step</th><th>Finding</th></tr>"
        + "".join(rows)
        + f"</table>{data_tag}</body></html>"
    )
