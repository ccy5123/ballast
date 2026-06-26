# ballast

Foundation domain layer for the **ballast** leveraged-ETF rebalancing bot.

`src/ballast/core/` is a pure domain layer (no network, filesystem, or wall clock).
Time and prices are injected via the `Market` snapshot. All money/quantity values
use `Decimal` (never `float`), quantized to 2 decimal places with `ROUND_HALF_UP`.

Around the core sit `src/ballast/backtest/` (engine, costs, metrics, parameter
sweep / walk-forward), `src/ballast/adapters/` (Toss adapters at the IO boundary —
read-only market/account access plus the `toss/orders.py` write adapter that
submits live orders over `POST /api/v1/orders`), `src/ballast/orders/` — a
broker-neutral, **dry-run-first** Order Manager that turns strategy decisions into
idempotent order intents — and `src/ballast/state/` — a State Store
(in-memory + SQLite behind one port) with a pure, idempotent reconciliation core
for durable, restart-safe state. The dry-run path now has a real live-submission
backend, and persisted state plus reconciliation give the foundation for
unattended operation.

The next direction is the **24/7 operating path**: an always-on worker
(Runner / Config → Scheduler → Notifier) driving cycles through the State Store,
with a Streamlit dashboard as its decoupled read/write peer.

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
