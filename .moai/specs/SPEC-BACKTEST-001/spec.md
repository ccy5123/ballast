---
id: SPEC-BACKTEST-001
version: 0.1.0
status: draft
created: 2026-06-26
updated: 2026-06-26
author: ccy5123
priority: high
lifecycle_level: spec-first
---

## HISTORY

### v0.1.0 (2026-06-26)

- Initial draft. Defines the **shared backtest engine** for ballast: a deterministic replay
  loop that drives EITHER strategy (`VRStrategy` on a `cycle` cadence, `MABStrategy` daily)
  through the SAME pure CORE-001 `Strategy.plan_orders` contract that live trading will use, a
  close-based **fill model**, an after-tax **cost model** (commission, capital-gains tax 22% with
  a 2.5M KRW annual deduction, FX), performance **metrics**, and per-leverage **baselines**.
  Module home: `src/ballast/backtest/`.
- Five EARS requirement modules (`engine`, `fills`, `costs`, `metrics`, `baselines`) — one per
  planned module. Adds backtest-only types (`OHLCBar`, `Fill`, `Trade`, `EquityPoint`,
  `BacktestResult`) under `src/ballast/backtest/types.py`. REUSES the CORE-001 `Order` / `Side` /
  `OrderType` / `State` / `Config` and the `Strategy` protocol; it redefines none of them.
- The data input is **abstract / injected**: the engine consumes an in-memory OHLC series (a typed
  `OHLCBar` sequence or a pandas `DataFrame` with a fixed schema) plus an FX series. A concrete
  data fetcher (yfinance / CSV) is OUT OF SCOPE — a later thin adapter. Tests use small synthetic
  series and are fully deterministic.
- Per the constitution, `pandas` / `numpy` are allowed in THIS backtest layer (they are forbidden
  only in `src/ballast/core/`). Realized cash, fees, and tax MUST remain `Decimal`; curve/ratio
  math (CAGR/MDD/Sharpe) may use float/numpy but is kept clearly separated.
- Scope is intentionally narrow: parameter sweep, walk-forward, and regime split are **DEFERRED to
  SPEC-BACKTEST-002** (see Out-of-Scope). This SPEC is the P0 deliverable that produces the
  cross-leverage comparison tables.

---

# SPEC-BACKTEST-001 — Shared Backtest Engine + Cost Model + Metrics + Baselines

`@SPEC:SPEC-BACKTEST-001`

## Environment

- Language: Python `>=3.11` (single language).
- Module home: `src/ballast/backtest/` (`engine.py`, `fills.py`, `costs.py`, `metrics.py`,
  `baselines.py`, `types.py`). This layer is the **stateful IO/orchestration boundary** — it is the
  place pandas/numpy live, NOT the pure core.
- Dependencies: the CORE-001 domain types + the `Strategy` protocol (consumed, never redefined);
  `pandas` / `numpy` (backtest-only, permitted here per the constitution `tech.md` §"Forbidden
  Libraries"); standard library `decimal` for all money math.
- Money policy: realized **cash**, **fees**, and **tax** are `Decimal`, quantized to 2 decimal
  places at money boundaries via the CORE-001 `quantize_money` (`ROUND_HALF_UP`). Curve/ratio math
  (returns, CAGR, MDD, volatility, Sharpe) MAY use `float`/`numpy` but is computed in clearly
  separated helpers and never feeds back onto the `Decimal` cash ledger.
- Determinism: given the same OHLC series, FX series, `Config`, and starting `State`, the engine
  produces byte-identical `BacktestResult`s. No wall clock, no network, no randomness.
- Tests: `pytest`, `pytest-cov`, `hypothesis` (property-based for financial invariants such as
  no-look-ahead and cash conservation).
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: The engine replays **historical daily OHLC** through the same pure CORE-001 functions live
  trading uses. Backtest decisions and live decisions are 1:1 reproducible for the same `Config`
  (this is the project's core value proposition — single code path).
- A2: **Data input is abstract / injected.** The engine consumes an in-memory OHLC series (a typed
  `OHLCBar` sequence OR a pandas `DataFrame` with the fixed schema in §"Fixed Definitions") and an
  FX series. A concrete fetcher (yfinance / CSV) is out of scope; a later thin adapter materializes
  this contract.
- A3: This SPEC depends on **SPEC-CORE-001** (domain types + `Strategy` protocol), **SPEC-VR-001**
  (`VRStrategy`), and **SPEC-MAB-001** (`MABStrategy`). It drives strategies only through
  `strategy.plan_orders(market, state, cfg)` and the strategy's `cadence` / `ns` attributes.
- A4: The engine is the **stateful orchestrator**: it owns `holdings`, `avg_price`, `pool` (VR) /
  `seed_remaining` (MAB), `round_idx`, realized PnL, and the equity curve. It rebuilds a fresh
  immutable CORE-001 `State` from its own bookkeeping before each `plan_orders` call (the core
  stays pure; the engine never mutates a `State` in place).
- A5: **No look-ahead bias.** A bar's fills and decisions at bar `t` use only data up to and
  including bar `t`'s defined fields (close-based). A bar never peeks at `t+1`.
- A6: **Trigger cadence is read from the strategy.** `MABStrategy.cadence == "daily"` triggers every
  trading bar; `VRStrategy.cadence == "cycle"` triggers once per VR cycle (default monthly [T2]).
  The cycle length is a parameter, not a literal.
- A7: Cost assumptions (commission rate, slippage bps, tax rate, annual deduction, FX basis) are
  **explicit, documented parameters with provisional defaults** (§"Fixed Definitions" + §TBD), so a
  later sweep can vary them.
- A8: The `State.data` container is `dict[str, Decimal]` (CORE-001); the engine therefore stores all
  per-strategy state it injects (`V_n`, `pool`, `qty`, `avg_price`, `holdings`, `seed_remaining`,
  `round_idx`) as `Decimal` values, exactly matching the keys VR/MAB read.

## Fixed Definitions (normative)

These definitions are **fixed** and MUST be reflected in code (docstrings) and tests.

- **OHLC input schema** — the abstract data contract (A2). Either a sequence of `OHLCBar` or a
  pandas `DataFrame` with these columns, one row per trading day, ordered ascending by `date`:

  | Column      | Type            | Meaning                                                          |
  | ----------- | --------------- | ---------------------------------------------------------------- |
  | `date`      | date            | Trading day (unique, ascending).                                 |
  | `open`      | Decimal         | Bar open (USD).                                                  |
  | `high`      | Decimal         | Bar high (USD) — used by the reserved-limit range test.          |
  | `low`       | Decimal         | Bar low (USD) — used by the reserved-limit range test.           |
  | `close`     | Decimal         | Bar close (USD) — the close-based fill price and mark-to-market. |
  | `adj_close` | Decimal \| None | Optional total-return / adjusted close (for baselines).          |
  | `fx_usdkrw` | Decimal         | USD→KRW rate for that bar (the FX series, aligned by `date`).    |

  A typed `OHLCBar` carries the same fields. The optional `index` series for synthetic
  1x/2x/3x baselines (§REQ-BACKTEST-001-R5) is a parallel series with `date` + `close` (the
  underlying index level), aligned by `date`.

- **`E` for VR** = `qty × that bar's close` — the attack-asset valuation at the trigger-time close,
  exactly matching SPEC-VR-001 §"Fixed Definitions". The engine computes `E` by setting
  `Market.current_price = bar.close` so `VRStrategy.plan_orders` derives `E = qty × close` itself.

- **MAB state evolution** — `avg_price`, `seed_remaining`, `round_idx`, and `holdings` evolve from
  fills: a BUY fill updates volume-weighted `avg_price` and increments `round_idx`, decrements
  `seed_remaining` by the fill's cash; a SELL fill (profit-take or quarter-sell) reduces `holdings`
  and realizes PnL. The seed resets per SPEC-MAB-001 cycle semantics (`round_idx > n_splits` →
  quarter-sell path).

- **Close-based fill price (no look-ahead)** — fills for bar `t` use only bar `t`'s `open/high/low/
close`; the fill price is the bar **close** for `LOC` / MOC-like orders, and the **limit price**
  for a `reserved_limit` whose limit lies within `[low, high]` (§REQ-BACKTEST-001-R2).

- **Money vs curve basis** — realized cash, fees, and tax are `Decimal`, quantized to 2 places.
  Equity-curve points are recorded in BOTH **USD** and **KRW** (USD × that bar's `fx_usdkrw`);
  ratio metrics (CAGR/MDD/Sharpe) are computed on the curve and their **basis (KRW vs USD) is
  stated** in the result.

- **Cost defaults (provisional, parameterized)** — see §TBD; defaults:
  commission `Decimal("0.00")` per trade (Toss US-stock commission-free default, parameterized),
  slippage `Decimal("0")` bps, capital-gains tax rate `Decimal("0.22")`, annual KRW deduction
  `Decimal("2500000")`, FX basis = per-bar `fx_usdkrw` (no smoothing).

## Requirements

The backtest layer MUST satisfy the following EARS requirements. All are tagged to
`@SPEC:SPEC-BACKTEST-001`. There are exactly five requirement modules (one per planned module);
together they cover all five EARS types.

### REQ-BACKTEST-001-R1 — Engine Replay (Ubiquitous + Event-driven) — `engine.py`

`@SPEC:SPEC-BACKTEST-001` `REQ-BACKTEST-001-R1`

The system **shall always** iterate the OHLC bars in ascending `date` order and, for each bar,
build a CORE-001 `Market` snapshot from the bar (`current_price = bar.close`, `fx_rate =
bar.fx_usdkrw`, `is_open = True`, `is_holiday = False`) and an immutable CORE-001 `State` from its
own bookkeeping, exposing exactly the keys the chosen strategy reads:

- for `VRStrategy` (ns `vr`): `V_n`, `pool`, `qty`;
- for `MABStrategy` (ns `mab`): `avg_price`, `holdings`, `seed_remaining`, `round_idx`
  (all `Decimal`).

**When** the strategy's trigger fires for the bar — **every trading bar** for `cadence == "daily"`
(MAB), or **once per VR cycle** (default monthly [T2]) for `cadence == "cycle"` (VR) — the system
**shall** call `strategy.plan_orders(market, state, cfg)`, pass the resulting `Order`s to the fill
model (REQ-BACKTEST-001-R2), apply the returned `Fill`s to update `holdings` / `avg_price` /
`pool`(VR) or `seed_remaining`(VR-N/A; MAB) / `round_idx` / realized PnL, and record one
`EquityPoint` (mark-to-market in BOTH USD and KRW). On a non-trigger bar the system **shall** record
an `EquityPoint` (mark-to-market only) and place no orders.

The replay **shall** be deterministic: identical inputs (OHLC, FX, `Config`, starting `State`)
always yield an identical `BacktestResult`. The engine **shall not** mutate `Config` or any `State`
it passes into `plan_orders` (it constructs a fresh immutable `State` per call).

### REQ-BACKTEST-001-R2 — Fill Model (Event-driven + Unwanted) — `fills.py`

`@SPEC:SPEC-BACKTEST-001` `REQ-BACKTEST-001-R2`

The function `simulate_fill(order, bar, *, slippage_bps) -> Fill | None` applies **close-based**
fill semantics using ONLY that bar's data (no look-ahead):

- **When** `order.order_type == OrderType.LOC` and the limit condition is satisfied — a BUY fills
  if `bar.close <= limit_price`, a SELL fills if `bar.close >= limit_price` — the system **shall**
  return a `Fill` at the bar **close** (adjusted by slippage). The no-limit MAB quarter-sell
  (`limit_price is None`, an MOC-like order) **shall** fill at the bar close.
- **When** `order.order_type == OrderType.RESERVED_LIMIT` (VR) and the bar's range crosses the
  limit — i.e. `bar.low <= limit_price <= bar.high` — the system **shall** return a `Fill` at the
  **limit price** (adjusted by slippage); otherwise the order is a miss and **shall** carry no
  carry-over (it is cancelled for that cycle, never rolled into the next bar).
- The system **shall** model an **unfilled** order (an LOC whose close fails the limit, or a
  reserved-limit whose range does not cross the limit) by returning `None` — and **shall not**
  change any position for a `None` fill (Unwanted: a miss leaves holdings/cash untouched).
- **Slippage** is a config parameter in basis points: a BUY fills at `price × (1 + slippage_bps/
10000)`, a SELL at `price × (1 - slippage_bps/10000)`; the default is `0` bps.
- The system **shall not** use any field of bar `t+1` when filling an order on bar `t`
  (no look-ahead).

### REQ-BACKTEST-001-R3 — Cost Model: Commission / Tax / FX (State-driven + Unwanted) — `costs.py`

`@SPEC:SPEC-BACKTEST-001` `REQ-BACKTEST-001-R3`

All functions in this module are **pure and `Decimal`-based** (no float on the money path). They
track realized gains per lot and per tax year.

- The system **shall** apply a per-trade Toss US-stock **commission** to each fill, reducing cash by
  the modeled amount (`commission_per_trade` parameter; default `Decimal("0.00")`).
- **While** computing the year's tax, the system **shall** apply a capital-gains tax of **22%**
  (`Decimal("0.22")`) on **net realized USD gains per tax year**, computed **after** a
  **2,500,000 KRW** annual deduction (`Decimal("2500000")`) converted to USD via the FX series. Net
  realized loss in a year incurs no tax (tax is `max(0, …)`), and the deduction **shall reset per
  calendar year** (each year starts a fresh deduction; unused deduction does not carry over).
- The system **shall** convert USD ↔ KRW using the per-bar `fx_usdkrw` from the FX series (the FX
  basis is per-bar, no smoothing, [T7-bt]); realized-gain accounting tracks the USD gain and the
  KRW-converted deduction at the realization bar's FX.
- Realized-gain tracking **shall** be per lot: a SELL fill realizes
  `(sell_price − avg_cost_basis) × qty − commission` in USD; MAB's frequent profit-takes accumulate
  many small realized gains, so the per-year aggregation models the resulting **tax drag**
  explicitly.
- The system **shall not** use `float` for any money/commission/tax value, and **shall not** tax a
  year whose net realized gain (after deduction) is `<= 0`.

### REQ-BACKTEST-001-R4 — Metrics (Ubiquitous) — `metrics.py`

`@SPEC:SPEC-BACKTEST-001` `REQ-BACKTEST-001-R4`

The system **shall always** compute, from the equity curve and the trade/cash ledger, a metrics
bundle stating its KRW-vs-USD basis:

- **CAGR** — compound annual growth rate from first to last `EquityPoint`.
- **MDD** — maximum drawdown (largest peak-to-trough decline of the equity curve).
- **volatility** — annualized standard deviation of periodic returns.
- **Sharpe** — annualized mean periodic return over volatility (risk-free rate a parameter,
  default `0`).
- **rebalance / profit-take counts** — number of VR rebalances / MAB profit-take SELLs (and
  quarter-sells) executed.
- **turnover** — total traded notional over average equity.
- **tax drag** — the gap between the **gross** (pre-tax) return and the **after-tax** return, a
  non-negative quantity for a profitable sequence with realized gains.
- **final assets** — the last `EquityPoint`, reported in BOTH USD and KRW.

Curve/ratio metrics (CAGR/MDD/volatility/Sharpe) MAY be computed with `float`/`numpy`; the realized
cash quantities they reference (final assets, tax drag in money terms) **shall** originate from the
`Decimal` ledger. The bundle **shall** state whether ratios were computed on the **KRW** or **USD**
curve.

### REQ-BACKTEST-001-R5 — Baselines (Optional + Ubiquitous) — `baselines.py`

`@SPEC:SPEC-BACKTEST-001` `REQ-BACKTEST-001-R5`

The system **shall** produce leverage baselines so strategy results are comparable on equal footing,
each run through the same cost model where applicable:

- **Underlying index 1x / 2x / 3x buy & hold** — synthetic curves derived from the **index series**:
  a daily-rebalanced `k×` exposure (`k ∈ {1, 2, 3}`) compounding the index's daily return by `k`
  from a common starting capital. (This models the leveraged-ETF daily-reset behavior synthetically
  from the index, not the ETF price.)
- **Naive buy & hold of the target ETF** — buy the full starting capital of the target ETF at the
  first bar's close and hold to the last bar, marked to market, run through the same commission/tax
  model on the single terminal realized gain.
- **Where** an index series is provided, the system **shall** compute the 1x/2x/3x baselines;
  **where** it is absent, the system **shall** still compute the naive ETF buy & hold and **shall**
  omit the index baselines rather than fabricating them.

These baselines enable the reporting use-cases (VR basic-vs-skill, MAB version comparison, and
VR-vs-MAB-vs-parallel comparisons); the **exhaustive sweep** that varies parameters is
SPEC-BACKTEST-002, not this SPEC.

## Specifications

| Capability                         | Module                              | Contract (key function / type signatures)                                                                                                                                                                                                                                 |
| ---------------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backtest-only types                | `src/ballast/backtest/types.py`     | `OHLCBar`, `Fill`, `Trade`, `EquityPoint`, `BacktestResult` (immutable; money fields `Decimal`)                                                                                                                                                                           |
| Engine replay (deterministic loop) | `src/ballast/backtest/engine.py`    | `run_backtest(strategy: Strategy, bars: Sequence[OHLCBar] \| DataFrame, cfg: Config, *, start_state: State, cycle_length="monthly", index=None, costs=CostModel()) -> BacktestResult`                                                                                     |
| Fill model (close-based, no peek)  | `src/ballast/backtest/fills.py`     | `simulate_fill(order: Order, bar: OHLCBar, *, slippage_bps: Decimal = Decimal("0")) -> Fill \| None`                                                                                                                                                                      |
| Cost model (commission/tax/FX)     | `src/ballast/backtest/costs.py`     | `CostModel(commission_per_trade=Decimal("0.00"), tax_rate=Decimal("0.22"), annual_deduction_krw=Decimal("2500000"), slippage_bps=Decimal("0"))`; `apply_commission(...)`, `realized_gain_usd(fill, avg_cost)`, `annual_tax(year_gains_usd, fx_at_realization) -> Decimal` |
| Metrics (curve + ledger)           | `src/ballast/backtest/metrics.py`   | `compute_metrics(result: BacktestResult, *, basis: Literal["KRW","USD"]="KRW", risk_free=0.0) -> Metrics` (CAGR/MDD/vol/Sharpe/counts/turnover/tax_drag/final_assets)                                                                                                     |
| Baselines (1x/2x/3x + naive B&H)   | `src/ballast/backtest/baselines.py` | `index_buy_hold(index, k: int, capital: Decimal) -> EquityCurve`; `naive_buy_hold(bars, capital, costs) -> BacktestResult`                                                                                                                                                |

### Backtest-only types (new in this SPEC; REUSE CORE-001 elsewhere)

- **`OHLCBar`** — one trading day: `date`, `open`, `high`, `low`, `close` (`Decimal` USD),
  `adj_close: Decimal | None`, `fx_usdkrw: Decimal`. Immutable.
- **`Fill`** — a realized execution: `order: Order` (CORE-001), `fill_price: Decimal`,
  `qty: Decimal`, `date`, `commission: Decimal`. Immutable.
- **`Trade`** — a closed round-trip (or a realizing SELL) for the ledger: `side`, `qty`,
  `entry_price`/`exit_price` or `realized_gain_usd: Decimal`, `tax_year: int`. Immutable.
- **`EquityPoint`** — one curve sample: `date`, `equity_usd: Decimal`, `equity_krw: Decimal`,
  `holdings: Decimal`, `cash: Decimal`. Immutable.
- **`BacktestResult`** — the run output: ordered `list[EquityPoint]`, `list[Fill]`,
  `list[Trade]`, realized PnL (`Decimal`), total tax (`Decimal`), and the inputs' fingerprint for
  reproducibility. Immutable.

### Reused CORE-001 types (do NOT redefine)

- `Order`, `Side`, `OrderType`, `Market`, `State`, `Config` — `src/ballast/core/{models,config}.py`.
- `quantize_money` — `models.py` (single source of 2-place `ROUND_HALF_UP` rounding for cash/fees/
  tax).
- `Strategy` protocol — `strategy.py` (the engine drives `VRStrategy` / `MABStrategy` through it).

## Dependency on SPEC-CORE-001 / SPEC-VR-001 / SPEC-MAB-001

This SPEC has a **hard dependency** on SPEC-CORE-001 (domain types + `Strategy` protocol) and drives
the strategies specified in SPEC-VR-001 (`VRStrategy`, `cadence="cycle"`) and SPEC-MAB-001
(`MABStrategy`, `cadence="daily"`). The engine never imports VR/MAB internals — it only constructs a
`Market`/`State` and calls `plan_orders`, reads `cadence`/`ns`, and consumes the returned `Order`s.
CORE-001 (and the two strategy cores) must be implemented before SPEC-BACKTEST-001 runs against
real strategies (a trivial conforming stub strategy suffices for engine-only tests).

## TBD Items (carried as parameters, not blockers)

| TBD     | Item                           | Resolution carried as                                                                                                           |
| ------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| T2      | VR cycle length                | `cycle_length` param; **default monthly** (the scheduler in a later SPEC consumes the same value; here it slices the bars).     |
| T5 / T6 | `E` / pool timing              | Already **fixed** in CORE-001 / VR (`E = qty × close`; deposit enters only via `±flow`). The engine honors these, adds nothing. |
| T7-bt   | Commission rate / slippage bps | `commission_per_trade` (default `Decimal("0.00")`) and `slippage_bps` (default `Decimal("0")`) params on `CostModel`.           |
| T8-bt   | FX basis / smoothing           | Per-bar `fx_usdkrw` with no smoothing by default; a smoothing option deferred to BACKTEST-002.                                  |
| T9-bt   | Tax-lot accounting method      | Average-cost lot basis by default (matches MAB `avg_price`); FIFO/specific-lot variants deferred to BACKTEST-002.               |

## Out of Scope (SPEC-BACKTEST-002 and later adapters)

- **SPEC-BACKTEST-002**: parameter **sweep** + **walk-forward** + **regime split** (up / down /
  sideways) + **robust-plateau** evaluation. None of these appear in this SPEC.
- The **concrete data fetcher** (yfinance / CSV loader) — a later thin adapter that materializes the
  abstract OHLC + FX input contract defined here.
- Broker / market-data **live adapters** (Toss / KIS), the order manager, reconciliation, and the
  scheduler — those are P1+ SPECs and the live execution path.

The engine's data input is abstract precisely so the fetcher can be added later without touching the
replay/cost/metric code.

## Traceability

- `@SPEC:SPEC-BACKTEST-001` — this document.
- `@TEST:SPEC-BACKTEST-001` — see `acceptance.md` Given/When/Then scenarios +
  `tests/unit/backtest/test_{engine,fills,costs,metrics,baselines}.py`.
- `@CODE:SPEC-BACKTEST-001` — `src/ballast/backtest/{engine,fills,costs,metrics,baselines,types}.py`.
- `@DOC:SPEC-BACKTEST-001` — generated during `/moai:3-sync`.
- Depends on `@SPEC:SPEC-CORE-001`, `@SPEC:SPEC-VR-001`, `@SPEC:SPEC-MAB-001`.

### Requirement Index

| Requirement ID      | EARS Type                 | Summary                                                                                                  |
| ------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------- |
| REQ-BACKTEST-001-R1 | Ubiquitous + Event-driven | `engine.py` replay: ordered bars, build Market/State, trigger by `cadence`, apply fills, record curve    |
| REQ-BACKTEST-001-R2 | Event-driven + Unwanted   | `fills.py` close-based: LOC fills at close, reserved_limit fills if range crosses; miss→None; slippage   |
| REQ-BACKTEST-001-R3 | State-driven + Unwanted   | `costs.py` Decimal: commission, 22% tax after 2.5M KRW yearly deduction (resets), FX; no float, no tax≤0 |
| REQ-BACKTEST-001-R4 | Ubiquitous                | `metrics.py`: CAGR/MDD/vol/Sharpe/counts/turnover/tax-drag/final-assets; KRW-vs-USD basis stated         |
| REQ-BACKTEST-001-R5 | Optional + Ubiquitous     | `baselines.py`: index 1x/2x/3x B&H (synthetic) + naive ETF B&H through the same cost model               |
