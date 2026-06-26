# Implementation Plan — SPEC-BACKTEST-001 (Shared Backtest Engine + Cost Model + Metrics + Baselines)

`@SPEC:SPEC-BACKTEST-001`

This plan covers the shared backtest engine only: a deterministic replay loop plus a fill model, a
cost model, a metrics bundle, and per-leverage baselines, under `src/ballast/backtest/`. It builds
on the contracts fixed in SPEC-CORE-001 and drives the strategies specified in SPEC-VR-001 and
SPEC-MAB-001 purely through `Strategy.plan_orders`. Parameter sweep, walk-forward, and regime split
remain in SPEC-BACKTEST-002; the concrete data fetcher (yfinance / CSV) is a later adapter.

## Module Layout

A new package alongside the pure core. **Unlike `src/ballast/core/`, this layer MAY use
`pandas`/`numpy`** (the constitution forbids them only in core). It is the stateful IO/orchestration
boundary; the strategy math it calls stays pure.

```
src/ballast/backtest/
├── types.py        # OHLCBar, Fill, Trade, EquityPoint, BacktestResult (immutable; money = Decimal)
├── fills.py        # simulate_fill(order, bar, *, slippage_bps) -> Fill | None  (close-based, no peek)
├── costs.py        # CostModel: commission, 22% tax after 2.5M KRW deduction (resets/yr), FX  (Decimal)
├── metrics.py      # compute_metrics(...) -> Metrics  (CAGR/MDD/vol/Sharpe/counts/turnover/tax-drag)
├── baselines.py    # index 1x/2x/3x buy&hold (synthetic) + naive ETF buy&hold (same cost model)
└── engine.py       # run_backtest(strategy, bars, cfg, *, start_state, cycle_length, index, costs)
```

Dependency direction (no cycles; the backtest layer depends ONLY on the core, never the reverse):

```
engine.py ─► types.py, fills.py, costs.py, metrics.py, baselines.py
          ─► (core) models.py, strategy.py, config.py        # Market, State, Order, Strategy, Config
fills.py  ─► types.py ─► (core) models.py                    # Order, Side, OrderType, quantize_money
costs.py  ─► types.py ─► (core) models.py                    # quantize_money only (Decimal money math)
metrics.py, baselines.py ─► types.py                          # curve/ledger consumers
```

The engine drives `VRStrategy` / `MABStrategy` through the `Strategy` protocol; it imports neither
`vr.py` nor `mab.py` internals — it constructs a `Market`/`State`, calls `plan_orders`, and reads
`cadence` / `ns`.

## Data-Input Schema (abstract / injected — A2)

The engine accepts EITHER a typed `Sequence[OHLCBar]` OR a pandas `DataFrame` with this fixed
schema (one row per trading day, ascending `date`):

| Column      | Type            | Use                                                        |
| ----------- | --------------- | ---------------------------------------------------------- |
| `date`      | date            | Trading day (unique, ascending). Cycle slicing keys on it. |
| `open`      | Decimal         | Bar open (USD).                                            |
| `high`      | Decimal         | Reserved-limit range test upper bound.                     |
| `low`       | Decimal         | Reserved-limit range test lower bound.                     |
| `close`     | Decimal         | Close-based fill price + mark-to-market + VR `E`.          |
| `adj_close` | Decimal \| None | Optional total-return / adjusted close for baselines.      |
| `fx_usdkrw` | Decimal         | USD→KRW for that bar (FX series, aligned by `date`).       |

An optional **index series** (parallel `date` + `close` index level) drives the synthetic
1x/2x/3x baselines. A `DataFrame` input is normalized to `OHLCBar` at the boundary so the inner loop
sees one type. A concrete fetcher that produces this contract is OUT OF SCOPE (later adapter).

## Function / Type Signatures (target)

```python
from __future__ import annotations
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

import pandas as pd  # backtest-layer only; forbidden in core

from ballast.core.config import Config
from ballast.core.models import Order, Side, State
from ballast.core.strategy import Strategy


@dataclass(frozen=True, slots=True)
class OHLCBar:
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal | None
    fx_usdkrw: Decimal


@dataclass(frozen=True, slots=True)
class Fill:
    order: Order
    fill_price: Decimal
    qty: Decimal
    date: date
    commission: Decimal


@dataclass(frozen=True, slots=True)
class EquityPoint:
    date: date
    equity_usd: Decimal
    equity_krw: Decimal
    holdings: Decimal
    cash: Decimal


@dataclass(frozen=True, slots=True)
class CostModel:
    commission_per_trade: Decimal = Decimal("0.00")   # Toss US-stock commission [T7-bt]
    tax_rate: Decimal = Decimal("0.22")               # capital-gains 22%
    annual_deduction_krw: Decimal = Decimal("2500000")  # 2.5M KRW / calendar year, resets
    slippage_bps: Decimal = Decimal("0")              # [T7-bt]


def simulate_fill(
    order: Order, bar: OHLCBar, *, slippage_bps: Decimal = Decimal("0")
) -> Fill | None: ...

def run_backtest(
    strategy: Strategy,
    bars: Sequence[OHLCBar] | pd.DataFrame,
    cfg: Config,
    *,
    start_state: State,
    cycle_length: Literal["monthly"] | int = "monthly",   # [T2]
    index: Sequence[tuple[date, Decimal]] | None = None,
    costs: CostModel = CostModel(),
) -> "BacktestResult": ...

def compute_metrics(
    result: "BacktestResult", *, basis: Literal["KRW", "USD"] = "KRW", risk_free: float = 0.0
) -> "Metrics": ...
```

Notes on the signatures:

- `run_backtest` takes the strategy as a `Strategy`-typed argument and reads its `cadence` to choose
  the daily-vs-cycle trigger; it never special-cases VR/MAB by name.
- `start_state` is the initial CORE-001 `State` (e.g. VR `{V_n, pool, qty}` or MAB
  `{avg_price, holdings, seed_remaining, round_idx}`, all `Decimal`). The engine clones a fresh
  immutable `State` from its own bookkeeping each trigger — it never mutates the passed-in `State`.
- `cycle_length` defaults to `"monthly"` [T2]; an `int` means "every N trading bars" (the same value
  a later scheduler will consume). Here it only slices which bars trigger VR.
- `index` is optional: when absent, `baselines.py` omits the 1x/2x/3x curves and still computes the
  naive ETF buy & hold (REQ-BACKTEST-001-R5).

## Decimal-vs-numpy Boundary Policy

This is the single most important separation in this layer.

- **`Decimal` (money path — required):** realized cash, `pool` / `seed_remaining`, `holdings`,
  `avg_price`, fill prices, **commission**, **realized gains**, **tax**, and the `EquityPoint`
  `equity_usd` / `equity_krw` / `cash`. All quantized to 2 places via the CORE-001 `quantize_money`
  at money boundaries. `costs.py` is entirely `Decimal`; `fills.py` produces `Decimal` fill prices;
  `types.py` money fields are `Decimal`.
- **`float` / `numpy` (curve & ratio math — permitted, isolated):** CAGR, MDD, volatility, Sharpe,
  and turnover ratios in `metrics.py` MAY convert the `Decimal` curve to a `numpy` array of `float`
  for the ratio computation. The converted floats are **read-only**: they never flow back onto the
  `Decimal` ledger, and money-denominated outputs (final assets, tax-drag in cash terms) come from
  the `Decimal` ledger, not the float curve.
- **One-way conversion:** `Decimal → float` happens only inside `metrics.py` (and only for
  ratios). No function takes a `float` and writes it back as money. `mypy --strict` plus a test that
  asserts `costs.py` / `fills.py` / `types.py` contain no `float` on money fields enforces this.

## Engine State Bookkeeping (how fills mutate the engine, not the core)

The engine owns mutable bookkeeping; the core stays pure. Per trigger:

1. Build `Market(current_price=bar.close, fx_rate=bar.fx_usdkrw, is_open=True, is_holiday=False)`.
2. Build a fresh immutable `State(ns=strategy.ns, data={...Decimal...})` exposing exactly the keys
   the strategy reads (`V_n`/`pool`/`qty` for VR; `avg_price`/`holdings`/`seed_remaining`/`round_idx`
   for MAB).
3. `orders = strategy.plan_orders(market, state, cfg)`.
4. For each order: `fill = simulate_fill(order, bar, slippage_bps=costs.slippage_bps)`; if `None`
   (a miss), change nothing (REQ-BACKTEST-001-R2 Unwanted).
5. Apply each `Fill`: a BUY updates volume-weighted `avg_price`, increments `holdings`, decrements
   cash (`pool`/`seed_remaining`) by `fill cash + commission`, increments MAB `round_idx`; a SELL
   reduces `holdings`, increases cash, and records a realizing `Trade` whose `realized_gain_usd =
(fill_price − avg_cost) × qty − commission` tagged with the bar's `tax_year`.
6. Record one `EquityPoint` (USD mark-to-market = `cash + holdings × bar.close`; KRW = USD ×
   `bar.fx_usdkrw`).

At year boundaries (and at the run end) `costs.annual_tax(year_gains_usd, fx_at_realization)` is
applied per calendar year, after the 2.5M KRW deduction (converted at FX), and the deduction resets.

## Dependency on the Cores

`engine.py` and friends import and REUSE (never redefine):

- `Order`, `Side`, `OrderType`, `Market`, `State`, `Config` (`core/models.py`, `core/config.py`).
- `quantize_money` (`core/models.py`) — the single 2-place rounding helper for cash/fees/tax.
- `Strategy` protocol (`core/strategy.py`) — `run_backtest` is generic over any conforming strategy;
  `VRStrategy` (`cadence="cycle"`) and `MABStrategy` (`cadence="daily"`) are the two it replays.
- The VR fixed definition `E = qty × close` is honored implicitly: the engine sets
  `Market.current_price = bar.close`, so `VRStrategy.plan_orders` computes `E` itself — the engine
  adds nothing and re-derives nothing (T5/T6 already fixed upstream).

## Task Decomposition

Ordered by dependency; priority labels only (no time estimates).

### Primary Goal — `types.py` + `fills.py` (the deterministic execution primitives) [Priority High]

1. Define the immutable backtest types (`OHLCBar`, `Fill`, `Trade`, `EquityPoint`, `BacktestResult`)
   with `Decimal` money fields, quantized at construction (mirror CORE-001 `__post_init__`).
2. Implement `simulate_fill`: LOC fills at close when the limit condition holds (BUY `close<=limit`,
   SELL `close>=limit`); the no-limit MAB quarter-sell fills at close; `reserved_limit` fills at the
   limit when `low<=limit<=high`, else returns `None`; apply `slippage_bps`; never read `t+1`.

### Secondary Goal — `costs.py` (commission / tax / FX, all Decimal) [Priority High]

3. Implement `CostModel` with the provisional defaults (commission `0.00`, tax `0.22`, deduction
   `2,500,000` KRW, slippage `0` bps).
4. `apply_commission(cash, costs)`; `realized_gain_usd(fill, avg_cost)`; `annual_tax(year_gains_usd,
fx_at_realization)` = `max(0, (gross_gain_usd − deduction_krw/fx) ) × 0.22`, per calendar year,
   deduction resets each year. No float; no tax when net realized gain ≤ 0.

### Final Goal — `engine.py` replay (drives strategies via `plan_orders`) [Priority High]

5. Normalize `DataFrame`→`OHLCBar` at the boundary; iterate ascending; build `Market`/`State`;
   trigger every bar (daily) or once per cycle (monthly [T2] / every-N-bars) by `strategy.cadence`.
6. Call `plan_orders`, route orders through `simulate_fill`, apply fills to the engine bookkeeping,
   accrue per-year realized gains, record the USD+KRW equity curve; apply `annual_tax` at year
   boundaries; assemble an immutable `BacktestResult`. Keep it deterministic and `Config`-immutable.

### Final Goal — `metrics.py` + `baselines.py` (reporting surface) [Priority High]

7. `compute_metrics`: CAGR/MDD/volatility/Sharpe from the curve (float/numpy allowed), plus
   rebalance/profit-take counts, turnover, **tax drag** (gross-vs-after-tax gap), and final assets
   (USD+KRW) from the `Decimal` ledger; state the KRW-vs-USD basis.
8. `baselines.py`: synthetic index 1x/2x/3x daily-rebalanced buy & hold from the index series, and
   naive ETF buy & hold run through the same cost model; omit index baselines when no index series.

### Optional Goal — comparison-table assembly helper [Priority Low]

9. A thin helper that lines up strategy result + baselines into a comparison table (CAGR/MDD/tax-
   drag/final assets per leverage). No sweep logic — that is BACKTEST-002.

## Technical Approach

- **Single code path for backtest and live:** the engine drives strategies ONLY via
  `plan_orders(market, state, cfg)`. The same pure functions that decide live orders decide backtest
  orders — the project's central correctness guarantee.
- **Engine is the only stateful part:** the core stays pure; the engine reconstructs a fresh
  immutable `State` each trigger from its own mutable bookkeeping and never mutates a `State` or
  `Config` in place.
- **Close-based, no look-ahead:** `simulate_fill` reads only bar `t`. A `hypothesis` invariant
  asserts that permuting any field of bar `t+1` never changes the fill on bar `t`.
- **Money is Decimal, ratios are float — strictly separated:** `costs.py`/`fills.py`/`types.py` are
  Decimal-only; `metrics.py` is the single place `Decimal→float` conversion happens, and only for
  ratios.
- **FX per bar:** USD↔KRW uses each bar's `fx_usdkrw`; the equity curve carries both USD and KRW so
  metrics can be reported on either basis (default KRW, since tax/deduction are KRW-denominated).
- **Tax modeled like reality:** 22% on net realized USD gain per calendar year, after a 2.5M KRW
  deduction converted at FX, resetting each year. MAB's frequent profit-takes make the per-year
  aggregation (and thus tax drag) material — modeled explicitly, not approximated.
- **Baselines on equal footing:** the naive ETF buy & hold runs through the same cost model; index
  1x/2x/3x are synthetic daily-rebalanced curves so a 2x vs 3x comparison is apples-to-apples.

## Risk Analysis

| Risk                                                          | Impact                                                       | Mitigation                                                                                                                                         |
| ------------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Look-ahead bias** (fill peeks at `t+1`)                     | Backtest beats live; results are fiction                     | `simulate_fill` takes a single `bar`; a `hypothesis` invariant: mutating `t+1` never changes the fill on `t`; engine passes only the current bar   |
| **Tax-year boundary / deduction reset** off-by-one            | Wrong tax; deduction carried over or double-counted          | Key realized gains by `bar.date.year`; `annual_tax` per year with a fresh 2.5M KRW deduction; tests crossing a Dec→Jan boundary and a 2-year run   |
| **Deduction over/under-applied** (applied to losses or twice) | Negative tax / understated drag                              | `tax = max(0, (gross_usd − deduction_krw/fx)) × 0.22`; a loss year → 0 tax; deduction applied once per year; explicit loss-year + just-under tests |
| **Unfilled-order accounting** (a miss still moves position)   | Phantom holdings / cash drift                                | `simulate_fill` returns `None` on a miss; the engine changes nothing for `None`; a `hypothesis` cash-conservation invariant (Σ flows balance)      |
| **FX alignment** (bar vs FX series mismatched by date)        | Wrong KRW equity / wrong deduction conversion                | FX is a column on `OHLCBar` (joined by `date` at the boundary); a missing/misaligned FX row is a hard error, never silently forward-filled         |
| **`float` leaking onto the money path** (via numpy)           | Precision loss on cash/tax                                   | `Decimal`-only in `costs.py`/`fills.py`/`types.py`; `Decimal→float` confined to `metrics.py` ratios; test asserts no float money fields            |
| **VR cycle trigger vs MAB daily trigger** mis-cadenced        | VR rebalances daily or MAB triggers monthly → wrong behavior | Trigger is chosen from `strategy.cadence`; `cycle_length` slices VR triggers; tests assert VR fires once/cycle and MAB every bar                   |
| **`State` mutation / non-Decimal in `State.data`**            | Core contract broken (data must be `dict[str, Decimal]`)     | Engine builds a fresh immutable `State` with `Decimal`-only values each trigger (incl. `round_idx` as `Decimal`, read back via `int(...)`)         |
| **Reserved-limit carry-over** (a missed VR limit rolls over)  | Stale orders fill on a later bar                             | A reserved-limit miss is cancelled for that cycle (no carry-over); test a limit outside `[low,high]` → no fill, no roll-forward                    |
| **Non-determinism** (dict ordering / numpy seeding)           | Non-reproducible `BacktestResult`                            | Ordered bars + ordered ledgers; no randomness; a test runs the same inputs twice and asserts identical results                                     |

## Test Approach

- Framework: `pytest` + `pytest-cov`; property-based invariants with `hypothesis`.
- Location: `tests/unit/backtest/test_{types,fills,costs,metrics,baselines,engine}.py`.
- **Tiny synthetic series + hand-computed expected values** (deterministic unit tests):
  - A 3-bar synthetic series + a trivial conforming stub strategy → an **exact** equity curve
    (hand-computed `EquityPoint`s in USD and KRW).
  - LOC BUY fills only when `close <= limit`; LOC SELL only when `close >= limit`; the no-limit
    quarter-sell fills at close.
  - `reserved_limit` fills at the limit when `low <= limit <= high`; a limit outside the range →
    `None` (no fill, no carry-over).
  - An unfilled order changes no position (holdings/cash identical before and after).
  - Commission reduces cash by exactly the modeled amount.
  - Capital-gains tax 22% applies only on net realized gain **above** the 2.5M KRW yearly deduction;
    a Dec→Jan boundary run shows the deduction resetting the next year.
  - Tax drag `> 0` for a profitable MAB-like sequence with frequent profit-take SELLs.
  - Synthetic index 1x/2x/3x buy & hold computed from a known index series (e.g. +10% index →
    +30% on the 3x baseline before costs); naive ETF buy & hold through the cost model.
  - Edge: a series with no triggered trades → a flat curve and zero tax.
- **Property-based (`hypothesis`) invariants:**
  - **No look-ahead:** for any series, changing any field of bar `t+1` never alters the fill or the
    `EquityPoint` at bar `t`.
  - **Cash conservation:** `final_cash + holdings × last_close == start_cap + Σ realized − Σ commission − Σ tax` (USD), within the 2-place quantum.
  - **Determinism:** running the same inputs twice yields an identical `BacktestResult`.
  - **No-float money:** every money field on `Fill` / `EquityPoint` / `Trade` / tax output is a
    2-place `Decimal` and re-quantizing is idempotent.
  - **Tax non-negativity:** `annual_tax(...) >= 0` for any inputs; a loss year → `0`.
- Purity-of-boundary check: a test asserts `core/` modules are unchanged and that the engine only
  reaches the strategy through `plan_orders` (no import of `vr.py`/`mab.py` internals).

## Quality Gates (must pass before merge)

- `uv run ruff check .` → 0 errors; `uv run ruff format --check .` → clean.
- `uv run mypy --strict src` → 0 errors.
- `uv run pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85%.
- No `float` on the money path (`costs.py`/`fills.py`/`types.py` Decimal-only; float confined to
  `metrics.py` ratios); no look-ahead; the engine reaches strategies only via `plan_orders`.
- Note: `pandas` / `numpy` ARE allowed in `src/ballast/backtest/` (forbidden only in
  `src/ballast/core/`).

## Traceability

- `@SPEC:SPEC-BACKTEST-001` → `@TEST:SPEC-BACKTEST-001`
  (`tests/unit/backtest/test_{engine,fills,costs,metrics,baselines}.py`) →
  `@CODE:SPEC-BACKTEST-001` (`src/ballast/backtest/{engine,fills,costs,metrics,baselines,types}.py`)
  → `@DOC:SPEC-BACKTEST-001`.
- Depends on `@SPEC:SPEC-CORE-001`, `@SPEC:SPEC-VR-001`, `@SPEC:SPEC-MAB-001`
  (`src/ballast/core/{models,instrument,config,strategy,vr,mab}.py`).
