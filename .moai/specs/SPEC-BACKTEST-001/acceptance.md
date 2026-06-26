# Acceptance Criteria — SPEC-BACKTEST-001 (Shared Backtest Engine + Cost Model + Metrics + Baselines)

`@SPEC:SPEC-BACKTEST-001` `@TEST:SPEC-BACKTEST-001`

All scenarios use Given/When/Then. Money/quantity values are `Decimal`; example strings represent
`Decimal` literals (e.g. `"100.00"` → `Decimal("100.00")`). Realized **cash**, **fees**, and **tax**
are `Decimal`, quantized to 2 places at money boundaries via the CORE-001 `quantize_money`
(`ROUND_HALF_UP`); curve/ratio math (CAGR/MDD/Sharpe) may use float/numpy but never feeds back onto
the `Decimal` ledger. Fills are **close-based** and use only the current bar (no look-ahead).

`OHLCBar` rows below give `(date, open, high, low, close, adj_close, fx_usdkrw)`. Unless a scenario
overrides it, FX is constant `fx_usdkrw = "1300.00"` and `adj_close = None`. The engine drives a
strategy only through `strategy.plan_orders(market, state, cfg)` and reads `strategy.cadence`.

## Scenario 1 — 3-bar series + trivial strategy → exact equity curve (REQ-BACKTEST-001-R1)

**Given** a 3-bar synthetic series
`[ (D1, 100,100,100,100, None, 1300), (D2, 100,110,90,110, None, 1300), (D3, 100,120,100,120, None, 1300) ]`,
**And** a trivial conforming **stub strategy** (`cadence = "daily"`, `ns = "vr"`) that on bar D1
returns a single LOC BUY of `qty = "10.00"` at `limit_price = "100.00"` and thereafter returns `[]`,
**And** `start_state` cash `pool = "1000.00"` (USD), commission `"0.00"`, slippage `0` bps,
**When** `run_backtest(stub, bars, cfg, start_state=..., costs=CostModel())` is evaluated,
**Then** on D1 the BUY fills at close `"100.00"`, committing `10 × 100 = "1000.00"` cash → `holdings
= "10.00"`, `cash = "0.00"`,
**And** the equity curve marks to market at each bar close:
`D1.equity_usd = 0 + 10×100 = "1000.00"`, `D2 = 10×110 = "1100.00"`, `D3 = 10×120 = "1200.00"`,
**And** the KRW curve is the USD curve × `1300`: `D1 = "1300000.00"`, `D2 = "1430000.00"`,
`D3 = "1560000.00"`,
**And** the `BacktestResult` is deterministic: re-running with identical inputs yields an identical
result.

## Scenario 2 — LOC BUY fills only when close <= limit (REQ-BACKTEST-001-R2)

**Given** an LOC BUY `Order(side=BUY, qty="5.00", limit_price="100.00", order_type=LOC)`,
**When** `simulate_fill(order, bar, slippage_bps="0")` is evaluated against
`bar = (D, 101,102,99,100, None, 1300)` (close `"100.00" <= limit "100.00"`),
**Then** a `Fill` is returned at `fill_price = "100.00"` (the bar close), `qty = "5.00"`,
**And (no fill)** against `bar = (D, 101,103,101,102, None, 1300)` (close `"102.00" > limit
"100.00"`), `simulate_fill` returns `None` and **no position changes**,
**And (SELL symmetry)** an LOC SELL at `limit_price = "100.00"` fills at close only when
`close >= "100.00"` (fills at `"100.00"` when close is `"100.00"` or higher, else `None`).

## Scenario 3 — reserved_limit fills when the bar range crosses the limit (REQ-BACKTEST-001-R2)

**Given** a VR `Order(side=BUY, qty="3.00", limit_price="95.00", order_type=RESERVED_LIMIT)`,
**When** `simulate_fill(order, bar, slippage_bps="0")` is evaluated against
`bar = (D, 100,101,90,98, None, 1300)` (range `[low 90, high 101]` includes `95.00`),
**Then** a `Fill` is returned at `fill_price = "95.00"` (the **limit price**, not the close),
`qty = "3.00"`,
**And (miss, no carry-over)** against `bar = (D, 100,101,96,98, None, 1300)` (range `[96,101]` does
**not** include `95.00`), `simulate_fill` returns `None`, the order is cancelled for that cycle, and
it is **not** rolled forward onto the next bar,
**And (SELL side)** a reserved_limit SELL at `"105.00"` fills at `"105.00"` only when
`low <= 105 <= high`.

## Scenario 4 — Unfilled order carries no position change (REQ-BACKTEST-001-R2, Unwanted)

**Given** an engine mid-run with `holdings = "10.00"`, `cash = "500.00"`,
**And** an LOC BUY whose limit is below the bar close (a miss) — `simulate_fill` returns `None`,
**When** the engine applies that `None` fill,
**Then** `holdings` stays `"10.00"` and `cash` stays `"500.00"` (a miss leaves the position
untouched),
**And** the recorded `EquityPoint` for that bar reflects only mark-to-market (no trade), and the
trade ledger gains no `Fill`/`Trade` for the missed order.

## Scenario 5 — Commission reduces cash by the modeled amount (REQ-BACKTEST-001-R3)

**Given** `CostModel(commission_per_trade="1.00", tax_rate="0.22", annual_deduction_krw="2500000",
slippage_bps="0")`,
**And** an LOC BUY of `qty = "5.00"` that fills at `"100.00"` (cash for shares `= "500.00"`),
**When** the engine applies the fill with commission,
**Then** cash is reduced by `shares + commission = 500.00 + 1.00 = "501.00"` (the commission is
modeled per trade and lowers cash by exactly `"1.00"` beyond the share cost),
**And** the `Fill.commission == "1.00"` is recorded on the ledger,
**And (default)** with the default `commission_per_trade = "0.00"`, cash is reduced by exactly the
share cost `"500.00"` and `Fill.commission == "0.00"`.

## Scenario 6 — 22% tax only above the 2.5M KRW yearly deduction; resets next year (REQ-BACKTEST-001-R3)

**Given** `CostModel(tax_rate="0.22", annual_deduction_krw="2500000")` and constant `fx_usdkrw =
"1300.00"` (so the deduction is `2,500,000 / 1300 = "1923.08"` USD),
**And** in calendar year **Y1** the strategy realizes a net gain of `"3000.00"` USD (via profit-take
SELLs),
**When** `annual_tax(year_gains_usd="3000.00", fx_at_realization="1300.00")` is evaluated for Y1,
**Then** the taxable base is `max(0, 3000.00 − 1923.08) = "1076.92"` USD and the tax is
`1076.92 × 0.22 = "236.92"` USD,
**And (below the deduction)** if Y1's net gain were `"1500.00"` USD (`< "1923.08"`), the taxable base
is `max(0, 1500.00 − 1923.08) = "0.00"` and the tax is `"0.00"`,
**And (loss year)** a net realized **loss** in a year incurs `"0.00"` tax (never negative),
**And (reset)** in the **next** calendar year **Y2** a fresh `"2500000"` KRW deduction applies — Y1's
used/unused deduction does **not** carry into Y2; a 2-year run taxes each year independently.

## Scenario 7 — Tax drag > 0 for a profitable MAB-like sequence with frequent sells (REQ-BACKTEST-001-R4)

**Given** a synthetic up-trending series and an MAB-like stub that performs **frequent profit-take
SELLs** within one calendar year, realizing aggregate net gains comfortably above the 2.5M KRW
deduction,
**When** `compute_metrics(result, basis="KRW")` is evaluated,
**Then** the **gross** (pre-tax) return exceeds the **after-tax** return, so `tax_drag =
gross_return − after_tax_return > 0`,
**And** `tax_drag` is reported as a non-negative quantity sourced from the `Decimal` ledger (the
gross-vs-after-tax gap), illustrating that MAB's frequent realizations create a material tax drag,
**And (contrast)** a buy-and-hold sequence that realizes nothing until the end shows a smaller
per-year tax drag for the same total gain (fewer taxable years).

## Scenario 8 — 3x index buy & hold baseline from the index series (REQ-BACKTEST-001-R5)

**Given** an index series `[ (D1, 100.00), (D2, 110.00) ]` (a +10% index move) and starting capital
`"1000.00"` USD,
**When** `index_buy_hold(index, k=3, capital="1000.00")` is evaluated,
**Then** the synthetic 3x daily-rebalanced curve compounds the index's daily return by `k = 3`:
the day-1→day-2 index return `+10%` becomes `+30%`, so the curve ends at
`1000 × (1 + 3 × 0.10) = "1300.00"`,
**And (1x / 2x)** `k = 1` ends at `"1100.00"` and `k = 2` ends at `"1200.00"` (same series),
**And (no index)** when no index series is provided, `baselines` **omits** the 1x/2x/3x curves and
still computes the **naive ETF buy & hold** (buy the full capital at the first close, hold to the
last, marked to market, run through the same cost model on the single terminal realized gain).

## Scenario 9 — Edge: no trades → flat curve, zero tax (REQ-BACKTEST-001-R1 / R3 / R4)

**Given** a series where the strategy is never triggered into a fill (e.g. a stub returning `[]`
every bar) and `start_state` holds only cash `pool = "1000.00"`,
**When** `run_backtest(...)` then `compute_metrics(...)` are evaluated,
**Then** the equity curve is **flat** at `"1000.00"` USD every bar (`holdings = "0.00"`, `cash =
"1000.00"`), and `equity_krw = 1000 × fx_usdkrw` each bar,
**And** realized PnL is `"0.00"`, total tax is `"0.00"` (no realized gains), and `tax_drag` is
`"0.00"`,
**And** `MDD == 0`, `CAGR == 0` (no growth), and `final_assets` equals the start capital in both USD
and KRW.

## Scenario 10 — No look-ahead: bar `t` fill ignores bar `t+1` (REQ-BACKTEST-001-R2, invariant)

**Given** any series and a strategy producing an order on bar `t`,
**When** the fill for bar `t` is computed and then **any field of bar `t+1`** (its open/high/low/
close/fx) is mutated and the run is repeated up to bar `t`,
**Then** the `Fill` (or miss) and the `EquityPoint` at bar `t` are **identical** in both runs —
proving `simulate_fill` and the per-bar bookkeeping use only data up to and including bar `t`
(close-based, no peeking forward).

## Scenario 11 — Cadence: VR triggers per cycle, MAB triggers daily (REQ-BACKTEST-001-R1)

**Given** a multi-month daily series, a `VRStrategy` (`cadence = "cycle"`) with
`cycle_length = "monthly"`, and a `MABStrategy` (`cadence = "daily"`),
**When** each is run through `run_backtest(...)`,
**Then** the engine calls `VRStrategy.plan_orders` **once per cycle** (one trigger per month [T2]),
recording mark-to-market `EquityPoint`s on the non-trigger days in between,
**And** the engine calls `MABStrategy.plan_orders` on **every trading bar**,
**And** the trigger choice is driven by `strategy.cadence` (not by name), and the engine builds the
correct `State` keys for each (`V_n`/`pool`/`qty` for VR; `avg_price`/`holdings`/`seed_remaining`/
`round_idx` for MAB), all as `Decimal`.

---

## Quality-Gate Criteria

A change implementing SPEC-BACKTEST-001 is accepted only when all of the following hold:

- **Tested**: `uv run pytest --cov=src/ballast --cov-report=term-missing` passes with coverage
  **≥ 85%**; each scenario above has a corresponding test, and the `hypothesis` invariants hold:
  - **no look-ahead** — mutating bar `t+1` never changes the fill/`EquityPoint` at bar `t`,
  - **cash conservation** — `final_cash + holdings × last_close == start_cap + Σ realized − Σ commission − Σ tax` (USD) within the 2-place quantum,
  - **determinism** — identical inputs yield an identical `BacktestResult`,
  - **no-float money** — every money field on `Fill`/`EquityPoint`/`Trade`/tax output is a 2-place
    `Decimal` and re-quantizing is idempotent,
  - **tax non-negativity** — `annual_tax(...) >= 0`; a loss year → `"0.00"`.
- **Readable**: `uv run ruff check .` reports **0 errors**; naming follows the constitution
  (`snake_case` functions, `PascalCase` types, `*_usd`/`*_krw`/`*_qty` unit suffixes).
- **Unified**: `uv run ruff format --check .` is clean.
- **Secured / Type-safe**: `uv run mypy --strict src` reports **0 errors**; no `float` on the money
  path (`costs.py`/`fills.py`/`types.py` Decimal-only; float confined to `metrics.py` ratios); the
  engine reaches strategies only via `plan_orders` (no import of `vr.py`/`mab.py` internals); `State`
  is never mutated and `State.data` carries `Decimal` only.
- **Trackable**: `@SPEC:SPEC-BACKTEST-001` is linked to `@TEST` / `@CODE` / `@DOC` tags; commits use
  conventional-commit format referencing SPEC-BACKTEST-001.

Note: `pandas` / `numpy` **are allowed** in `src/ballast/backtest/` (forbidden only in
`src/ballast/core/`).

## Definition of Done

- All eleven scenarios pass as automated tests under
  `tests/unit/backtest/test_{engine,fills,costs,metrics,baselines}.py`.
- `src/ballast/backtest/{types,fills,costs,metrics,baselines,engine}.py` implement the contracts in
  `spec.md`, reusing CORE-001 types without redefining them and driving VR/MAB only through
  `plan_orders`.
- LSP run-phase gates clean: errors = 0, type errors = 0, lint errors = 0.
- Coverage ≥ 85%; the 3-bar worked example resolves to the exact USD curve
  `[1000.00, 1100.00, 1200.00]` and KRW curve `[1300000.00, 1430000.00, 1560000.00]`; the Y1 tax
  example resolves to `"236.92"` USD.
- The TBD items remain parameters, not hardcoded values: T2 (`cycle_length`, default monthly),
  T7-bt (`commission_per_trade` / `slippage_bps`), T8-bt (per-bar FX basis), T9-bt (average-cost
  lot basis). Sweep / walk-forward / regime split stay in SPEC-BACKTEST-002.

## Traceability

- `@SPEC:SPEC-BACKTEST-001` → `@TEST:SPEC-BACKTEST-001`
  (this file → `tests/unit/backtest/test_{engine,fills,costs,metrics,baselines}.py`) →
  `@CODE:SPEC-BACKTEST-001` (`src/ballast/backtest/{engine,fills,costs,metrics,baselines,types}.py`)
  → `@DOC:SPEC-BACKTEST-001`.
- Depends on `@SPEC:SPEC-CORE-001`, `@SPEC:SPEC-VR-001`, `@SPEC:SPEC-MAB-001`
  (`src/ballast/core/{models,instrument,config,strategy,vr,mab}.py`).
