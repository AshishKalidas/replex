# Cost

Teams change prompts and models as much to spend less as to behave better. RepleX puts a price
on every recorded run, so a drift check can say what a change costs, not only what it does.

Costs are computed from what is already in the cassette. Nothing calls an API to find a price.

## Where a cost comes from

For each model step, in order:

1. **The provider's own figure.** Some providers return the cost of a call in the response
   (OpenRouter's `usage.cost`). It is used as is, because it already reflects caching, routing
   and discounts.
2. **Tokens times the model's price.** Input and output tokens from the recorded `usage`,
   priced from your `[tool.replex] prices` or the built-in table.
3. **Unknown.** A model with no price is never guessed. The step's cost is `None`, the run's
   total is `None`, and `unpriced_models` names the models that need a price.

A step that recorded no tokens and no cost (a failed request, for example) costs nothing.

## Built-in prices

US dollars per million tokens, at the providers' standard rates:

| Model | Input | Output |
|---|---|---|
| `claude-opus-5-5` | 4.00 | 20.00 |
| `claude-sonnet-5-5` | 2.00 | 10.00 |
| `claude-haiku-5-5` | 0.10 | 0.50 |
| `claude-opus-5` | 5.00 | 25.00 |
| `claude-sonnet-5` | 2.00 | 10.00 |
| `claude-sonnet-4-6` | 3.00 | 15.00 |
| `claude-haiku-4-5` | 1.00 | 5.00 |
| `gpt-4o` | 2.50 | 10.00 |
| `gpt-4o-mini` | 0.15 | 0.60 |
| `gpt-4.1` | 2.00 | 8.00 |
| `gpt-4.1-mini` | 0.40 | 1.60 |
| `gpt-4.1-nano` | 0.10 | 0.40 |

Names match loosely: case, provider prefixes (`anthropic/claude-sonnet-5.5` on OpenRouter),
`@version` and date suffixes (`-20260101`, `-2024-07-18`) are ignored, and version dots match
dashes. Variants with their own pricing, such as OpenRouter's `:free` models, do not match and
stay unknown.

The table does not model prompt-cache reads and writes, batch discounts, partner-platform
pricing (Bedrock, Vertex AI) or long-prompt rates (Claude Haiku 5.5 charges more above 100K
input tokens). When those matter, set your own prices or rely on the provider-reported cost.

## Your own prices

Add or replace prices in `pyproject.toml`, in dollars per million tokens:

```toml
[tool.replex]
prices = {"my-model" = {input = 1.0, output = 4.0}, "claude-sonnet-5-5" = {input = 2.0, output = 10.0}}
```

Your entries win over the built-in table and use the same name matching. pytest, the CLI and
the console all read them. From Python:

```python
from replex import Pricing, compare, load_config, load_trace

pricing = Pricing.from_config(load_config())          # or Pricing({"my-model": {"input": 1, "output": 4}})
trace = pricing.priced(load_trace("tests/cassettes/refund_request.replex.json"))
trace.cost, [s.cost for s in trace.model_calls]
report = compare(baseline, candidate, policy, pricing=pricing)
```

## Where you see it

- `replex ls` has a cost column (`cost` in `--json`); `replex show` prints the run's total and
  each model step's cost.
- `replex drift` and the Markdown report have a Cost row for both runs.
- In the console, a scenario's review page shows the cost of both runs under "In numbers", and
  the Overview says how the latest run's cost changed, for example "Latest run costs 94% less."
- The `cost` drift check warns (or fails) when a run gets more expensive than you allow; see
  [DRIFT.md](DRIFT.md#cost).
