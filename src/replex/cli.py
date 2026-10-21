"""The ``replex`` command-line interface."""

from __future__ import annotations

import functools
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click
from rich.console import Console
from rich.table import Table

from replex._version import __version__
from replex.cassette.storage import (
    atomic_write_text,
    cassette_path,
    list_cassettes,
    load,
    name_from_path,
    promote_candidate,
)
from replex.config import Config, check_ci_guard, load_config
from replex.errors import CassetteError, ReplexError
from replex.pricing import Pricing, format_usd

F = TypeVar("F", bound=Callable[..., Any])


def _console() -> Console:
    return Console(
        highlight=False, soft_wrap=False, width=max(100, shutil.get_terminal_size().columns)
    )


def _errors(fn: F) -> F:
    """Turn RepleX errors into clean CLI errors (exit code 2 for usage, 1 otherwise)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ReplexError as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def _config() -> Config:
    return load_config()


def _resolve(arg: str, *directories: Path) -> Path:
    """A cassette given as a path, or as a name inside one of ``directories`` (in order)."""
    p = Path(arg)
    if p.is_file():
        return p
    tried = []
    for directory in directories:
        candidate = cassette_path(directory, arg)
        if candidate.is_file():
            return candidate
        tried.append(str(candidate))
    raise click.BadParameter(
        f"no cassette file {arg!r} (also tried {', '.join(tried)})", param_hint="cassette"
    )


def _policy(path: str | None, config: Config) -> Any:
    from replex.drift.policy import Policy, load_policy

    if path:
        return load_policy(path)
    return load_policy(config.policy_path) if config.policy_path.is_file() else Policy()


def _emit_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="replex")
def cli() -> None:
    """RepleX: record-and-replay regression testing for AI agents."""


# --------------------------------------------------------------------------- ls


@cli.command("ls")
@click.argument("directory", required=False, type=click.Path(file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_errors
def ls_cmd(directory: Path | None, as_json: bool) -> None:
    """List the cassettes in DIRECTORY (default: the configured cassette_dir)."""
    from replex.providers import build_trace

    cfg = _config()
    pricing = Pricing.from_config(cfg)
    d = directory or cfg.cassette_path
    rows: list[dict[str, Any]] = []
    for path in list_cassettes(d):
        try:
            c = load(path)
        except CassetteError as exc:
            rows.append({"name": name_from_path(path), "error": str(exc)})
            continue
        t = build_trace(c, pricing=pricing)
        rows.append(
            {
                "name": c.name,
                "file": str(path),
                "provider": c.meta.provider,
                "model": c.meta.model,
                "steps": t.steps_count,
                "tool_calls": len(t.tool_calls),
                "tokens": t.usage.total,
                "cost": t.cost,
                "created_at": c.created_at,
            }
        )
    if as_json:
        _emit_json(rows)
        return
    if not rows:
        click.echo(f"no cassettes in {d}")
        return
    table = Table(title=f"cassettes in {d}", title_justify="left")
    for col in ("name", "provider", "model", "steps", "tools", "tokens", "cost", "date"):
        numeric = col in ("steps", "tools", "tokens", "cost")
        table.add_column(col, justify="right" if numeric else "left")
    for r in rows:
        if "error" in r:
            table.add_row(r["name"], "[red]invalid[/red]", r["error"][:60], "", "", "", "", "")
            continue
        table.add_row(
            r["name"],
            r["provider"],
            r["model"],
            str(r["steps"]),
            str(r["tool_calls"]),
            str(r["tokens"]),
            format_usd(r["cost"]),
            r["created_at"],
        )
    _console().print(table)


# --------------------------------------------------------------------------- show


@cli.command("show")
@click.argument("cassette")
@click.option("--json", "as_json", is_flag=True, help="print the trace as JSON")
@click.option("--full", is_flag=True, help="do not truncate long values")
@_errors
def show_cmd(cassette: str, as_json: bool, full: bool) -> None:
    """Show a cassette's trace step by step."""
    from replex.drift.report import render_trace
    from replex.providers import build_trace

    cfg = _config()
    path = _resolve(cassette, cfg.cassette_path, cfg.candidate_path)
    trace = build_trace(load(path), pricing=Pricing.from_config(cfg))
    if as_json:
        click.echo(trace.to_json())
        return
    render_trace(trace, _console(), full=full)


# --------------------------------------------------------------------------- diff


@cli.command("diff")
@click.argument("a")
@click.argument("b")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_errors
def diff_cmd(a: str, b: str, as_json: bool) -> None:
    """Aligned behavioural diff of two cassettes (A = before, B = after)."""
    from replex.drift.diff import diff
    from replex.drift.report import diff_table
    from replex.providers import build_trace

    cfg = _config()
    pricing = Pricing.from_config(cfg)
    ta = build_trace(load(_resolve(a, cfg.cassette_path, cfg.candidate_path)), pricing=pricing)
    tb = build_trace(load(_resolve(b, cfg.candidate_path, cfg.cassette_path)), pricing=pricing)
    d = diff(ta, tb)
    if as_json:
        _emit_json(d.model_dump(mode="json"))
        return
    console = _console()
    console.print(diff_table(d, ta, tb))
    s = d.summary
    console.print(
        f"[dim]=[/dim] {s['same']} same   [yellow]~[/yellow] {s['changed']} changed   "
        f"[green]+[/green] {s['added']} added   [red]-[/red] {s['removed']} removed"
    )


# --------------------------------------------------------------------------- drift


@cli.command("drift")
@click.argument("baseline")
@click.argument("candidate")
@click.option("--policy", "policy_path", type=click.Path(dir_okay=False), help="policy YAML file")
@click.option("--json", "as_json", is_flag=True, help="print the report as JSON")
@click.option("--markdown", "as_markdown", is_flag=True, help="print the report as Markdown")
@_errors
def drift_cmd(
    baseline: str, candidate: str, policy_path: str | None, as_json: bool, as_markdown: bool
) -> None:
    """Compare CANDIDATE with BASELINE under the policy. Exit code 1 on failures."""
    from replex.drift.compare import compare
    from replex.drift.report import render_report

    cfg = _config()
    base = _resolve(baseline, cfg.cassette_path)
    cand = _resolve(candidate, cfg.candidate_path)
    report = compare(
        base,
        cand,
        _policy(policy_path, cfg),
        name=name_from_path(base),
        pricing=Pricing.from_config(cfg),
    )
    if as_json:
        click.echo(report.to_json(), nl=False)
    elif as_markdown:
        click.echo(report.to_markdown())
    else:
        render_report(report, _console())
    if not report.passed:
        sys.exit(1)


# --------------------------------------------------------------------------- run


@cli.command("run")
@click.argument("suite_file", default="replex.suite.yaml", type=click.Path(dir_okay=False))
@click.option(
    "--mode",
    type=click.Choice(["replay", "record", "drift", "rewrite"]),
    help="default: the configured mode (REPLEX_MODE, then [tool.replex])",
)
@click.option("--only", multiple=True, metavar="NAME", help="run only this scenario (repeatable)")
@click.option("--overwrite", is_flag=True, help="let record mode replace existing cassettes")
@click.option(
    "--report",
    "report_out",
    type=click.Path(dir_okay=False, path_type=Path),
    help="after a drift run, write a report (.html, .md or .json by suffix)",
)
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_errors
def run_cmd(
    suite_file: str,
    mode: str | None,
    only: tuple[str, ...],
    overwrite: bool,
    report_out: Path | None,
    as_json: bool,
) -> None:
    """Run the scenarios in SUITE_FILE (default replex.suite.yaml). Exit code 1 on failures."""
    from replex.drift.output import write_drift_report
    from replex.suite import load_suite, run_suite, suite_config
    from replex.suite.render import render_results

    suite = load_suite(suite_file)
    cfg = suite_config(suite, mode)
    results = run_suite(suite, cfg, only=only, overwrite=overwrite)
    drifted = [r.name for r in results if r.drift_report is not None]
    written = write_drift_report(cfg, drifted, report_out) if report_out and drifted else None
    if as_json:
        _emit_json(
            {
                "suite": str(suite.path),
                "mode": cfg.mode,
                "passed": all(r.status == "passed" for r in results),
                "report": str(written) if written else None,
                "scenarios": [r.to_dict() for r in results],
            }
        )
    else:
        console = _console()
        render_results(results, cfg.mode, console)
        if written:
            console.print(f"drift report: {written}")
    if any(r.status != "passed" for r in results):
        sys.exit(1)


# --------------------------------------------------------------------------- report


@cli.command("report")
@click.argument("baseline_dir", type=click.Path(file_okay=False, exists=True, path_type=Path))
@click.argument("candidate_dir", type=click.Path(file_okay=False, exists=True, path_type=Path))
@click.option(
    "--html",
    "html_out",
    type=click.Path(dir_okay=False, path_type=Path),
    help="write the single-file HTML report",
)
@click.option(
    "--markdown",
    "md_out",
    type=click.Path(dir_okay=False, path_type=Path),
    help="write a Markdown summary",
)
@click.option(
    "--json",
    "json_out",
    type=click.Path(dir_okay=False, path_type=Path),
    help="write the reports as JSON",
)
@click.option(
    "--compact",
    is_flag=True,
    help="write the Markdown as a short pull request comment (one row per scenario)",
)
@click.option("--title", help="heading of the Markdown summary")
@click.option("--link", help="URL of the full report, linked from the compact summary")
@click.option(
    "--max-chars",
    type=click.IntRange(min=200),
    help="truncate the compact summary to this length (default: GitHub's comment limit)",
)
@click.option("--policy", "policy_path", type=click.Path(dir_okay=False), help="policy YAML file")
@click.option("--fail-on-drift", is_flag=True, help="exit 1 if any pair fails its policy")
@_errors
def report_cmd(
    baseline_dir: Path,
    candidate_dir: Path,
    html_out: Path | None,
    md_out: Path | None,
    json_out: Path | None,
    compact: bool,
    title: str | None,
    link: str | None,
    max_chars: int | None,
    policy_path: str | None,
    fail_on_drift: bool,
) -> None:
    """Compare every cassette in CANDIDATE_DIR with its baseline and write reports."""
    from replex.drift.compare import compare
    from replex.drift.report import (
        GITHUB_COMMENT_LIMIT,
        comment_markdown,
        html_report,
        reports_markdown,
    )

    if not compact and (link or max_chars):
        raise click.UsageError("--link and --max-chars apply to --compact only")
    cfg = _config()
    policy = _policy(policy_path, cfg)
    pricing = Pricing.from_config(cfg)
    baselines = {name_from_path(p): p for p in list_cassettes(baseline_dir)}
    candidates = {name_from_path(p): p for p in list_cassettes(candidate_dir)}
    reports = [
        compare(baselines[name], cand, policy, name=name, pricing=pricing)
        for name, cand in candidates.items()
        if name in baselines
    ]
    console = _console()
    if not reports:
        console.print(
            f"[yellow]no candidate in {candidate_dir} has a baseline in {baseline_dir}[/yellow]"
        )
    else:
        table = Table(title="RepleX drift report", title_justify="left")
        for col in ("cassette", "result", "fail", "warn", "steps", "tools (candidate)"):
            table.add_column(col)
        for r in reports:
            table.add_row(
                r.name,
                "[green]passed[/green]" if r.passed else "[red]FAILED[/red]",
                str(len(r.failures)),
                str(len(r.warnings)),
                f"{r.baseline.steps} → {r.candidate.steps}",
                " → ".join(r.candidate.tool_names),
            )
        console.print(table)
    if html_out:
        written = atomic_write_text(html_out, html_report(reports)).resolve()
        console.print(f"HTML report: {written}")
    if md_out:
        if compact:
            markdown = comment_markdown(
                reports,
                title=title or "RepleX behaviour review",
                missing=[name for name in baselines if name not in candidates],
                new=[name for name in candidates if name not in baselines],
                link=link,
                max_chars=max_chars or GITHUB_COMMENT_LIMIT,
            )
        else:
            markdown = reports_markdown(reports, title=title or "RepleX drift report")
        written = atomic_write_text(md_out, markdown).resolve()
        console.print(f"Markdown summary: {written}")
    if json_out:
        text = json.dumps([r.to_dict() for r in reports], indent=2, sort_keys=True) + "\n"
        console.print(f"JSON: {atomic_write_text(json_out, text).resolve()}")
    if fail_on_drift and any(not r.passed for r in reports):
        sys.exit(1)


# --------------------------------------------------------------------------- redact


@cli.command("redact")
@click.argument("directory", type=click.Path(exists=True, path_type=Path))
@click.option("--check", is_flag=True, help="scan cassettes and exit 1 if anything is unredacted")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_errors
def redact_cmd(directory: Path, check: bool, as_json: bool) -> None:
    """Scan cassettes for secrets and personal data (use in CI)."""
    from replex.redaction import RedactionRules, check_directory

    if not check:
        raise click.UsageError("only scanning is supported: run `replex redact --check DIR`")
    cfg = _config()
    rules = RedactionRules.from_config(cfg.redact_patterns, cfg.redact_paths)
    findings = check_directory(directory, rules)
    if as_json:
        _emit_json([f.__dict__ for f in findings])
    elif findings:
        table = Table(title=f"{len(findings)} possible leak(s)", title_justify="left")
        for col in ("file", "location", "kind", "excerpt"):
            table.add_column(col)
        for f in findings:
            table.add_row(f.file, f.location, f.kind, f.excerpt)
        _console().print(table)
    else:
        root = Path(directory)
        files = root.rglob("*.replex.json") if root.is_dir() else [root]
        n = sum(1 for f in files if not f.name.startswith("."))
        click.echo(f"ok: {n} cassette(s) in {directory}, nothing unredacted")
    if findings:
        sys.exit(1)


# --------------------------------------------------------------------------- consistency


@cli.command("consistency")
@click.argument("names", nargs=-1)
@click.option(
    "--candidates",
    "candidate_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="candidate directory holding runs/<name>/ (default: the configured candidate_dir)",
)
@click.option(
    "--baselines",
    "baseline_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="baseline directory (default: the configured cassette_dir)",
)
@click.option(
    "--by-field",
    "field",
    metavar="FIELD",
    help="also split paths by this field of a JSON final answer (e.g. decision)",
)
@click.option("--json", "as_json", is_flag=True, help="print the reports as JSON")
@click.option("--markdown", "as_markdown", is_flag=True, help="print the reports as Markdown")
@_errors
def consistency_cmd(
    names: tuple[str, ...],
    candidate_dir: Path | None,
    baseline_dir: Path | None,
    field: str | None,
    as_json: bool,
    as_markdown: bool,
) -> None:
    """How consistently repeated runs behave (pytest --replex-mode=drift --replex-repeat=N).

    Groups the runs of each scenario by the tools called, in order, and shows how many runs
    took each path and whether the recorded run's path is what most runs do. NAMES default to
    every scenario with repeated runs.
    """
    from replex.consistency import analyse_runs, json_field, scenarios_with_runs
    from replex.consistency.render import consistency_markdown, render_consistency

    cfg = _config()
    candidates = candidate_dir or cfg.candidate_path
    categorise = json_field(field) if field else None
    reports = []
    for name in names or scenarios_with_runs(candidates):
        report = analyse_runs(
            candidates,
            name,
            baseline_dir=baseline_dir or cfg.cassette_path,
            categorise=categorise,
        )
        if report is None:
            raise click.BadParameter(f"no repeated runs of {name!r} in {candidates}")
        reports.append(report)
    if as_json:
        _emit_json([r.to_dict() for r in reports])
    elif as_markdown:
        click.echo(consistency_markdown(reports), nl=False)
    elif not reports:
        click.echo(
            f"no repeated runs in {candidates}; record some with "
            f"pytest --replex-mode=drift --replex-repeat=5"
        )
    else:
        console = _console()
        for i, report in enumerate(reports):
            if i:
                console.print()
            render_consistency(report, console)


# --------------------------------------------------------------------------- approve


@cli.command("approve")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_errors
def approve_cmd(name: str, as_json: bool) -> None:
    """Promote the drift candidate NAME to be the new baseline."""
    cfg = _config()
    check_ci_guard("rewrite")
    dst = promote_candidate(cfg.candidate_path, cfg.cassette_path, name)
    from replex.history import record_decision

    record_decision(cfg.history_path, "accepted", name, via="cli", root=cfg.root)
    if as_json:
        _emit_json({"approved": name, "baseline": str(dst)})
    else:
        click.echo(f"approved {name}: {dst} now holds the new baseline (commit it)")


# --------------------------------------------------------------------------- history


@cli.command("history")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@click.option(
    "--limit",
    default=20,
    show_default=True,
    type=click.IntRange(min=1),
    help="how many recent checks per scenario to show",
)
@_errors
def history_cmd(name: str | None, as_json: bool, limit: int) -> None:
    """Show how each scenario's drift checks went over time, and what was accepted."""
    from replex.history import read, render_history, summary

    path = _config().history_path
    if path is None:
        raise click.ClickException("drift history is disabled ([tool.replex] history)")
    data = summary(read(path), name=name, limit=limit)
    if as_json:
        _emit_json(data.model_dump(mode="json"))
        return
    if not data.scenarios:
        click.echo(
            f"no drift checks recorded for {name}" if name else f"no drift history at {path}"
        )
        return
    render_history(data, _console(), detailed=name is not None)


# --------------------------------------------------------------------------- console


@cli.command("console")
@click.argument("directory", required=False, type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--candidates",
    "candidate_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="drift candidates directory",
)
@click.option("--policy", "policy_path", type=click.Path(dir_okay=False), help="policy YAML file")
@click.option(
    "--sample",
    "sample_only",
    is_flag=True,
    help="show only the bundled sample workspaces (no project needed)",
)
@click.option(
    "--with-sample/--no-sample",
    "with_sample",
    default=True,
    show_default=True,
    help="also offer the bundled sample workspaces next to your project",
)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="interface; keep loopback outside containers",
)
@click.option("--open/--no-open", "open_browser", default=True, help="open a browser tab")
@_errors
def console_cmd(
    directory: Path | None,
    candidate_dir: Path | None,
    policy_path: str | None,
    sample_only: bool,
    with_sample: bool,
    port: int,
    host: str,
    open_browser: bool,
) -> None:
    """Serve the local read-only console for your project's cassettes (needs replex[console]).

    DIRECTORY defaults to the configured cassette_dir. The bundled sample workspaces are offered
    next to it, so there is always something to explore.
    """
    try:
        from replex.console.server import project_workspace, sample_workspaces, serve
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise click.ClickException(
            "the console needs the optional extra: pip install 'replex[console]'"
        ) from exc
    workspaces = []
    if not sample_only:
        cfg = _config()
        workspaces.append(
            project_workspace(
                directory or cfg.cassette_path,
                candidate_dir or cfg.candidate_path,
                Path(policy_path) if policy_path else cfg.policy_path,
                cfg.history_path,
                pricing=Pricing.from_config(cfg),
            )
        )
    if sample_only or with_sample:
        workspaces.extend(sample_workspaces())
    serve(  # pragma: no cover - long-running server
        workspaces=workspaces,
        host=host,
        port=port,
        open_browser=open_browser and not os.environ.get("CI"),
    )


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
