# ballast

Foundation domain layer for the **ballast** leveraged-ETF rebalancing bot.

`src/ballast/core/` is a pure domain layer (no network, filesystem, or wall clock).
Time and prices are injected via the `Market` snapshot. All money/quantity values
use `Decimal` (never `float`), quantized to 2 decimal places with `ROUND_HALF_UP`.

Around the core sit `src/ballast/backtest/` (engine, costs, metrics, parameter
sweep / walk-forward), `src/ballast/adapters/` (Toss read-only adapter at the IO
boundary), and `src/ballast/orders/` — a broker-neutral, **dry-run-first** Order
Manager that turns strategy decisions into idempotent order intents and records
order plans without submitting real orders.

See [`.moai/specs/`](.moai/specs/) for the specs, plans, and acceptance criteria
(start with [`SPEC-CORE-001/`](.moai/specs/SPEC-CORE-001/)).

## Development

```bash
uv venv
uv pip install -e ".[dev]"

uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pytest --cov=src/ballast --cov-report=term-missing
```
