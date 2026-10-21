"""Drift results as runners present them: the failure message and the report file."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from replex.cassette.storage import atomic_write_text, cassette_path
from replex.config import Config
from replex.drift.compare import DriftReport, compare
from replex.drift.policy import Policy, load_policy
from replex.drift.report import html_report, reports_markdown
from replex.pricing import Pricing


def drift_failure_message(name: str, report: DriftReport, candidate: Path | None) -> str:
    """The failing findings of a drift run, where its candidate is and how to approve it."""
    lines = [f"RepleX drift check failed for {name!r}:"]
    lines += [f"  [{f.check}] {f.message}" for f in report.failures]
    lines.append(f"  candidate: {candidate}")
    if any(f.check == "blocked_tool" for f in report.failures):
        lines.append(
            "  the run stopped early, so it cannot be approved: fix the change, or re-record "
            "this scenario on purpose with --replex-mode=rewrite"
        )
    else:
        lines.append(f"  approve it with: replex approve {name}")
    return "\n".join(lines)


def write_drift_report(config: Config, names: Iterable[str], path: Path) -> Path:
    """Compare each named candidate with its baseline and write one report file.

    The format follows the suffix: ``.md`` for Markdown, ``.json`` for JSON, anything else for
    the single-file HTML report.
    """
    policy = load_policy(config.policy_path) if config.policy_path.is_file() else Policy()
    pricing = Pricing.from_config(config)
    reports = []
    for name in sorted(set(names)):
        base = cassette_path(config.cassette_path, name)
        cand = cassette_path(config.candidate_path, name)
        if base.is_file() and cand.is_file():
            reports.append(compare(base, cand, policy, name=name, pricing=pricing))
    suffix = path.suffix.lower()
    if suffix == ".md":
        text = reports_markdown(reports)
    elif suffix == ".json":
        text = json.dumps([r.to_dict() for r in reports], indent=2, sort_keys=True) + "\n"
    else:
        text = html_report(reports)
    return atomic_write_text(path, text).resolve()
