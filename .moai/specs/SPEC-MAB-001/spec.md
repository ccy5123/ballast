---
id: SPEC-MAB-001
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

- Initial draft. Defines the MAB ("무한매수법 / Infinite Buying") short/mid-horizon swing
  strategy core as two pure functions plus one `Strategy`-conforming class in
  `src/ballast/core/mab.py`: `mab_daily_orders`, `mab_on_seed_exhausted`, and `MABStrategy`.
- Builds directly on the contracts fixed in SPEC-CORE-001 (`Order`, `Decision`, `Market`,
  `State`, `Config`, the `Strategy` protocol, the instrument registry's `resolve_target_pct`,
  the `OrderType.LOC` / `Side` enums, and `quantize_money`). It REUSES those types; it does
  not redefine them. It mirrors the construction patterns established in `src/ballast/core/vr.py`
  (clamp → floor toward zero → zero-check → quantize at the `Order` boundary).
- The MAB version (V1.0–V2.2) and the halftime (전반전/후반전) buy/sell rules are carried as a
  documented, testable TBD [T9]. The primary mechanics (two-point LOC buys, LOC profit-take,
  quarter-sell on seed exhaustion) are fully defined; only the version-specific embellishments
  (target step-down, quarter-stop-loss) are provisional. Default `version="v2.2"`.
- Whether the Toss Open API exposes LOC is a LIVE-execution concern [T8] only; this pure core
  stamps `OrderType.LOC` regardless and is fully buildable and backtestable now. T8 is NOT a
  blocker for this SPEC.
- Scope intentionally excludes the backtest engine and cost model, broker/market-data adapters,
  the scheduler, and any live execution — those are later SPECs. VR is already specified
  (SPEC-VR-001) and out of scope here.

---

# SPEC-MAB-001 — MAB (무한매수법 / Infinite Buying) Strategy Core

`@SPEC:SPEC-MAB-001`

## Environment

- Language: Python `>=3.11` (single language).
- Module: `src/ballast/core/mab.py`. PURE — no network, no filesystem, no clock
  (`datetime.now()` is forbidden). Time and prices arrive only via the `Market` snapshot;
  `avg_price` / `holdings` / `seed_remaining` / `round_idx` arrive via the `State` argument;
  the strategy knobs arrive via the `Config` argument.
- Money/quantity: `Decimal` only (never `float`). Every monetary/quantity result is quantized to
  2 decimal places via `quantize_money` (`ROUND_HALF_UP`) at the `Order` boundary; share
  quantities are discretized **toward zero** (`ROUND_FLOOR`) before the zero-check so the
  clamp/zero invariants survive `Order.__post_init__` (mirrors `vr.py`'s `order_from_decision`).
- Order type: **`OrderType.LOC` only** on the MAB path — both BUY legs and the SELL profit-take
  and the seed-exhausted quarter-sell are LOC. No `reserved_limit`, no `market`.
- Dependencies: standard library + `decimal` only for the math; the CORE-001 domain types for the
  contracts. No `pandas`/`numpy` in the core (those are backtest-only per the constitution).
- Tests: `pytest`, `pytest-cov`, `hypothesis` (property-based for financial invariants).
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: MAB is the short/mid-horizon swing strategy. A `seed` is split into `n_splits` parts
  (default 40). Each trading day one part is bought via LOC orders, and an LOC profit-take SELL of
  the held quantity is posted. When the seed is exhausted (after `n_splits` buys) the strategy
  quarter-sells (`holdings / 4`) to re-secure the seed and the cycle repeats.
- A2: MAB's order type is **`LOC`**; cadence is **`daily`**; namespace is **`mab`**.
- A3: This SPEC depends on **SPEC-CORE-001**. It consumes `Order`, `Market`, `State`, `Config`, the
  `Strategy` protocol, the instrument registry (`resolve_target_pct`), the `OrderType.LOC` / `Side`
  enums, and the `quantize_money` helper — and redefines none of them. It mirrors `vr.py`'s order
  construction (clamp → floor toward zero → drop dust → quantize).
- A4: The day's per-round buy budget = `seed / n_splits`, **never exceeding `seed_remaining`**. Each
  day's part is split into **two LOC buys** by `split_ratio`: a near-average buy at `avg_price`, and
  a step-up buy at `avg_price * (1 + alpha)`. Two price points secure a minimum close-based fill.
- A5: The profit-take is an **LOC SELL** of the entire `holdings` at `avg_price * (1 + target_pct)`,
  placed only **while `holdings > 0`**. `target_pct` is per-instrument/leverage (TQQQ ~0.10–0.15,
  SOXL ~0.20) and is resolved through the CORE-001 chain (`resolve_target_pct`: explicit strategy
  override → instrument default → error) — **never hardcoded** [T10].
- A6: Rounds `1..n_splits/2` are 전반전 (first half); rounds `n_splits/2 + 1 .. n_splits` are
  후반전 (second half). The buy/sell rules differ by phase per `halftime_rule`; the exact rules are a
  documented TBD [T9]. This SPEC fixes a clear, testable provisional default (see REQ-MAB-001-R2).
- A7: `version` ∈ {V1.0 .. V2.2} [T9]; later versions add target-price step-down and a
  quarter-stop-loss. Provisional; default `version="v2.2"`. The provisional version embellishments
  do not alter the primary mechanics defined here — they are carried as named variants for later
  SPECs.
- A8: MAB math is deterministic for a given `(avg_price, holdings, seed, seed_remaining, round_idx,
n_splits, target_pct, alpha, split_ratio, halftime_rule, version)` — the same inputs always yield
  the same list of `Order`s.

## Fixed Definitions (normative)

These definitions are **fixed** and MUST be reflected in code (docstrings) and tests:

- **`seed`** = the total capital allocated to one MAB cycle, split into `n_splits` parts. The
  per-round budget is `seed / n_splits`.
- **`seed_remaining`** = the cash still available in the current cycle (cash before this day's buy).
  The effective per-round budget = `min(seed / n_splits, seed_remaining)`; a day's buys never commit
  more cash than `seed_remaining`.
- **`avg_price`** = the volume-weighted average purchase price of the current holdings (the cost
  basis). Both buy legs and the profit-take price are expressed relative to `avg_price`.
- **`holdings`** = the currently held share quantity (the profit-take SELL covers exactly this).
- **`round_idx`** = the 1-based index of today's buy within the cycle (`1 .. n_splits`); it selects
  the halftime phase (전반전 if `round_idx <= n_splits/2`, else 후반전).
- **`alpha`** = the fractional step-up of the second buy's price above `avg_price`
  (price B = `avg_price * (1 + alpha)`) [T10].
- **`split_ratio`** ∈ `(0, 1)` = the fraction of the day's budget allocated to the near-average buy;
  `(1 - split_ratio)` goes to the step-up buy [T10].
- **`target_pct`** = the per-instrument profit-take fraction (SELL price = `avg_price * (1 + target_pct)`),
  resolved via the CORE-001 registry chain [T10].
- All money/qty are `Decimal`, quantized to 2 places at the `Order` boundary; share quantities are
  floored toward zero before the zero-check (no dust orders).

## Requirements

The MAB core MUST satisfy the following EARS requirements. All are tagged to `@SPEC:SPEC-MAB-001`.
There are five requirement modules; together they cover all five EARS types.

### REQ-MAB-001-R1 — Daily Two-Point LOC Buys + LOC Profit-Take (Ubiquitous + Event-driven)

`@SPEC:SPEC-MAB-001` `REQ-MAB-001-R1`

The function `mab_daily_orders(avg_price, holdings, seed, seed_remaining, round_idx, n_splits,
target_pct, alpha, split_ratio, *, halftime_rule, version, ticker, account_seq)` returns a list of
CORE-001 `Order`s for one trading day.

The system **shall always** compute the day's effective budget as
`budget = min(seed / n_splits, seed_remaining)` and split it into two LOC BUY legs by `split_ratio`:

- **Near-average buy (leg A)** at `limit_price = avg_price`, budget portion `budget * split_ratio`,
  `qty_A = floor(budget_A / avg_price)`.
- **Step-up buy (leg B)** at `limit_price = avg_price * (1 + alpha)`, budget portion
  `budget * (1 - split_ratio)`, `qty_B = floor(budget_B / (avg_price * (1 + alpha)))`.

**When** `holdings > 0`, the system **shall** additionally emit one **LOC SELL** profit-take of the
entire `holdings` at `limit_price = avg_price * (1 + target_pct)` (Event-driven). All three orders
carry `order_type = OrderType.LOC`.

Share quantities **shall** be floored toward zero (`ROUND_FLOOR`) before quantization; the
`limit_price`s **shall** be quantized to 2 places at the `Order` boundary via `quantize_money`.

### REQ-MAB-001-R2 — Halftime Phase Rule (State-driven, PROVISIONAL [T9])

`@SPEC:SPEC-MAB-001` `REQ-MAB-001-R2`

The system applies the halftime phase rule selected by `halftime_rule` (PROVISIONAL [T9]; default
`"standard"`), keyed on `round_idx` versus `n_splits / 2`:

- **While** `round_idx <= n_splits / 2` (전반전 / first half), the system **shall** place **both**
  LOC BUY legs (A near-average and B step-up) as in REQ-MAB-001-R1.
- **While** `round_idx > n_splits / 2` (후반전 / second half), the system **shall** **reduce buy
  aggressiveness** by placing **only the near-average leg A** and dropping the step-up leg B
  (PROVISIONAL [T9]). The day's budget allocation is unchanged for leg A (`budget * split_ratio`).

The boundary round `round_idx == n_splits / 2` is the **last 전반전 round** (both legs); the first
후반전 round is `round_idx == n_splits / 2 + 1`. For odd `n_splits`, `n_splits / 2` uses integer
floor division so the split point is deterministic.

The profit-take SELL of REQ-MAB-001-R1 is **independent of phase** (it depends only on `holdings > 0`).
For `version` in the v2.x family, a 후반전 **profit-target step-down** is noted as a further
provisional variant [T9]; it is NOT applied by the default `"standard"` rule in this SPEC, so the
default behavior is fully defined and testable.

### REQ-MAB-001-R3 — Seed-Remaining Cap & Dust Suppression (Unwanted/Constraint)

`@SPEC:SPEC-MAB-001` `REQ-MAB-001-R3`

The system **shall not** commit more buy cash than `seed_remaining` in a single day: the effective
budget is capped at `seed_remaining` before splitting, so leg A + leg B cash never exceeds it.

- **If** `seed_remaining <= 0`, **then** the system **shall not** emit any BUY order for the day
  (the profit-take SELL may still be emitted while `holdings > 0`).
- **If** a buy leg's `qty` floors to `0` (the budget portion cannot afford one share at that leg's
  price), **then** the system **shall not** emit that leg (no zero/dust order). The other leg and the
  profit-take are unaffected.
- The system **shall not** emit a profit-take SELL **while** `holdings <= 0`.

### REQ-MAB-001-R4 — Seed Exhausted Quarter-Sell `mab_on_seed_exhausted` (Event-driven, PROVISIONAL [T9])

`@SPEC:SPEC-MAB-001` `REQ-MAB-001-R4`

The function `mab_on_seed_exhausted(holdings, *, version, ticker, account_seq)` returns a single
CORE-001 `Order`.

**When** the seed is exhausted (after `n_splits` buys), the system **shall** return a **LOC SELL** of
`holdings / 4` (a quarter-sell) to re-secure the seed before the cycle repeats. The sold quantity is
floored toward zero (`floor(holdings / 4)`) and carries `order_type = OrderType.LOC`.

For `version` in the v2.x family, a **quarter-stop-loss** variant is noted as provisional [T9]
(a quarter-sell triggered by a loss threshold rather than seed exhaustion); the default behavior of
this function is the seed-exhausted quarter-sell defined above. `version` selects the variant later;
the default `version="v2.2"` uses the quarter-sell.

### REQ-MAB-001-R5 — MAB Strategy Wiring `MABStrategy` (Ubiquitous)

`@SPEC:SPEC-MAB-001` `REQ-MAB-001-R5`

The system **shall always** expose a `MABStrategy` class that satisfies the CORE-001 `Strategy`
protocol structurally:

- attribute `cadence = "daily"`,
- attribute `ns = "mab"`,
- method `plan_orders(self, market: Market, state: State, cfg: Config) -> list[Order]` that reads
  `avg_price` / `holdings` / `seed_remaining` / `round_idx` from `state`, and `n_splits` / `alpha` /
  `split_ratio` / `halftime_rule` / `version` / `seed` / `account_seq` / `ticker` (plus `target_pct`
  via the registry chain `resolve_target_pct`) from `cfg`, then wires the daily orders. When the seed
  is exhausted (`round_idx > n_splits`, equivalently `seed_remaining <= 0` after the final buy),
  `plan_orders` **shall** trigger the `mab_on_seed_exhausted` quarter-sell path instead of the daily
  buys.

`plan_orders` **shall** remain **pure**: it reads only its `market`, `state`, and `cfg` arguments and
performs no IO, no clock access, and no mutation of its inputs. MAB emits the `LOC` order type only.

## Specifications

| Capability                                 | Module                    | Contract                                                                                                                                                                                            |
| ------------------------------------------ | ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Daily two-point LOC buys + LOC profit-take | `src/ballast/core/mab.py` | `mab_daily_orders(avg_price, holdings, seed, seed_remaining, round_idx, n_splits, target_pct, alpha, split_ratio, *, halftime_rule="standard", version="v2.2", ticker, account_seq) -> list[Order]` |
| Seed-exhausted quarter-sell (LOC)          | `src/ballast/core/mab.py` | `mab_on_seed_exhausted(holdings, *, version="v2.2", ticker, account_seq) -> Order`                                                                                                                  |
| Strategy wiring (daily cadence, ns=mab)    | `src/ballast/core/mab.py` | `MABStrategy` implementing `Strategy`: `cadence="daily"`, `ns="mab"`, `plan_orders(market, state, cfg) -> list[Order]`                                                                              |

### Reused CORE-001 types (do NOT redefine)

- `Order`, `Market`, `State`, `Config` — `src/ballast/core/models.py`, `config.py`.
- `Side` (uses `Side.BUY` / `Side.SELL`), `OrderType` (uses `OrderType.LOC`) — `models.py`.
- `quantize_money` — `models.py` (single source of 2-place `ROUND_HALF_UP` rounding).
- `Strategy` protocol — `strategy.py` (`MABStrategy` conforms to it).
- `InstrumentRegistry.resolve_target_pct(ticker, explicit)` — `instrument.py`
  (explicit > `default_target_pct` > error) [T10].

### StrategyConfig fields proposed for the run phase (OPTIONAL, default-bearing, non-breaking)

`StrategyConfig` (`src/ballast/core/config.py`) currently carries the VR knobs plus `account_seq`,
`ticker`, `target_pct`, `band`. MAB needs the following **OPTIONAL** additions, all default-bearing so
existing CORE-001 / VR configs keep loading unchanged (mirrors how the VR knobs were added):

| Field           | Type                            | Default           | Purpose / TBD                                                  |
| --------------- | ------------------------------- | ----------------- | -------------------------------------------------------------- |
| `seed`          | `Money` (`Decimal`)             | `Decimal("0")`    | Total MAB cycle capital; per-round budget = seed/n_splits      |
| `n_splits`      | `int`                           | `40`              | Number of parts the seed is split into                         |
| `alpha`         | `Money` (`Decimal`)             | `Decimal("0.10")` | Step-up of the second buy price [T10] (matches config example) |
| `split_ratio`   | `Money` (`Decimal`)             | `Decimal("0.5")`  | Day-budget split between leg A and leg B [T10]                 |
| `halftime_rule` | `Literal["standard"]`           | `"standard"`      | Halftime phase rule selector [T9] (provisional)                |
| `version`       | `Literal["v1.0", ... , "v2.2"]` | `"v2.2"`          | MAB version variant [T9] (provisional)                         |

`target_pct` is already present in `StrategyConfig` and is resolved through the registry chain
(`resolve_target_pct`); MAB reuses it as the explicit override and never hardcodes it [T10].

## Dependency on SPEC-CORE-001

This SPEC has a **hard dependency** on SPEC-CORE-001. The MAB core compiles only against the domain
types and the `Strategy` protocol fixed there, and it resolves `target_pct` through the CORE-001
registry chain (`resolve_target_pct`). SPEC-CORE-001 must be implemented (or stubbed against its
contracts) before SPEC-MAB-001. It does NOT depend on SPEC-VR-001; it only mirrors `vr.py`'s
construction patterns.

## TBD Items (carried as parameters, not blockers)

| TBD | Item                                                  | Resolution carried as                                                                                                                                                                |
| --- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| T8  | Toss Open API LOC exposure                            | **LIVE-only**; the pure core stamps `OrderType.LOC` regardless and is fully buildable/backtestable now. KIS fallback is a later adapter SPEC. **Not a blocker for SPEC-MAB-001.**    |
| T9  | MAB version (V1–V2.2) + halftime rules                | `version` / `halftime_rule` params with **provisional, testable** defaults (`"v2.2"` / `"standard"`); primary mechanics (two-point buys, profit-take, quarter-sell) are well-defined |
| T10 | per-instrument `target_pct` / `alpha` / `split_ratio` | params + instrument default via the registry (`resolve_target_pct`) for `target_pct`; `alpha` / `split_ratio` carried as params with defaults + a sweep grid (later SPEC)            |

## Out of Scope (later SPECs)

- VR strategy core (already specified in SPEC-VR-001).
- Backtest engine + cost model (fees, 22% capital-gains tax, FX, slippage) + per-leverage baselines.
- Broker / market-data adapters (Toss / KIS), order manager, reconciliation.
- Scheduler (daily + cycle, DST-aware) and any live execution path (including the T8 LOC-exposure
  resolution / KIS fallback).

## Traceability

- `@SPEC:SPEC-MAB-001` — this document.
- `@TEST:SPEC-MAB-001` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/core/test_mab.py`.
- `@CODE:SPEC-MAB-001` — `src/ballast/core/mab.py`.
- `@DOC:SPEC-MAB-001` — generated during `/moai:3-sync`.
- Depends on `@SPEC:SPEC-CORE-001` (`src/ballast/core/{models,instrument,config,strategy}.py`).

### Requirement Index

| Requirement ID | EARS Type                  | Summary                                                                                                 |
| -------------- | -------------------------- | ------------------------------------------------------------------------------------------------------- |
| REQ-MAB-001-R1 | Ubiquitous + Event-driven  | `mab_daily_orders`: two-point LOC buys (avg, avg·(1+alpha)) + LOC profit-take when holdings>0           |
| REQ-MAB-001-R2 | State-driven (PROVISIONAL) | Halftime rule [T9]: 전반전 both legs, 후반전 near-avg leg only (reduced aggressiveness)                 |
| REQ-MAB-001-R3 | Unwanted/Constraint        | Cap budget at `seed_remaining`; `seed_remaining<=0` ⇒ no buys; floor dust ⇒ drop; holdings<=0 ⇒ no sell |
| REQ-MAB-001-R4 | Event-driven (PROVISIONAL) | `mab_on_seed_exhausted`: LOC quarter-sell `floor(holdings/4)`; v2.x quarter-stop-loss variant [T9]      |
| REQ-MAB-001-R5 | Ubiquitous                 | `MABStrategy` implements `Strategy` (cadence="daily", ns="mab"), pure `plan_orders` wiring              |
