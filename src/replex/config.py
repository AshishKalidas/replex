"""Configuration: ``[tool.replex]`` in ``pyproject.toml``, then environment, then options.

Precedence (lowest to highest): built-in defaults, ``pyproject.toml``, ``REPLEX_*``
environment variables, pytest command-line options, marker / ``session()`` arguments.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from replex.capture.matching import DEFAULT_IGNORE_FIELDS
from replex.errors import ConfigError, RecordingRefused
from replex.pricing import ModelPrice

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

Mode = Literal["replay", "record", "drift", "rewrite", "live"]
MODES: tuple[str, ...] = get_args(Mode)

_ENV: dict[str, str] = {
    "REPLEX_MODE": "mode",
    "REPLEX_CASSETTE_DIR": "cassette_dir",
    "REPLEX_CANDIDATE_DIR": "candidate_dir",
    "REPLEX_MATCH": "match",
    "REPLEX_TOOLS": "tools",
    "REPLEX_POLICY": "policy",
}


class Config(BaseModel):
    """Resolved RepleX settings. Relative paths are resolved against ``root``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    cassette_dir: str = "tests/cassettes"
    candidate_dir: str = ".replex/candidates"
    mode: Mode = "replay"
    match: Literal["strict", "sequence"] = "strict"
    tools: Literal["replay", "live", "auto"] | None = None
    intercept_hosts: list[str] = Field(default_factory=lambda: ["api.anthropic.com"])
    passthrough_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    ignore_request_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_FIELDS))
    redact_patterns: list[str | dict[str, str]] = Field(default_factory=list)
    redact_paths: list[str] = Field(default_factory=list)
    policy: str = "replex.policy.yaml"
    strict_unused: bool = True
    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    root: str = "."

    @field_validator("mode", "match", "tools", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    def path(self, value: str) -> Path:
        """Resolve a configured path against the project root."""
        p = Path(value)
        return p if p.is_absolute() else Path(self.root) / p

    @property
    def cassette_path(self) -> Path:
        """Absolute directory for baselines."""
        return self.path(self.cassette_dir)

    @property
    def candidate_path(self) -> Path:
        """Absolute directory for drift candidates."""
        return self.path(self.candidate_dir)

    @property
    def policy_path(self) -> Path:
        """Absolute path of the drift policy file."""
        return self.path(self.policy)

    def merged(self, **overrides: Any) -> Config:
        """A copy with non-``None`` overrides applied and validated."""
        data = self.model_dump()
        data.update({k: v for k, v in overrides.items() if v is not None})
        return _validate(data, "options")


def _validate(data: Mapping[str, Any], source: str) -> Config:
    try:
        return Config.model_validate(dict(data))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"invalid RepleX configuration ({source}): {problems}") from exc


def find_pyproject(start: str | os.PathLike[str] | None = None) -> Path | None:
    """The nearest ``pyproject.toml`` at or above ``start`` (default: the current directory)."""
    here = Path(start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        candidate = d / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def load_config(
    start: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Config:
    """Load configuration from the nearest ``pyproject.toml``, the environment and overrides."""
    env = os.environ if env is None else env
    data: dict[str, Any] = {}
    pyproject = find_pyproject(start)
    root = Path(start).resolve() if start else Path.cwd()
    if pyproject is not None:
        root = pyproject.parent
        try:
            parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {pyproject}: {exc}") from exc
        section = parsed.get("tool", {}).get("replex", {})
        if not isinstance(section, dict):
            raise ConfigError(f"{pyproject}: [tool.replex] must be a table")
        data.update(section)
    data["root"] = str(root)
    base = _validate(data, str(pyproject or "defaults"))
    from_env = {field: env[var] for var, field in _ENV.items() if env.get(var)}
    if from_env:
        base = base.merged(**from_env)
    return base.merged(**overrides) if overrides else base


def in_ci(env: Mapping[str, str] | None = None) -> bool:
    """True when the ``CI`` environment variable is set to anything but false-ish values."""
    env = os.environ if env is None else env
    return env.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def check_ci_guard(mode: str, env: Mapping[str, str] | None = None) -> None:
    """Refuse ``record``/``rewrite`` in CI unless ``REPLEX_ALLOW_RECORD_IN_CI=1``."""
    env = os.environ if env is None else env
    if mode in ("record", "rewrite") and in_ci(env) and env.get("REPLEX_ALLOW_RECORD_IN_CI") != "1":
        raise RecordingRefused(
            f"RepleX refuses mode={mode!r} in CI: CI must never silently create baselines. "
            f"Record locally and commit the cassettes, or set REPLEX_ALLOW_RECORD_IN_CI=1 "
            f"if this job is meant to record."
        )
