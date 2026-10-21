"""The suite file: an agent callable, scenarios and the expectations they must meet."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from replex.cassette.storage import sanitise_name
from replex.errors import ConfigError
from replex.trace.model import Trace

DEFAULT_SUITE = "replex.suite.yaml"
_AGENT = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")


class SuiteError(ConfigError):
    """A suite file is missing, malformed, or names an agent that cannot be imported."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolsExpectation(_Strict):
    """The tools the agent calls, compared with ``sequence`` as ``assert_tool_sequence`` does."""

    sequence: list[str]
    mode: Literal["exact", "subsequence", "same_set"] = "exact"


class FinalOutputExpectation(_Strict):
    """The final answer: a substring, a regular expression and/or a JSON Schema."""

    contains: str | None = None
    matches: str | None = None
    json_schema: dict[str, Any] | None = None

    @field_validator("matches")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc
        return value

    @field_validator("json_schema", mode="before")
    @classmethod
    def _schema_file(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, str):
            return value
        base = (info.context or {}).get("base_dir", Path.cwd())
        path = Path(value) if Path(value).is_absolute() else Path(base) / value
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load JSON schema {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"JSON schema {path} must be an object")
        return data

    @model_validator(mode="after")
    def _not_empty(self) -> FinalOutputExpectation:
        if self.contains is None and self.matches is None and self.json_schema is None:
            raise ValueError("set at least one of contains, matches or json_schema")
        return self


class Expectations(_Strict):
    """Checks on a scenario's trace. Each one maps onto a :class:`~replex.Trace` assertion."""

    tools: ToolsExpectation | None = None
    no_tools: list[str] = Field(default_factory=list)
    max_steps: PositiveInt | None = None
    max_tokens: PositiveInt | None = None
    final_output: FinalOutputExpectation | None = None

    @field_validator("tools", mode="before")
    @classmethod
    def _tool_list(cls, value: Any) -> Any:
        return {"sequence": value} if isinstance(value, list) else value

    @field_validator("no_tools", mode="before")
    @classmethod
    def _one_pattern(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    def overlaid(self, other: Expectations) -> Expectations:
        """These expectations with every key that ``other`` sets replaced by its value."""
        return self.model_copy(update={k: getattr(other, k) for k in other.model_fields_set})

    def check(self, trace: Trace) -> list[str]:
        """Run every expectation against ``trace``; return the failure messages."""
        checks: list[tuple[Any, ...]] = []
        if self.tools is not None:
            checks.append((trace.assert_tool_sequence, list(self.tools.sequence), self.tools.mode))
        checks += [(trace.assert_no_tool, pattern) for pattern in self.no_tools]
        if self.max_steps is not None:
            checks.append((trace.assert_max_steps, self.max_steps))
        if self.max_tokens is not None:
            checks.append((trace.assert_max_tokens, self.max_tokens))
        if self.final_output is not None:
            out = self.final_output
            checks.append((trace.assert_final_output, out.contains, out.matches, out.json_schema))
        failures = []
        for check, *args in checks:
            try:
                check(*args)
            except AssertionError as exc:
                failures.append(str(exc))
        return failures


class Scenario(_Strict):
    """One scenario: its name doubles as the cassette name."""

    name: str
    input: Any = Field(...)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expect: Expectations = Field(default_factory=Expectations)

    @field_validator("name")
    @classmethod
    def _cassette_name(cls, value: str) -> str:
        if not value or sanitise_name(value) != value:
            raise ValueError(
                "use letters, digits, '_', '.' and '-' only: the name is also the cassette name"
            )
        return value


class Suite(_Strict):
    """A suite file: the agent to call and the scenarios to run it on."""

    version: Literal[1] = 1
    agent: str
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expect: Expectations = Field(default_factory=Expectations)
    scenarios: list[Scenario] = Field(min_length=1)
    _path: Path = PrivateAttr(default_factory=lambda: Path(DEFAULT_SUITE))

    @field_validator("agent")
    @classmethod
    def _agent_spec(cls, value: str) -> str:
        if not _AGENT.match(value):
            raise ValueError("expected 'package.module:function'")
        return value

    @field_validator("scenarios")
    @classmethod
    def _unique(cls, value: list[Scenario]) -> list[Scenario]:
        seen: set[str] = set()
        for scenario in value:
            if scenario.name in seen:
                raise ValueError(f"scenario name {scenario.name!r} is used twice")
            seen.add(scenario.name)
        return value

    @property
    def path(self) -> Path:
        """The file this suite was loaded from."""
        return self._path

    def expectations(self, scenario: Scenario) -> Expectations:
        """The suite-wide expectations overlaid with the scenario's own."""
        return self.expect.overlaid(scenario.expect)

    def agent_kwargs(self, scenario: Scenario) -> dict[str, Any]:
        """Keyword arguments for the agent: the suite's, then the scenario's."""
        return {**self.kwargs, **scenario.kwargs}

    def select(self, names: Sequence[str]) -> list[Scenario]:
        """The scenarios called ``names`` (all of them when ``names`` is empty), in file order."""
        if not names:
            return list(self.scenarios)
        known = {s.name for s in self.scenarios}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise SuiteError(
                f"{self.path}: no scenario named {', '.join(map(repr, unknown))}; "
                f"the suite has {', '.join(sorted(known))}"
            )
        return [s for s in self.scenarios if s.name in names]


def _line(node: yaml.Node, loc: tuple[int | str, ...]) -> int:
    """The 1-based line of the YAML node at ``loc``, or of its closest existing parent."""
    for part in loc:
        child: yaml.Node | None = None
        if isinstance(node, yaml.MappingNode):
            child = next((v for k, v in node.value if k.value == part), None)
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            child = node.value[part] if part < len(node.value) else None
        if child is None:
            break
        node = child
    return node.start_mark.line + 1


def _where(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}" if out else str(part)
    return out or "suite"


def parse_suite(text: str, *, path: str | os.PathLike[str] = DEFAULT_SUITE) -> Suite:
    """Validate suite YAML ``text``. Errors name the file, line and field."""
    path = Path(path)
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{path}:{mark.line + 1}" if mark is not None else str(path)
        raise SuiteError(f"{where}: invalid YAML: {getattr(exc, 'problem', None) or exc}") from exc
    if node is None or not isinstance(data, dict):
        raise SuiteError(f"{path}: a suite must be a mapping with 'agent' and 'scenarios'")
    try:
        suite = Suite.model_validate(data, context={"base_dir": path.parent})
    except ValidationError as exc:
        lines = [
            f"{path}:{_line(node, e['loc'])}: {_where(e['loc'])}: {e['msg']}"
            for e in exc.errors()[:10]
        ]
        raise SuiteError("invalid suite\n" + "\n".join(lines)) from exc
    suite._path = path
    return suite


def load_suite(path: str | os.PathLike[str]) -> Suite:
    """Load and validate a suite file. Relative schema paths resolve against its directory."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SuiteError(f"suite file not found: {p}") from exc
    except OSError as exc:
        raise SuiteError(f"cannot read suite file {p}: {exc}") from exc
    return parse_suite(text, path=p)
