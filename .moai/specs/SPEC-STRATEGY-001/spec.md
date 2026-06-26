---
id: SPEC-STRATEGY-001
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

- Initial draft. Resolves the **two HANDOFF §4 pre-live correctness follow-ups** — both required before
  faithful live/backtest behavior:
  1. **MAB quarter-sell LOC price** (smaller, MAB-local): `mab_on_seed_exhausted` emits
     `OrderType.LOC` with `limit_price=None` (effectively MOC). Backtest fills at close (fine), but the
     live Toss path (SPEC-ADAPTER-002) rejects a price-less `LIMIT + timeInForce=CLS` (Toss has no MOC)
     and surfaces it as `FAILED`. The quarter-sell LOC is given a **deterministic, injected-price**
     limit so it is a valid priced LOC live, while preserving the backtest close-fill behavior.
  2. **VR `V_n` multi-cycle evolution** (larger, `Strategy`-contract change): `Strategy.plan_orders`
     returns **only** orders, so the backtest engine cannot observe the recomputed VR value line `V_n`
     across cycles → multi-cycle VR replay is **not faithful** (the engine feeds the same stale seed
     `V_n` back every cycle). The `Strategy` contract is enriched with a **state-evolution channel** so
     the engine can advance `V_n` each cycle. MAB is unaffected (its state derives from fills).
- These changes **intentionally change behavior** to fix correctness. Per Constitution (DDD), the work
  is **ANALYZE-PRESERVE-IMPROVE**: characterization tests pin the current behavior first, then the
  corrected behavior is asserted with explicit new assertions. The exact `Strategy`-contract mechanism
  for follow-up 2 has **three candidate forms** (state-delta return / `advance_state` method / engine
  calls `next_value`); they are enumerated with trade-offs and a **RECOMMENDATION** in `plan.md` and
  are flagged as a **design decision for orchestrator/user review** — this SPEC pins the required
  outcome, not the mechanism.
- Reuses SPEC-CORE-001 (`Order`, `OrderType`, `Side`, `Market`, `State`, `quantize_money`, the
  `Strategy` protocol), SPEC-VR-001 (`next_value`/`VRStrategy`), SPEC-MAB-001
  (`mab_on_seed_exhausted`/`MABStrategy`), and SPEC-BACKTEST-001 (`run_backtest`). It forks none of
  these types; if the `Strategy` protocol changes, VR / MAB / the engine are updated **consistently**.

---

# SPEC-STRATEGY-001 — Pre-Live Strategy Correctness (MAB quarter-sell LOC price + VR `V_n` multi-cycle evolution)

`@SPEC:SPEC-STRATEGY-001`

> Two correctness fixes required before _faithful_ live/backtest behavior (HANDOFF §4). The core stays
> **pure** (no IO, no clock, no `float`); money/quantity are `Decimal` normalized to 2 dp via
> `quantize_money`. Time and prices are **injected** via the `Market` snapshot. No new third-party deps.

## Environment

- Language: Python `>=3.11` (single language; financial values are `Decimal`).
- Packaging/deps: `uv` + `pyproject.toml`. **No new runtime/test third-party deps** — the change is
  internal to the existing pure core (`src/ballast/core/`) and the backtest engine
  (`src/ballast/backtest/`).
- Module locations (touch points):
  - `src/ballast/core/strategy.py` — the shared `Strategy` protocol (contract enrichment for R2).
  - `src/ballast/core/vr.py` — `VRStrategy` produces the recomputed `V_n` through the new channel (R3);
    `next_value` itself is **unchanged**.
  - `src/ballast/core/mab.py` — `mab_on_seed_exhausted` gains a deterministic injected-price LOC limit
    (R1); `MABStrategy` conforms to the enriched contract with an **empty** state channel.
  - `src/ballast/backtest/engine.py` — the engine **observes and applies** the recomputed `V_n` each
    cycle (R4); MAB bookkeeping (fill-derived) is untouched.
- Purity: the core (`src/ballast/core/`) performs **no** network/filesystem IO and **never** reads the
  wall clock (`datetime.now()` is forbidden). The quarter-sell reference price and all VR inputs arrive
  via the injected `Market` snapshot and `State`; knobs via `Config`.
- Money/quantity: `Decimal` only (never `float`), normalized to 2 dp via `quantize_money` at the
  `Order`/`Market`/`State` boundaries. The backtest curve/ratio math MAY use `numpy`/`float` (curve math
  only, never the money ledger) — unchanged by this SPEC.
- Tests: `pytest`, `pytest-cov`. DDD ANALYZE-PRESERVE-IMPROVE: **characterization tests first** (pin
  current behavior), then corrected-behavior tests. The multi-cycle VR replay is validated through
  `run_backtest` over a synthetic ascending bar series (no network).
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: **The two fixes are independent in mechanism but shipped together.** Follow-up 1 (MAB LOC price)
  is MAB-local (`mab.py` only). Follow-up 2 (VR `V_n`) changes the shared `Strategy` contract and so
  touches `strategy.py` + `vr.py` + `mab.py` + `engine.py`. They are bundled because both are pre-live
  correctness blockers.
- A2: **Behavior is intentionally changed.** Both follow-ups alter observable behavior to fix
  correctness. This is **not** a behavior-preserving refactor; it is a corrected-behavior change guarded
  by characterization tests (which document the prior, buggy behavior) plus new assertions.
- A3: **MAB is unaffected by the contract change.** MAB strategy state (`avg_price` / `holdings` /
  `seed_remaining` / `round_idx`) is derived by the engine from **fills**, not from a strategy-returned
  state. Whatever state-evolution channel is added, MAB returns an **empty** delta / no-op, and its
  fill-driven bookkeeping is identical to today.
- A4: **VR single-cycle orders are unchanged.** For any single cycle, the order VR plans from a given
  seed `V_n` is byte-for-byte identical before and after the fix. Only the **cross-cycle** advancement
  of `V_n` is corrected (today it is stale; after the fix it advances).
- A5: **The quarter-sell reference price is injected.** The deterministic LOC limit for the
  seed-exhausted quarter-sell is computed purely from an injected reference price (the cycle's
  current/close price from `Market`), never from a wall clock or network. In backtest this reference is
  the bar close, so the priced LOC fills at close exactly as the price-less LOC did.
- A6: **Money is `Decimal`, 2 dp.** Every price/qty/amount is `Decimal`, normalized via
  `quantize_money`. `float` never appears on the money path; the `sqrt(G)` path in `next_value` (already
  `Decimal.sqrt`) is unchanged.
- A7: **No type forking.** `Order` / `OrderType` / `Side` / `Market` / `State` / `Decision` and the
  `Strategy` protocol are reused. If `Strategy.plan_orders` changes shape, VR, MAB, and the engine are
  updated in lockstep; the protocol remains the **single** strategy contract.
- A8: **Engine remains generic.** The engine drives strategies only through the `Strategy` protocol. The
  preferred contract enrichment keeps the engine from hardcoding VR-specific knowledge (the engine
  applies an opaque, namespace-scoped state delta to its ledger rather than reaching into `vr.next_value`).

## Fixed Definitions

These are pinned for this SPEC and MUST NOT drift silently. Changing any of them requires a HISTORY
entry. (FD3 deliberately fixes the _outcome_ of the `Strategy`-contract change, not the _mechanism_ —
the mechanism choice is the reviewed decision in `plan.md`.)

- **FD1 — Quarter-sell LOC is priced deterministically from an injected reference.** The seed-exhausted
  quarter-sell (`mab_on_seed_exhausted`) **shall** carry a non-`None` `limit_price` equal to the
  injected reference price (the cycle's current/close price supplied via `Market.current_price`),
  normalized to 2 dp via `quantize_money`. Quantity is unchanged: `qty = floor(holdings / 4)`
  (`ROUND_FLOOR`), `order_type = OrderType.LOC`, `side = SELL`. This rule keeps backtest close-fill
  behavior identical (limit == close ⇒ LOC fills at close) and yields a live-valid priced LOC.
- **FD2 — MAB quarter-sell is the only None-priced order removed.** The MAB daily LOC BUY legs and the
  LOC profit-take SELL already carry a derived `limit_price` (around `avg_price`); they are **unchanged**.
  Only `mab_on_seed_exhausted` changes from `limit_price=None` to the FD1 priced limit.
- **FD3 — The `Strategy` contract exposes recomputed strategy state.** `Strategy.plan_orders` (or an
  accompanying contract member) **shall** make the strategy's recomputed, persist-worthy state for the
  snapshot observable to the engine, as a **namespace-scoped state delta** of `Decimal`-valued keys (VR:
  at least `{"V_n": V2}`; MAB: an **empty** delta). The engine **shall** apply this delta to its ledger
  each cycle so the next cycle reads the advanced state. The exact contract member — (1) a richer
  `plan_orders` return carrying the delta, (2) a separate `advance_state` method, or (3) the engine
  calling `next_value` directly — is the **reviewed design decision** in `plan.md`; the leading/RECOMMENDED
  form is (1). Whatever form is chosen, FD3's observable outcome holds.
- **FD4 — VR delta is single-pass and consistent.** The `V_n` the engine persists for cycle _n_ **shall
  be the same** `V2` value VR used to compute that cycle's rebalance decision (no double computation that
  could diverge). VR already computes `V2 = next_value(...)` once inside `plan_orders`; the delta surfaces
  exactly that value.
- **FD5 — MAB delta is empty / no-op.** `MABStrategy` **shall** conform to the enriched contract by
  emitting an **empty** state delta. The engine **shall not** apply any strategy-returned state to MAB
  bookkeeping; MAB state remains 100% fill-derived (`avg_price`/`holdings`/`seed_remaining`/`round_idx`).
- **FD6 — Single-cycle VR orders are invariant.** Given an identical seed `V_n`, `pool`, `qty`, price,
  and config, the order(s) VR plans for one cycle are **identical** before and after this SPEC. Only the
  cross-cycle `V_n` advancement changes.
- **FD7 — Purity and `Decimal` preserved.** The core stays pure (no IO/clock/`float`); the engine reads
  no wall clock. All money/quantity stays `Decimal` (2 dp) via `quantize_money`. The state delta carries
  `Decimal` values only; a bare `float` is never introduced on the money path.

## Requirements

The system MUST satisfy the following EARS requirements (4 modules). All are tagged to
`@SPEC:SPEC-STRATEGY-001`.

### REQ-STRATEGY-001-R1 — MAB Quarter-Sell LOC Carries a Deterministic Injected Price (Event-driven + Unwanted)

`@SPEC:SPEC-STRATEGY-001` `REQ-STRATEGY-001-R1`

- **When** the MAB seed is exhausted and the strategy plans the seed-exhausted quarter-sell
  (`mab_on_seed_exhausted` / `MABStrategy.plan_orders` on the `round_idx > n_splits` path), the system
  **shall** produce a single `OrderType.LOC` SELL of `qty = floor(holdings / 4)` (`ROUND_FLOOR`) whose
  `limit_price` is the injected reference price (the cycle's current/close price from
  `Market.current_price`), normalized to 2 dp via `quantize_money` (FD1).
- The system **shall not** emit a `limit_price=None` quarter-sell. (Today's price-less LOC is MOC-style
  and is rejected by the live Toss path as a price-less `LIMIT + CLS`.)
- The reference price **shall** be **injected** (via `Market`), never read from a wall clock or network;
  `mab_on_seed_exhausted` **shall** accept the reference price as an explicit `Decimal` argument and
  `MABStrategy.plan_orders` **shall** pass `market.current_price` to it. The function **shall** remain
  pure.
- The MAB daily LOC BUY legs and the LOC profit-take SELL **shall** be unchanged (FD2); only the
  quarter-sell gains a price.
- **Backtest preservation:** because the LOC limit equals the bar close, the quarter-sell **shall** fill
  at the close and apply to the engine ledger **identically** to today's price-less LOC (no change to
  realized P&L, holdings, or tax accrual for the quarter-sell).

### REQ-STRATEGY-001-R2 — `Strategy` Contract Exposes Recomputed Strategy State to the Engine (Ubiquitous)

`@SPEC:SPEC-STRATEGY-001` `REQ-STRATEGY-001-R2`

> The exact contract member is the reviewed design decision in `plan.md` (three options); the
> leading/RECOMMENDED form is a richer `plan_orders` return carrying a namespace-scoped state delta.
> This requirement pins the **outcome** (FD3) regardless of which option is adopted.

- The system **shall** enrich the shared `Strategy` contract (`src/ballast/core/strategy.py`) so that,
  for a given `(Market, State, Config)` snapshot, a strategy makes its recomputed, persist-worthy state
  observable to the driver as a **namespace-scoped state delta**: a mapping of `Decimal`-valued state
  keys the engine should carry into the next cycle (FD3).
- The contract **shall** remain a pure structural `typing.Protocol`: it prescribes **no** IO, **no**
  clock, and ships **no** concrete implementation. The state delta **shall** carry `Decimal` values only
  (FD7).
- The enriched contract **shall** be the **single** strategy contract: VR, MAB, the backtest engine, and
  the parameter-sweep harness (`src/ballast/backtest/sweep.py`) **shall** all conform to the same shape;
  no second `Strategy`/`Order` type is forked (A7).
- A strategy that has **no** evolving internal state (e.g. MAB) **shall** be able to satisfy the contract
  by returning an **empty** delta (FD5); the contract **shall not** force such a strategy to invent
  state.

### REQ-STRATEGY-001-R3 — `VRStrategy` Produces the Recomputed `V_n` Through the New Channel (Event-driven + State-driven)

`@SPEC:SPEC-STRATEGY-001` `REQ-STRATEGY-001-R3`

- **When** `VRStrategy.plan_orders` runs for a cycle, the system **shall**, in addition to the
  order(s) it already returns, surface the recomputed value line `V2 = next_value(...)` for that cycle
  through the R2 state channel as `{"V_n": V2}` (FD3, FD4).
- The `V_n` surfaced **shall be exactly** the `V2` value VR used to compute that cycle's
  `rebalance_decision` (single-pass; no second `next_value` evaluation that could diverge) (FD4).
- **While** VR plans for a single cycle from a given seed `V_n`/`pool`/`qty`/price/config, the order(s)
  it returns **shall be identical** to today's (FD6); `next_value`, `rebalance_decision`, and
  `order_from_decision` **shall** be unchanged.
- `VRStrategy.plan_orders` **shall** remain pure: it reads only its `market`/`state`/`cfg` arguments and
  mutates none of them; the surfaced `V_n` is a `Decimal` (2 dp via `quantize_money`, as already produced
  by `next_value`).

### REQ-STRATEGY-001-R4 — Backtest Engine Observes and Applies `V_n` for Faithful Multi-Cycle Replay (Event-driven + Unwanted)

`@SPEC:SPEC-STRATEGY-001` `REQ-STRATEGY-001-R4`

- **When** the engine triggers a strategy on a cycle and receives the R2 state delta, the system
  **shall** apply that delta to its bookkeeping ledger (for VR: write the returned `V_n` into
  `_Ledger.v_n`) so that the **next** cycle's `_build_state` exposes the **advanced** `V_n` (not the
  stale seed) (FD3).
- **While** a multi-cycle VR backtest runs over N cycles, the value line `V_n` the engine feeds into
  cycle _n+1_ **shall** be the `V2` VR computed in cycle _n_ (faithful multi-cycle evolution), replacing
  today's behavior where `_Ledger.v_n` is seeded once and **never advanced** (the same stale `V_n` is
  fed every cycle).
- The engine **shall not** apply any strategy-returned state to **MAB** bookkeeping: MAB's delta is
  empty (FD5), and `avg_price`/`holdings`/`seed_remaining`/`round_idx` remain 100% fill-derived; a MAB
  backtest result (curve, fills, trades, realized P&L, tax, fingerprint inputs) **shall** be unchanged.
- The engine **shall** remain pure toward its inputs (it never mutates `Config` or the passed-in
  `State`) and **shall** read no wall clock; it applies the delta only to its own mutable `_Ledger`.
- **While** the chosen contract form is option (1)/(2) (state-delta return / `advance_state`), the engine
  **shall not** hardcode VR-specific knowledge: it applies an opaque, namespace-scoped `Decimal` delta
  rather than calling `vr.next_value` itself (A8). (Option (3) is documented as the dispreferred
  alternative in `plan.md`.)

## Specifications

Core/engine input → change → implementing module.

| Concern (REQ)                                             | Input (reused)                                       | Module / change                                                                                      |
| --------------------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Quarter-sell priced LOC `R1`                              | `Market.current_price`, `holdings`, `quantize_money` | `src/ballast/core/mab.py` — `mab_on_seed_exhausted(+ref_price)`, `MABStrategy.plan_orders` passes it |
| `Strategy` contract enrichment (state-delta channel) `R2` | CORE `Market`/`State`/`Config`, `Order`              | `src/ballast/core/strategy.py` — enrich `plan_orders` return (RECOMMENDED) or add `advance_state`    |
| VR surfaces recomputed `V_n` `R3`                         | `next_value` (`V2`) already computed in `VRStrategy` | `src/ballast/core/vr.py` — `VRStrategy.plan_orders` returns orders + `{"V_n": V2}`                   |
| MAB conforms with empty delta `R2`/`R4`                   | n/a                                                  | `src/ballast/core/mab.py` — `MABStrategy.plan_orders` returns orders + empty delta                   |
| Engine applies `V_n` delta each cycle `R4`                | the R2 delta, `_Ledger.v_n`                          | `src/ballast/backtest/engine.py` — capture delta, write `ledger.v_n`, generic `_apply_state_delta`   |
| Sweep harness still conforms `R2`                         | `run_backtest` pass-through                          | `src/ballast/backtest/sweep.py` — type-only conformance (no `plan_orders` call site to change)       |

## Dependencies

- **Depends on SPEC-CORE-001**: reuses `ballast.core.models.{Order, OrderType, Side, Market, State,
Decision, quantize_money}` and the `ballast.core.strategy.Strategy` protocol (the protocol is the
  artifact enriched by R2). No CORE type is forked.
- **Depends on SPEC-VR-001**: `next_value` / `VRStrategy` — `next_value` is unchanged; `VRStrategy`
  surfaces the value it already computes (R3).
- **Depends on SPEC-MAB-001**: `mab_on_seed_exhausted` / `MABStrategy` — the quarter-sell gains a priced
  LOC (R1); `MABStrategy` conforms with an empty delta.
- **Depends on SPEC-BACKTEST-001**: `run_backtest` / `_Ledger` / `_build_state` — the engine observes and
  applies the `V_n` delta (R4). The sweep harness (SPEC-BACKTEST-002) only passes the strategy through.
- **Enables faithful downstream live behavior**: the priced quarter-sell LOC flows correctly through the
  SPEC-ORDER-001 mapping (`LOC → LIMIT + CLS`, now with a price) and the SPEC-ADAPTER-002 Toss write
  adapter (a priced `LIMIT + CLS`, accepted instead of `FAILED`).
- **No new third-party deps.**

## Scope

### In scope

- Give the MAB seed-exhausted quarter-sell a deterministic, injected-price LOC limit (R1), preserving
  backtest close-fill behavior and unblocking the live Toss path.
- Enrich the shared `Strategy` contract with a namespace-scoped state-evolution channel (R2), have
  `VRStrategy` surface its recomputed `V_n` through it (R3), and have the backtest engine apply that
  delta each cycle so multi-cycle VR replay is faithful (R4) — with MAB explicitly unaffected.
- Characterization tests (current behavior) + corrected-behavior tests for both clusters; consistent
  updates to VR / MAB / engine / sweep type conformance.

### Out of scope (deferred / not changed)

The system **shall not** do any of the following in this SPEC:

- Change the **VR math** (`next_value`/`rebalance_decision`/`order_from_decision`) or the MAB daily/
  profit-take mechanics — only the quarter-sell price and the cross-cycle state channel change.
- Persist strategy state to any external store (the State Store backlog item is separate); the only
  "persistence" here is the engine carrying the delta into the next in-memory cycle.
- Implement or modify any broker write call — the priced LOC simply flows through the **existing**
  SPEC-ORDER-001 mapping and SPEC-ADAPTER-002 adapter unchanged.
- Add a MOC order type or any non-`Decimal` money path; adopt MOC for the quarter-sell (rejected — the
  task requires a **priced** LOC because Toss has no MOC).
- **Finalize the `Strategy`-contract mechanism** (option 1 vs 2 vs 3). The three options + RECOMMENDATION
  are in `plan.md` and are flagged for orchestrator/user review; this SPEC pins only the required
  outcome (FD3).

## Reality Constraints

- Multi-cycle VR fidelity (R3/R4) is validated **only** through `run_backtest` over a synthetic ascending
  bar series in-memory (no network, no real broker). The priced quarter-sell LOC's live acceptance is a
  property of the downstream SPEC-ADAPTER-002 adapter and is validated there / by the user's local soak —
  it is not, and cannot be, an automated gate in this environment.
- Core stays pure: no IO, no clock, no `float`. Money/quantity is `Decimal` (2 dp) via `quantize_money`.
  The engine reads no wall clock and never mutates `Config` or the passed-in `State`; it applies the
  delta only to its own `_Ledger`.

## Traceability

- `@SPEC:SPEC-STRATEGY-001` — this document.
- `@TEST:SPEC-STRATEGY-001` — see `acceptance.md` Given/When/Then scenarios + characterization and
  corrected-behavior tests in `tests/unit/core/{test_mab,test_vr,test_strategy}.py` and
  `tests/unit/backtest/{test_engine,test_engine_paths}.py`.
- `@CODE:SPEC-STRATEGY-001` — `src/ballast/core/{strategy,vr,mab}.py` and
  `src/ballast/backtest/engine.py` (+ `sweep.py` type conformance).
- `@DOC:SPEC-STRATEGY-001` — generated during `/moai:3-sync`.

### Requirement Index

| Requirement ID      | EARS Type                   | Summary                                                                                                |
| ------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------ |
| REQ-STRATEGY-001-R1 | Event-driven + Unwanted     | MAB seed-exhausted quarter-sell LOC carries a deterministic injected (close) price; no `None` price    |
| REQ-STRATEGY-001-R2 | Ubiquitous                  | `Strategy` contract enriched with a namespace-scoped `Decimal` state-delta channel (mechanism: review) |
| REQ-STRATEGY-001-R3 | Event-driven + State-driven | `VRStrategy` surfaces recomputed `V_n` (`V2`) through the channel; single-cycle orders unchanged       |
| REQ-STRATEGY-001-R4 | Event-driven + Unwanted     | Backtest engine applies the `V_n` delta each cycle (faithful multi-cycle VR); MAB unaffected           |
