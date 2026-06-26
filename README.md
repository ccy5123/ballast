# ballast

Foundation domain layer for the **ballast** leveraged-ETF rebalancing bot.

`src/ballast/core/` is a pure domain layer (no network, filesystem, or wall clock).
Time and prices are injected via the `Market` snapshot. All money/quantity values
use `Decimal` (never `float`), quantized to 2 decimal places with `ROUND_HALF_UP`.

See [`.moai/specs/SPEC-CORE-001/`](.moai/specs/SPEC-CORE-001/) for the spec,
plan, and acceptance criteria.

## Development

```bash
uv venv
uv pip install -e ".[dev]"

uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pytest --cov=src/ballast --cov-report=term-missing
```
