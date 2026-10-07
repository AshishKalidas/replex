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
)
from replex.config import Config, check_ci_guard, load_config
from replex.errors import CassetteError, ReplexError

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

    d = directory or _config().cassette_path
    rows: list[dict[str, Any]] = []
    for path in list_cassettes(d):
        try:
            c = load(path)
        except CassetteError as exc:
            rows.append({"name": name_from_path(path), "error": str(exc)})
            continue
        t = build_trace(c)
        rows.append(
            {
                "name": c.name,
                "file": str(path),
                "provider": c.meta.provider,
                "model": c.meta.model,
                "steps": t.steps_count,
                "tool_calls": len(t.tool_calls),
                "tokens": t.usage.total,
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
    for col in ("name", "provider", "model", "steps", "tools", "tokens", "date"):
        table.add_column(col, justify="right" if col in ("steps", "tools", "tokens") else "left")
    for r in rows:
        if "error" in r:
            table.add_row(r["name"], "[red]invalid[/red]", r["error"][:60], "", "", "", "")
            continue
        table.add_row(
            r["name"],
            r["provider"],
            r["model"],
            str(r["steps"]),
            str(r["tool_calls"]),
            str(r["tokens"]),
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
    trace = build_trace(load(_resolve(cassette, cfg.cassette_path, cfg.candidate_path)))
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
    ta = build_trace(load(_resolve(a, cfg.cassette_path, cfg.candidate_path)))
    tb = build_trace(load(_resolve(b, cfg.candidate_path, cfg.cassette_path)))
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
    report = compare(base, cand, _policy(policy_path, cfg), name=name_from_path(base))
    if as_json:
        click.echo(report.to_json(), nl=False)
    elif as_markdown:
        click.echo(report.to_markdown())
    else:
        render_report(report, _console())
    if not report.passed:
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
@click.option("--policy", "policy_path", type=click.Path(dir_okay=False), help="policy YAML file")
@click.option("--fail-on-drift", is_flag=True, help="exit 1 if any pair fails its policy")
@_errors
def report_cmd(
    baseline_dir: Path,
    candidate_dir: Path,
    html_out: Path | None,
    md_out: Path | None,
    json_out: Path | None,
    policy_path: str | None,
    fail_on_drift: bool,
) -> None:
    """Compare every cassette in CANDIDATE_DIR with its baseline and write reports."""
    from replex.drift.compare import compare
    from replex.drift.report import html_report, reports_markdown

    cfg = _config()
    policy = _policy(policy_path, cfg)
    baselines = {name_from_path(p): p for p in list_cassettes(baseline_dir)}
    reports = []
    for cand in list_cassettes(candidate_dir):
        name = name_from_path(cand)
        if name in baselines:
            reports.append(compare(baselines[name], cand, policy, name=name))
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
        Path(md_out).parent.mkdir(parents=True, exist_ok=True)
        Path(md_out).write_text(reports_markdown(reports), encoding="utf-8")
        console.print(f"Markdown summary: {Path(md_out).resolve()}")
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


# --------------------------------------------------------------------------- approve


@cli.command("approve")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_errors
def approve_cmd(name: str, as_json: bool) -> None:
    """Promote the drift candidate NAME to be the new baseline."""
    cfg = _config()
    check_ci_guard("rewrite")
    src = cassette_path(cfg.candidate_path, name)
    if not src.is_file():
        raise click.ClickException(f"no candidate {src}; run the drift job first")
    load(src)  # validate before promoting
    dst = cassette_path(cfg.cassette_path, name)
    atomic_write_text(dst, src.read_text(encoding="utf-8"))
    src.unlink()
    if as_json:
        _emit_json({"approved": name, "baseline": str(dst)})
    else:
        click.echo(f"approved {name}: {dst} now holds the new baseline (commit it)")


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
    port: int,
    host: str,
    open_browser: bool,
) -> None:
    """Serve the local read-only console for DIRECTORY (needs replex[console])."""
    try:
        from replex.console.server import serve
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise click.ClickException(
            "the console needs the optional extra: pip install 'replex[console]'"
        ) from exc
    cfg = _config()
    serve(  # pragma: no cover - long-running server
        baseline_dir=directory or cfg.cassette_path,
        candidate_dir=candidate_dir or cfg.candidate_path,
        policy_path=Path(policy_path) if policy_path else cfg.policy_path,
        host=host,
        port=port,
        open_browser=open_browser and not os.environ.get("CI"),
    )


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
