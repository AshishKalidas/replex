"""Drift policies: which behavioural differences fail, warn or are ignored."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from replex.errors import PolicyError

Severity = Literal["fail", "warn", "ignore"]


class _Check(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSequenceCheck(_Check):
    """Order of tool calls. ``subsequence``: the baseline's tools appear in order (extras ok)."""

    mode: Literal["exact", "subsequence", "same_set", "ignore"] = "exact"
    severity: Severity = "fail"


class ToolArgsOverride(_Check):
    """Per-tool settings for :class:`ToolArgsCheck`."""

    ignore_keys: list[str] = Field(default_factory=list)
    tolerance: float | None = None


class ToolArgsCheck(_Check):
    """Arguments of aligned tool calls. ``ignore_keys`` accepts dotted paths."""

    mode: Literal["exact", "ignore"] = "exact"
    severity: Severity = "fail"
    ignore_keys: list[str] = Field(default_factory=list)
    tolerance: float = 0.0
    tools: dict[str, ToolArgsOverride] = Field(default_factory=dict)


class NewToolsCheck(_Check):
    """The candidate calls a tool that the baseline never called."""

    severity: Severity = "fail"


class ForbiddenToolsCheck(_Check):
    """Glob patterns that must never appear among the candidate's tool calls."""

    patterns: list[str] = Field(default_factory=list)
    severity: Severity = "fail"


class FinalOutputCheck(_Check):
    """The final answer. ``judge`` asks a model whether the answers are equivalent."""

    mode: Literal["exact", "json_equal", "json_schema", "similarity", "judge", "ignore"] = (
        "similarity"
    )
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    schema_: dict[str, Any] | str | None = Field(default=None, alias="schema")
    similarity_backend: Literal["difflib", "embeddings"] = "difflib"
    embedding_model: str = "all-MiniLM-L6-v2"
    model: str = Field(default="anthropic/claude-haiku-5.5", min_length=1)
    criteria: str = Field(
        default="Same decision and same facts for the user; wording and tone may differ.",
        min_length=1,
    )
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    judgements_dir: str = "judgements"
    severity: Severity = "warn"

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StepsCheck(_Check):
    """Maximum increase in step count."""

    max_increase: int = Field(default=1, ge=0)
    severity: Severity = "warn"


class TokensCheck(_Check):
    """Maximum percentage increase in total tokens."""

    max_increase_pct: float = Field(default=25.0, ge=0.0)
    severity: Severity = "warn"


class LatencyCheck(_Check):
    """Latency is reported only; it never fails. ``severity: ignore`` hides it."""

    severity: Literal["info", "ignore"] = "info"


class Checks(_Check):
    """The full set of checks applied to one cassette."""

    tool_sequence: ToolSequenceCheck = Field(default_factory=ToolSequenceCheck)
    tool_args: ToolArgsCheck = Field(default_factory=ToolArgsCheck)
    new_tools: NewToolsCheck = Field(default_factory=NewToolsCheck)
    forbidden_tools: ForbiddenToolsCheck = Field(default_factory=ForbiddenToolsCheck)
    final_output: FinalOutputCheck = Field(default_factory=FinalOutputCheck)
    steps: StepsCheck = Field(default_factory=StepsCheck)
    tokens: TokensCheck = Field(default_factory=TokensCheck)
    latency: LatencyCheck = Field(default_factory=LatencyCheck)


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Policy(BaseModel):
    """A drift policy: default checks plus per-cassette overrides."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    defaults: Checks = Field(default_factory=Checks)
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)

    @property
    def base_dir(self) -> Path:
        """Directory that relative schema and judgement paths are resolved against."""
        return self._base_dir

    def for_cassette(self, name: str) -> Checks:
        """The effective checks for one cassette (defaults deep-merged with its override)."""
        override = self.overrides.get(name)
        if not override:
            return self.defaults
        merged = _deep_merge(self.defaults.model_dump(by_alias=True), override)
        try:
            return Checks.model_validate(merged)
        except ValidationError as exc:
            raise PolicyError(f"invalid override for {name!r}: {_errors(exc)}") from exc

    def resolve_schema(self, schema: dict[str, Any] | str) -> dict[str, Any]:
        """Load a schema given inline or as a path relative to the policy file."""
        if isinstance(schema, dict):
            return schema
        path = Path(schema)
        if not path.is_absolute():
            path = self._base_dir / path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyError(f"cannot load JSON schema {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PolicyError(f"JSON schema {path} must be an object")
        return data


def _errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])


def parse_policy(
    data: Any, *, base_dir: str | os.PathLike[str] | None = None, source: str = "policy"
) -> Policy:
    """Validate a policy mapping (as loaded from YAML)."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PolicyError(f"{source}: a policy must be a mapping")
    try:
        policy = Policy.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(f"{source}: {_errors(exc)}") from exc
    policy._base_dir = Path(base_dir) if base_dir else Path.cwd()
    for name in policy.overrides:
        policy.for_cassette(name)  # validate every override up front
    return policy


def load_policy(path: str | os.PathLike[str] | None) -> Policy:
    """Load a policy file. ``None`` or a missing default file gives the default policy."""
    if path is None:
        return Policy()
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyError(f"policy file not found: {p}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{p}: invalid YAML ({exc})") from exc
    return parse_policy(data, base_dir=p.parent, source=str(p))
