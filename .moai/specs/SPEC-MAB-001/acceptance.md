# Acceptance Criteria — SPEC-MAB-001 (MAB 무한매수법 / Infinite Buying Core)

`@SPEC:SPEC-MAB-001` `@TEST:SPEC-MAB-001`

All scenarios use Given/When/Then. Money/quantity values are `Decimal`; example strings represent
`Decimal` literals (e.g. `"52.50"` → `Decimal("52.50")`). Share quantities are floored toward zero
(`ROUND_FLOOR`) before the zero-check, and all `qty`/`limit_price` values are quantized to 2 decimal
places at the `Order` boundary via `quantize_money` (`ROUND_HALF_UP`). Every MAB order has
`order_type == OrderType.LOC`.

The standing example uses SOXL-like parameters: `target_pct = "0.20"`, `alpha = "0.05"`,
`split_ratio = "0.50"`, `n_splits = 40`, `ticker = "SOXL"`, `account_seq = "0002"`, and the default
`halftime_rule = "standard"`, `version = "v2.2"` unless a scenario overrides them.

## Scenario 1 — Mid-game day: two LOC buys + LOC profit-take (REQ-MAB-001-R1)

**Given** a 전반전 day with `round_idx = 5` (`5 <= n_splits // 2 = 20`), `holdings = "30.00"`,
**And** `seed = "8000.00"`, `n_splits = 40` so the per-round budget is `seed/n_splits = "200.00"`,
**And** `seed_remaining = "8000.00"` (the cap does not bind), `avg_price = "50.00"`,
**And** `target_pct = "0.20"`, `alpha = "0.05"`, `split_ratio = "0.50"`,
**When** `mab_daily_orders(avg_price="50.00", holdings="30.00", seed="8000.00", seed_remaining="8000.00", round_idx=5, n_splits=40, target_pct="0.20", alpha="0.05", split_ratio="0.50", halftime_rule="standard", version="v2.2", ticker="SOXL", account_seq="0002")`
is evaluated,
**Then** the budget splits into `budget_a = 200 * 0.50 = "100.00"` and `budget_b = 200 * 0.50 = "100.00"`,
**And** leg A (near-average) is an LOC BUY at `limit_price = avg_price = "50.00"` with
`qty_a = floor(100 / 50) = "2.00"`,
**And** leg B (step-up) is an LOC BUY at `limit_price = avg_price*(1 + alpha) = 50 * 1.05 = "52.50"`
with `qty_b = floor(100 / 52.50) = floor(1.904...) = "1.00"`,
**And** because `holdings = 30 > 0`, an LOC SELL profit-take of `qty = "30.00"` is placed at
`limit_price = avg_price*(1 + target_pct) = 50 * 1.20 = "60.00"`,
**And** the result is a 3-element list `[BUY 2.00 @ 50.00, BUY 1.00 @ 52.50, SELL 30.00 @ 60.00]`,
all with `order_type == OrderType.LOC`.

## Scenario 2 — Profit-take price = avg_price · (1 + target_pct) (REQ-MAB-001-R1)

**Given** `avg_price = "50.00"`, `holdings = "30.00"`, `target_pct = "0.20"` (SOXL),
**When** `mab_daily_orders(...)` is evaluated,
**Then** the SELL profit-take `limit_price == 50 * 1.20 == "60.00"`,
**And (TQQQ variant)** with `avg_price = "100.00"` and `target_pct = "0.10"` (resolved from the
registry for a TQQQ-like instrument [T10]), the SELL profit-take `limit_price == 100 * 1.10 == "110.00"`,
**And** `target_pct` is never hardcoded: it is resolved through
`InstrumentRegistry.resolve_target_pct(ticker, explicit)` (explicit override → instrument default →
error) [T10].

## Scenario 3 — Second buy price = avg_price · (1 + alpha) (REQ-MAB-001-R1)

**Given** `avg_price = "50.00"`, `alpha = "0.05"`, a 전반전 day (`round_idx = 5`),
**When** `mab_daily_orders(...)` is evaluated,
**Then** leg A's `limit_price == avg_price == "50.00"`,
**And** leg B's `limit_price == avg_price*(1 + alpha) == 50 * 1.05 == "52.50"`,
**And (alpha sensitivity)** with `alpha = "0.10"`, leg B's `limit_price == 50 * 1.10 == "55.00"` while
leg A stays at `"50.00"` (two price points secure a minimum close-based fill).

## Scenario 4 — seed_remaining below the per-round budget ⇒ buy capped (REQ-MAB-001-R3)

**Given** `seed = "8000.00"`, `n_splits = 40` so `seed/n_splits = "200.00"`,
**And** `seed_remaining = "120.00"` (less than the per-round budget), `avg_price = "50.00"`,
`split_ratio = "0.50"`, `alpha = "0.05"`, a 전반전 day (`round_idx = 5`), `holdings = "0.00"`,
**When** `mab_daily_orders(...)` is evaluated,
**Then** the effective budget is capped: `budget = min(200, 120) = "120.00"`,
**And** `budget_a = 120 * 0.50 = "60.00"` → leg A `qty = floor(60 / 50) = "1.00"` at `"50.00"`,
**And** `budget_b = 120 * 0.50 = "60.00"` → leg B `qty = floor(60 / 52.50) = floor(1.142...) = "1.00"`
at `"52.50"`,
**And** the total buy cash committed `(1 * 50) + (1 * 52.50) = "102.50"` does **not** exceed
`seed_remaining = "120.00"` (the cap holds),
**And** no profit-take is placed because `holdings = 0`.

## Scenario 5 — seed_remaining <= 0 ⇒ no buys (sell may remain) (REQ-MAB-001-R3)

**Given** `seed_remaining = "0.00"` (the cycle's cash is exhausted for buying), `holdings = "30.00"`,
`avg_price = "50.00"`, `target_pct = "0.20"`, a 전반전 day (`round_idx = 5`),
**When** `mab_daily_orders(...)` is evaluated,
**Then** **no BUY order is emitted** (neither leg A nor leg B),
**And** because `holdings = 30 > 0`, the LOC SELL profit-take of `"30.00"` at
`limit_price = 50 * 1.20 = "60.00"` is still emitted,
**And** the result is a 1-element list `[SELL 30.00 @ 60.00]`, `order_type == OrderType.LOC`.

## Scenario 6 — Seed exhausted ⇒ quarter-sell = floor(holdings / 4), LOC (REQ-MAB-001-R4)

**Given** the seed is exhausted after `n_splits` buys and `holdings = "40.00"`,
**When** `mab_on_seed_exhausted(holdings="40.00", version="v2.2", ticker="SOXL", account_seq="0002")`
is evaluated,
**Then** the returned `Order` is an LOC SELL with `qty = floor(40 / 4) = "10.00"`,
**And** `order.side == Side.SELL` and `order.order_type == OrderType.LOC`,
**And (flooring check)** with `holdings = "41.00"`, the quarter-sell is
`floor(41 / 4) = floor(10.25) = "10.00"` (floored toward zero; never oversells beyond the quarter),
**And** the v2.x **quarter-stop-loss** variant is noted as provisional [T9]; the default `version="v2.2"`
returns the seed-exhausted quarter-sell defined here.

## Scenario 7 — Edge: holdings = 0 ⇒ no profit-take (REQ-MAB-001-R1 / R3)

**Given** `holdings = "0.00"`, `seed = "8000.00"`, `n_splits = 40`, `seed_remaining = "8000.00"`,
`avg_price = "50.00"`, `alpha = "0.05"`, `split_ratio = "0.50"`, a 전반전 day (`round_idx = 5`),
**When** `mab_daily_orders(...)` is evaluated,
**Then** the two LOC BUY legs are emitted (leg A `"2.00" @ "50.00"`, leg B `"1.00" @ "52.50"`),
**And** **no SELL profit-take** is emitted because `holdings <= 0` (there is nothing to sell),
**And** the result is a 2-element list of LOC BUY orders only.

## Scenario 8 — 후반전 halftime rule: leg B dropped (REQ-MAB-001-R2, PROVISIONAL [T9])

**Given** a 후반전 day with `round_idx = 25` (`25 > n_splits // 2 = 20`), `holdings = "30.00"`,
`seed = "8000.00"`, `n_splits = 40`, `seed_remaining = "8000.00"`, `avg_price = "50.00"`,
`alpha = "0.05"`, `split_ratio = "0.50"`, `target_pct = "0.20"`, `halftime_rule = "standard"`,
**When** `mab_daily_orders(...)` is evaluated,
**Then** by the provisional `"standard"` rule [T9], 후반전 **reduces buy aggressiveness**: only the
near-average leg A `"2.00" @ "50.00"` is placed and the step-up leg B is **dropped**,
**And** the LOC SELL profit-take of `"30.00" @ "60.00"` is still placed (the SELL is phase-independent),
**And** the result is `[BUY 2.00 @ 50.00, SELL 30.00 @ 60.00]`,
**And (boundary)** at `round_idx = 20` (`== n_splits // 2`, the last 전반전 round) **both** buy legs are
placed; at `round_idx = 21` (first 후반전 round) only leg A is placed — confirming the split point is
`round_idx <= n_splits // 2` for 전반전.

## Scenario 9 — Dust suppression: a buy leg's qty rounds to 0 ⇒ dropped (REQ-MAB-001-R3)

**Given** a 전반전 day (`round_idx = 5`) with a tiny budget: `seed = "400.00"`, `n_splits = 40` so
`seed/n_splits = "10.00"`, `seed_remaining = "400.00"`, `avg_price = "50.00"`, `alpha = "0.05"`,
`split_ratio = "0.50"`, `holdings = "0.00"`,
**When** `mab_daily_orders(...)` is evaluated,
**Then** `budget = "10.00"`, so `budget_a = "5.00"` → `qty_a = floor(5 / 50) = floor(0.1) = 0` and
`budget_b = "5.00"` → `qty_b = floor(5 / 52.50) = 0`,
**And** because each leg's quantity floors to `0`, **neither** leg is emitted (no zero/dust LOC order),
**And** with `holdings = 0` there is no profit-take either, so the result is an **empty list `[]`**,
**And (partial-dust check)** if only leg B floors to `0` (e.g. a larger `split_ratio` funds leg A but
starves leg B), leg A is still emitted and only leg B is dropped — the legs are dropped independently.

## Scenario 10 — MABStrategy conforms and wires both paths (REQ-MAB-001-R5)

**Given** a `MABStrategy()` instance,
**When** it is checked against the CORE-001 `Strategy` protocol and invoked,
**Then** `cadence == "daily"` and `ns == "mab"`, and it satisfies `Strategy` structurally (type-checks
under `mypy --strict`, passes `isinstance` since the protocol is `runtime_checkable`),
**And** `plan_orders(market, state, cfg)` reads `avg_price`/`holdings`/`seed_remaining`/`round_idx`
from `state` and `seed`/`n_splits`/`alpha`/`split_ratio`/`halftime_rule`/`version`/`account_seq`/`ticker`
from `cfg`, resolves `target_pct` via `resolve_target_pct` [T10], then calls `mab_daily_orders` for a
normal day,
**And** when the seed is exhausted (`round_idx > n_splits`) it instead returns the single
`mab_on_seed_exhausted` quarter-sell,
**And** every returned order has `order_type == OrderType.LOC`,
**And** `plan_orders` performs no IO, no clock access, and does not mutate `state` or `cfg`.

---

## Quality-Gate Criteria

A change implementing SPEC-MAB-001 is accepted only when all of the following hold:

- **Tested**: `uv run pytest --cov=src/ballast --cov-report=term-missing` passes with coverage
  **≥ 85%** (core domain targets ~100%); each scenario above has a corresponding test, and the
  `hypothesis` invariants hold:
  - never buy beyond `seed_remaining` (sum of BUY-leg cash `<= seed_remaining` and `<= seed/n_splits`),
  - profit-take SELL appears iff `holdings > 0`,
  - every order is LOC,
  - every `qty`/`limit_price` is a 2-place `Decimal` and re-quantizing is idempotent,
  - quarter-sell `== floor(holdings / 4)`,
  - no returned order has `qty == 0` (no dust).
- **Readable**: `uv run ruff check .` reports **0 errors**; naming follows the constitution
  (`snake_case` functions, `PascalCase` types, `*_qty`/price unit suffixes where applicable).
- **Unified**: `uv run ruff format --check .` is clean (no formatting diffs).
- **Secured / Type-safe**: `uv run mypy --strict src` reports **0 errors**; no `float` on any
  money/qty path; no hardcoded `target_pct`/`alpha`/`split_ratio`/tickers; no network/filesystem/
  `datetime.now()` inside `src/ballast/core/mab.py`; MAB emits `LOC` orders only.
- **Trackable**: `@SPEC:SPEC-MAB-001` is linked to `@TEST` / `@CODE` / `@DOC` tags; commits use
  conventional-commit format referencing SPEC-MAB-001.

## Definition of Done

- All ten scenarios pass as automated tests under `tests/unit/core/test_mab.py`.
- `src/ballast/core/mab.py` implements `mab_daily_orders`, `mab_on_seed_exhausted`, and `MABStrategy`
  per `spec.md`, reusing CORE-001 types without redefining them and mirroring `vr.py`'s order
  construction.
- LSP run-phase gates clean: errors = 0, type errors = 0, lint errors = 0.
- Coverage ≥ 85%; the mid-game worked example resolves to
  `[BUY 2.00 @ 50.00, BUY 1.00 @ 52.50, SELL 30.00 @ 60.00]` and the quarter-sell example resolves to
  an LOC SELL of `"10.00"`.
- The three TBD items remain parameters, not hardcoded values: T8 (LOC exposure — live-only, core
  stamps LOC regardless), T9 (version / halftime rule — provisional defaults `"v2.2"` / `"standard"`),
  T10 (`target_pct` via registry, `alpha` / `split_ratio` params + later sweep grid).

## Traceability

- `@SPEC:SPEC-MAB-001` → `@TEST:SPEC-MAB-001` (this file → `tests/unit/core/test_mab.py`) →
  `@CODE:SPEC-MAB-001` (`src/ballast/core/mab.py`) → `@DOC:SPEC-MAB-001`.
- Depends on `@SPEC:SPEC-CORE-001` (`src/ballast/core/{models,instrument,config,strategy}.py`).
