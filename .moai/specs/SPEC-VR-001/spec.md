---
id: SPEC-VR-001
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

- Initial draft. Defines the VR ("Value Rebalancing") long-horizon strategy core as four
  pure functions plus one `Strategy`-conforming class in `src/ballast/core/vr.py`:
  `next_value`, `rebalance_decision`, `order_from_decision`, and `VRStrategy`.
- Builds directly on the contracts fixed in SPEC-CORE-001 (`Order`, `Decision`, `Market`,
  `State`, `Config`, the `Strategy` protocol, the instrument registry, and `quantize_money`).
  It REUSES those types; it does not redefine them.
- The skill formula is primary (`use_skill=True` by default). The basic-formula path and the
  exact placement of the growth-rate `r` are carried as a documented, testable TBD [T3].
- Scope intentionally excludes MAB, the backtest engine and cost model, broker/market-data
  adapters, the scheduler, and any live execution — those are later SPECs.

---

# SPEC-VR-001 — VR (Value Rebalancing) Strategy Core

`@SPEC:SPEC-VR-001`

## Environment

- Language: Python `>=3.11` (single language).
- Module: `src/ballast/core/vr.py`. PURE — no network, no filesystem, no clock
  (`datetime.now()` is forbidden). Time and prices arrive only via the `Market` snapshot
  and the `State`/`Config` arguments.
- Money/quantity: `Decimal` only (never `float`). Every monetary/quantity result is quantized to
  2 decimal places via `quantize_money` (`ROUND_HALF_UP`); tick-size is respected at the order boundary.
- `sqrt(G)` is computed with `Decimal` precision (a `decimal.Context`-driven `Decimal.sqrt`),
  never with `math.sqrt` (which returns `float`).
- Dependencies: standard library + `decimal` only for the math; the CORE-001 domain types for the
  contracts. No `pandas`/`numpy` in the core (those are backtest-only per the constitution).
- Tests: `pytest`, `pytest-cov`, `hypothesis` (property-based for financial invariants).
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: VR is the long-horizon strategy. Capital is split into an **attack asset** (a leveraged ETF)
  and **cash** (`pool`). Each cycle a **target value line `V`** is computed and the portfolio
  rebalances only when the attack-asset valuation `E` leaves a band around `V` (sell what rose,
  buy what fell).
- A2: VR's order type is **`reserved_limit`** (no LOC); cadence is **`cycle`** (default monthly [T2]).
- A3: This SPEC depends on **SPEC-CORE-001**. It consumes `Order`, `Decision`, `Market`, `State`,
  `Config`, the `Strategy` protocol, the instrument registry (`resolve_band`), and the
  `OrderType.RESERVED_LIMIT` / `Side` / `DecisionSide` enums and `quantize_money` helper — and
  redefines none of them.
- A4: `E` (attack-asset valuation) and `pool` (cash) follow the fixed definitions in §"Fixed
  Definitions" below, lifted from the source spec §5.4.
- A5: Band widths are **leverage-dependent** [T1]: they are not literals in the code. They are
  resolved through the CORE-001 chain — explicit strategy config first, then the instrument's
  `default_band`, then error — so a 3x instrument and a 2x instrument carry different bands.
- A6: The skill formula is the primary path. The basic formula and the exact role/placement of the
  growth-rate `r` are provisional [T3]; the SPEC fixes a testable provisional form and marks it so.
- A7: VR math is deterministic for a given `(V1, pool, E, G, flow, use_skill, r)` and for given
  band inputs — the same inputs always yield the same `V2` / `Decision` / `Order`.

## Fixed Definitions (from source spec §5.4 — normative)

These definitions are **fixed** and MUST be reflected in code (docstrings) and tests:

- **`E` (attack-asset valuation)** = valuation at the **trigger-time close**:
  live = the pre-close snapshot price; backtest = that bar's close price. Concretely
  `E = qty × close_price`, both `Decimal`.
- **`pool` (cash)** = the cash held **before this cycle's deposit**. A deposit/withdrawal enters the
  computation **only** through the `±flow` term of `next_value` — never by pre-adding it to `pool`.
- **`V` / `V1` / `V2`** = the target value line: `V1` is this cycle's line, `V2` is next cycle's line.
- All money/qty are `Decimal`, rounded to 2 decimal places, tick-size respected at the boundary.

## Requirements

The VR core MUST satisfy the following EARS requirements. All are tagged to `@SPEC:SPEC-VR-001`.
There are five requirement modules; together they cover all five EARS types.

### REQ-VR-001-R1 — Target Value Line `next_value` (Ubiquitous + Optional)

`@SPEC:SPEC-VR-001` `REQ-VR-001-R1`

The system **shall always** compute the next cycle's target value line `V2` from
`(V1, pool, E, G, flow)` using the **skill formula** (the default, `use_skill=True`):

```
V2 = V1 + pool/G + (E - V1) / (2 * sqrt(G)) ± flow
```

- `pool/G` schedules cash into the target over ~`G` cycles.
- `(E - V1) / (2*sqrt(G))` is the **skill correction**: it **raises** the target as price rises
  (so the next rebalance sells more) and **suppresses/lowers** it on a crash (so the next rebalance
  buys less and preserves cash); `sqrt(G)` damps the magnitude of this correction.
- `G` is the **gradient** (larger = more conservative). Provisional defaults: accumulate/hold = 10,
  withdraw = 20.
- `flow` is the signed deposit/withdrawal applied at the end (`accumulate` +, `withdraw` −,
  `hold` 0). The sign of `flow` **shall** move `V2` in the same direction.

**Where** `use_skill` is `False`, the system **shall** instead use the **basic formula**, which
removes the skill-correction term and lets a separate growth-rate `r` govern the climb. The exact
placement of `r` is a documented TBD [T3]; this SPEC fixes the following **provisional, testable**
form (clearly marked provisional, subject to change when [T3] is resolved):

```
V2 = V1 * (1 + r) + pool/G ± flow            # PROVISIONAL [T3]
```

`sqrt(G)` **shall** be computed with `Decimal` precision (a `decimal.Context`-driven `Decimal.sqrt`),
and the final `V2` **shall** be quantized to 2 decimal places. The default **shall** be `use_skill=True`.

### REQ-VR-001-R2 — Rebalance Decision `rebalance_decision` (Event-driven + State-driven)

`@SPEC:SPEC-VR-001` `REQ-VR-001-R2`

The function `rebalance_decision(E, V, min_band, max_band, target_mode)` returns a CORE-001
`Decision`.

- **When** `E > V * (1 + max_band)` (the attack asset has risen out of the band), the system
  **shall** return a **SELL** `Decision` (Event-driven).
- **When** `E < V * (1 - min_band)` (the attack asset has fallen out of the band), the system
  **shall** return a **BUY** `Decision` (Event-driven).
- **While** `V * (1 - min_band) <= E <= V * (1 + max_band)` (the attack asset stays inside the band),
  the system **shall** return a **HOLD** `Decision` with `target_amount = 0` (State-driven).

`target_mode` ∈ {`center`, `edge`} [T4] sets the **magnitude** of `Decision.target_amount`:

- `center` — rebalance back to the line `V`:
  SELL `target_amount = E - V`; BUY `target_amount = V - E`.
- `edge` — rebalance back to the **nearest band edge**:
  SELL `target_amount = E - V * (1 + max_band)`; BUY `target_amount = V * (1 - min_band) - E`.

Band widths **shall** be derived from the instrument registry (`default_band`) when not explicitly
set, via the CORE-001 resolution chain [T1]; bands are **leverage-dependent** and this dependency
**shall** be documented (a 3x instrument carries a different band than a 2x instrument).

### REQ-VR-001-R3 — Order Construction `order_from_decision` (Event-driven + Unwanted)

`@SPEC:SPEC-VR-001` `REQ-VR-001-R3`

The function `order_from_decision(decision, price, holdings, pool, allow_fractional, ...)` converts a
non-HOLD `Decision` into a concrete order.

- **When** a `SELL`/`BUY` `Decision` is supplied, the system **shall** convert
  `side + target_amount` into a `qty` at `price` and produce a **`reserved_limit`** `Order`
  (Event-driven). For a `HOLD` decision (or `target_amount == 0`) the system **shall** return `None`.
- The system **shall** clamp a `BUY` to available **buying power** (`pool`): the cash committed
  (`qty * price`) **shall not** exceed `pool`.
- The system **shall** clamp a `SELL` to the **sellable** quantity (`holdings`): `qty` **shall not**
  exceed `holdings`.
- **Where** `allow_fractional` is `False`, the system **shall** floor `qty` to whole shares.
- **If** the resulting `qty` rounds to `0`, **then** the system **shall not** emit an order and
  **shall** return `None` (Unwanted behavior — never place a zero/dust order).

### REQ-VR-001-R4 — VR Strategy Wiring `VRStrategy` (Ubiquitous)

`@SPEC:SPEC-VR-001` `REQ-VR-001-R4`

The system **shall always** expose a `VRStrategy` class that satisfies the CORE-001 `Strategy`
protocol structurally:

- attribute `cadence = "cycle"`,
- attribute `ns = "vr"`,
- method `plan_orders(self, market: Market, state: State, cfg: Config) -> list[Order]` that wires
  `next_value` → `rebalance_decision` → `order_from_decision`, reading `V_n`, `pool`, and `qty` from
  `state` and the bands / `account_seq` / `ticker` / `G` / `flow` / `use_skill` knobs from `cfg`.

`plan_orders` **shall** remain **pure**: it reads only its `market`, `state`, and `cfg` arguments and
performs no IO, no clock access, and no mutation of its inputs.

### REQ-VR-001-R5 — Fixed Definitions & Core Constraints (Unwanted/Constraint)

`@SPEC:SPEC-VR-001` `REQ-VR-001-R5`

The system **shall** honor the fixed definitions (§"Fixed Definitions"): `E = qty × trigger-close
price`; `pool` = cash **before** this cycle's deposit, with the deposit entering only via `±flow`.

Constraints (prohibitions):

- The system **shall not** use `float` for any money/quantity value, including the `sqrt(G)`
  computation — `Decimal` throughout, quantized to 2 places.
- The system **shall not** read the wall clock, network, or filesystem inside `src/ballast/core/vr.py`.
- The system **shall not** emit any order type other than `reserved_limit` for VR (no `LOC`,
  no `market`).
- The system **shall not** place an order while `E` is inside the band (HOLD ⇒ no order), and
  **shall not** place an order whose quantity rounds to `0`.
- The system **shall not** hardcode band widths or `G` — bands flow through the registry [T1] and
  `G`/`flow`/`use_skill`/`target_mode` arrive from config/arguments.

## Specifications

| Capability                             | Module                   | Contract                                                                                                                |
| -------------------------------------- | ------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| Next target value line (skill + basic) | `src/ballast/core/vr.py` | `next_value(V1, pool, E, G, flow, *, use_skill=True, r=Decimal("0")) -> Decimal`                                        |
| Rebalance decision (band test + mode)  | `src/ballast/core/vr.py` | `rebalance_decision(E, V, min_band, max_band, target_mode="center") -> Decision`                                        |
| Decision → order (clamp + floor)       | `src/ballast/core/vr.py` | `order_from_decision(decision, price, holdings, pool, *, allow_fractional=False, ticker, account_seq) -> Order \| None` |
| Strategy wiring (cycle cadence, ns=vr) | `src/ballast/core/vr.py` | `VRStrategy` implementing `Strategy`: `cadence="cycle"`, `ns="vr"`, `plan_orders(market, state, cfg) -> list[Order]`    |

### Reused CORE-001 types (do NOT redefine)

- `Order`, `Decision`, `Market`, `State`, `Config` — `src/ballast/core/models.py`, `config.py`.
- `Side`, `DecisionSide`, `OrderType` (uses `OrderType.RESERVED_LIMIT`) — `models.py`.
- `quantize_money` — `models.py` (single source of 2-place `ROUND_HALF_UP` rounding).
- `Strategy` protocol — `strategy.py`.
- `InstrumentRegistry.resolve_band(...)` — `instrument.py` (explicit > `default_band` > error) [T1].

## Dependency on SPEC-CORE-001

This SPEC has a **hard dependency** on SPEC-CORE-001. The VR core compiles only against the domain
types and the `Strategy` protocol fixed there, and it resolves `band` through the CORE-001 registry
chain. SPEC-CORE-001 must be implemented (or stubbed against its contracts) before SPEC-VR-001.

## TBD Items (carried as parameters, not blockers)

| TBD | Item                          | Resolution carried as                                                                                 |
| --- | ----------------------------- | ----------------------------------------------------------------------------------------------------- |
| T1  | VR band widths (per leverage) | `min_band` / `max_band` params + instrument `default_band` (CORE-001 chain) + sweep grid (later SPEC) |
| T2  | Cycle length                  | a config/param value; **default monthly** (scheduler in a later SPEC consumes it)                     |
| T3  | Basic-formula shape / `r`     | **provisional** `V2 = V1*(1+r) + pool/G ± flow`; skill formula is primary (`use_skill=True` default)  |
| T4  | `target_mode` center vs edge  | `target_mode ∈ {center, edge}` param; **default `center`**                                            |

## Out of Scope (later SPECs)

- MAB strategy core (`mab_daily_orders`, `mab_on_seed_exhausted`).
- Backtest engine + cost model (fees, 22% capital-gains tax, FX, slippage) + per-leverage baselines.
- Broker / market-data adapters (Toss / KIS), order manager, reconciliation.
- Scheduler (daily + cycle, DST-aware) and any live execution path.

## Traceability

- `@SPEC:SPEC-VR-001` — this document.
- `@TEST:SPEC-VR-001` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/core/test_vr.py`.
- `@CODE:SPEC-VR-001` — `src/ballast/core/vr.py`.
- `@DOC:SPEC-VR-001` — generated during `/moai:3-sync`.
- Depends on `@SPEC:SPEC-CORE-001` (`src/ballast/core/{models,instrument,config,strategy}.py`).

### Requirement Index

| Requirement ID | EARS Type                   | Summary                                                                                    |
| -------------- | --------------------------- | ------------------------------------------------------------------------------------------ |
| REQ-VR-001-R1  | Ubiquitous + Optional       | `next_value`: skill formula (primary) + basic-formula/`r` path [T3]; Decimal `sqrt(G)`     |
| REQ-VR-001-R2  | Event-driven + State-driven | `rebalance_decision`: SELL/BUY out of band, HOLD in band; `target_mode` center/edge [T4]   |
| REQ-VR-001-R3  | Event-driven + Unwanted     | `order_from_decision`: reserved_limit order; clamp BUY→pool / SELL→holdings; floor; 0→None |
| REQ-VR-001-R4  | Ubiquitous                  | `VRStrategy` implements `Strategy` (cadence="cycle", ns="vr"), pure `plan_orders` wiring   |
| REQ-VR-001-R5  | Unwanted/Constraint         | Fixed defs (E, pool); no float / no IO / reserved_limit only / no in-band or zero order    |
