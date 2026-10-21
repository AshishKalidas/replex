"""Where repeated runs live: ``<candidate_dir>/runs/<name>/run-NN.replex.json``.

Repeated runs sit in their own directory under the candidate directory, so they never mix
with the single latest run (``<candidate_dir>/<name>.replex.json``) that drift review and
``replex approve`` work on.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from replex.cassette.storage import SUFFIX, cassette_path, load, sanitise_name
from replex.consistency.analysis import Categoriser, ConsistencyReport, analyse
from replex.errors import CassetteNotFound
from replex.providers import build_trace

RUNS_DIR = "runs"
_RUN_FILE = re.compile(rf"run-(\d+){re.escape(SUFFIX)}")


def runs_dir(candidate_dir: str | os.PathLike[str], name: str) -> Path:
    """The directory holding the repeated runs of scenario ``name``."""
    return Path(candidate_dir) / RUNS_DIR / sanitise_name(name)


def run_path(candidate_dir: str | os.PathLike[str], name: str, number: int) -> Path:
    """The file for run ``number`` (1-based) of scenario ``name``."""
    if number < 1:
        raise ValueError(f"run numbers start at 1, got {number}")
    return runs_dir(candidate_dir, name) / f"run-{number:02d}{SUFFIX}"


def _number(path: Path) -> int | None:
    match = _RUN_FILE.fullmatch(path.name)
    return int(match.group(1)) if match else None


def list_runs(candidate_dir: str | os.PathLike[str], name: str) -> list[Path]:
    """The run files of scenario ``name``, in run order."""
    d = runs_dir(candidate_dir, name)
    if not d.is_dir():
        return []
    numbered = [(n, p) for p in d.iterdir() if (n := _number(p)) is not None]
    return [p for _, p in sorted(numbered)]


def prune_runs(candidate_dir: str | os.PathLike[str], name: str, keep: int) -> None:
    """Delete runs numbered above ``keep``, left over from an earlier, longer repetition."""
    for path in list_runs(candidate_dir, name):
        number = _number(path)
        if number is not None and number > keep:
            path.unlink(missing_ok=True)


def scenarios_with_runs(candidate_dir: str | os.PathLike[str]) -> list[str]:
    """Names of the scenarios that have at least one repeated run, sorted."""
    root = Path(candidate_dir) / RUNS_DIR
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and list_runs(candidate_dir, d.name))


def analyse_runs(
    candidate_dir: str | os.PathLike[str],
    name: str,
    *,
    baseline_dir: str | os.PathLike[str] | None = None,
    categorise: Categoriser | None = None,
) -> ConsistencyReport | None:
    """Analyse the stored runs of ``name`` against its baseline, if there is one.

    Returns ``None`` when the scenario has no repeated runs.
    """
    paths = list_runs(candidate_dir, name)
    if not paths:
        return None
    baseline = None
    if baseline_dir is not None:
        with contextlib.suppress(CassetteNotFound):
            baseline = build_trace(load(cassette_path(baseline_dir, name)))
    return analyse(
        [build_trace(load(p)) for p in paths],
        baseline=baseline,
        labels=[p.name[: -len(SUFFIX)] for p in paths],
        categorise=categorise,
        name=name,
    )
