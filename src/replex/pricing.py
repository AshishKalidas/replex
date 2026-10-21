"""What a run costs: per-model token prices and the cost of each model step.

Prices are in US dollars per million tokens. A small built-in table covers common models;
``[tool.replex] prices`` adds or replaces entries. A cost the provider reported in the recorded
response (OpenRouter's ``usage.cost``) always wins over the table. A model with no price is
reported as unknown, never estimated.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from replex.trace.model import ModelStep, Trace

if TYPE_CHECKING:
    from replex.config import Config


class ModelPrice(BaseModel):
    """Token prices for one model, in US dollars per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: float = Field(ge=0.0)
    output: float = Field(ge=0.0)


#: Standard first-party rates, keyed by canonical model name (see :func:`canonical_model`).
#: Cache reads and writes, batch discounts and long-context surcharges are not modelled.
BUILTIN_PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0),
    "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0),
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50),
    "claude-opus-5": ModelPrice(input=5.0, output=25.0),
    "claude-sonnet-5": ModelPrice(input=2.0, output=10.0),
    "claude-sonnet-4-6": ModelPrice(input=3.0, output=15.0),
    "claude-haiku-4-5": ModelPrice(input=1.0, output=5.0),
    "gpt-4o": ModelPrice(input=2.50, output=10.0),
    "gpt-4o-mini": ModelPrice(input=0.15, output=0.60),
    "gpt-4-1": ModelPrice(input=2.0, output=8.0),
    "gpt-4-1-mini": ModelPrice(input=0.40, output=1.60),
    "gpt-4-1-nano": ModelPrice(input=0.10, output=0.40),
}

_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def canonical_model(name: str) -> str:
    """The name used to look a model up in a price table.

    Lower-cases, drops provider prefixes (``anthropic/claude-sonnet-5.5``), a ``@version``
    suffix and a trailing release date (``-20260101`` or ``-2026-01-01``), and writes version
    dots as dashes, so ``anthropic/claude-sonnet-5.5`` and ``claude-sonnet-5-5`` are the same.
    """
    n = name.strip().lower().rsplit("/", 1)[-1].split("@", 1)[0]
    return _DATE_SUFFIX.sub("", n).replace(".", "-")


def format_usd(amount: float | None) -> str:
    """A cost for people: ``$0.0123``, ``$1.20``, ``<$0.0001`` or ``unknown``."""
    if amount is None:
        return "unknown"
    if amount == 0:
        return "$0.00"
    if amount < 0.0001:
        return "<$0.0001"
    if amount < 1:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


class Pricing:
    """Looks up model prices and puts a cost on traces.

    Args:
        prices: Extra or replacement prices by model name, as :class:`ModelPrice` objects or
            mappings with ``input`` and ``output`` (dollars per million tokens). They take
            precedence over the built-in table.
        builtin: Whether to fall back to :data:`BUILTIN_PRICES`.
    """

    def __init__(
        self,
        prices: Mapping[str, ModelPrice | Mapping[str, Any]] | None = None,
        *,
        builtin: bool = True,
    ) -> None:
        own = {k: ModelPrice.model_validate(v) for k, v in (prices or {}).items()}
        self._exact = own
        self._canonical = dict(BUILTIN_PRICES) if builtin else {}
        self._canonical.update({canonical_model(k): v for k, v in own.items()})

    @classmethod
    def from_config(cls, config: Config) -> Pricing:
        """The built-in table with the ``prices`` from ``[tool.replex]`` applied."""
        return cls(config.prices)

    def price_for(self, model: str) -> ModelPrice | None:
        """The price of ``model``, or ``None`` when it is not known."""
        return self._exact.get(model) or self._canonical.get(canonical_model(model))

    def step_cost(self, step: ModelStep) -> float | None:
        """The cost of one model call in dollars, or ``None`` when it cannot be known.

        The provider's own figure wins. A step that recorded no tokens costs nothing.
        """
        if step.reported_cost is not None:
            return step.reported_cost
        if step.usage.total == 0:
            return 0.0
        price = self.price_for(step.model)
        if price is None:
            return None
        dollars = (
            step.usage.input_tokens * price.input + step.usage.output_tokens * price.output
        ) / 1_000_000
        return round(dollars, 9)

    def priced(self, trace: Trace) -> Trace:
        """A copy of ``trace`` with the cost of every model step and the total filled in.

        The total is ``None`` when any model step has no known cost; ``unpriced_models`` then
        names the models that need a price.
        """
        steps: list[Any] = []
        costs: list[float] = []
        unpriced: set[str] = set()
        for step in trace.steps:
            if isinstance(step, ModelStep):
                cost = self.step_cost(step)
                if cost is None:
                    unpriced.add(step.model)
                else:
                    costs.append(cost)
                step = step.model_copy(update={"cost": cost})
            steps.append(step)
        return trace.model_copy(
            update={
                "steps": steps,
                "cost": None if unpriced else round(sum(costs), 9),
                "unpriced_models": sorted(unpriced),
            }
        )


DEFAULT_PRICING = Pricing()
