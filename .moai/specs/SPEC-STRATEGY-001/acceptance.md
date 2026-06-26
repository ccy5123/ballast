# SPEC-STRATEGY-001 — Acceptance Criteria

`@SPEC:SPEC-STRATEGY-001` `@TEST:SPEC-STRATEGY-001`

> All scenarios are validated by **in-memory unit tests** (no network, no broker). DDD
> ANALYZE-PRESERVE-IMPROVE: characterization scenarios pin the **current** behavior; corrected scenarios
> assert the **new** behavior. Money/quantity is `Decimal` (2 dp via `quantize_money`); the core stays
> pure (no IO, no clock, no `float`); time/prices are injected via `Market`. Multi-cycle VR fidelity is
> validated through `run_backtest` over a synthetic ascending bar series.

## Definition of Done

- All scenarios below pass in-memory.
- `ruff` clean, `ruff format --check` clean, `mypy --strict src` → 0 errors (across `strategy.py` /
  `vr.py` / `mab.py` / `engine.py` / `sweep.py`).
- Coverage ≥ 85% for the touched `src/ballast/core/**` and `src/ballast/backtest/engine.py` (core-like;
  target ~100%).
- Characterization tests (current behavior) are committed alongside the corrected-behavior tests; the
  preserved vs intentionally-changed behaviors are each asserted explicitly.
- No wall-clock read and no network IO inside `src/ballast/core/**`; the engine reads no wall clock and
  never mutates `Config` or the passed-in `State`.

---

## Cluster 1 — MAB Quarter-Sell LOC Price (R1)

### AC-1 — (Characterization) current quarter-sell is a price-less LOC (R1)

```gherkin
Given the pre-fix mab_on_seed_exhausted(holdings=Decimal("17"), ticker="SOXL", account_seq="acc-1")
When the seed-exhausted quarter-sell Order is produced
Then order_type == OrderType.LOC and side == Side.SELL
And qty == Decimal("4.00")            # floor(17 / 4) == 4, then quantize_money
And limit_price is None               # documents the current (MOC-style) behavior to be changed
```

### AC-2 — (Corrected) quarter-sell carries a deterministic injected (close) price (R1)

```gherkin
Given holdings=Decimal("17") and an injected reference (close) price ref_price=Decimal("12.34")
When mab_on_seed_exhausted(holdings, ref_price, ticker="SOXL", account_seq="acc-1") is called
Then order_type == OrderType.LOC and side == Side.SELL
And qty == Decimal("4.00")                       # quantity rule unchanged: floor(holdings / 4)
And limit_price == Decimal("12.34")              # == quantize_money(ref_price); NOT None
And limit_price is a Decimal (not float)
```

### AC-3 — (Corrected) MABStrategy passes market.current_price into the quarter-sell (R1)

```gherkin
Given a MAB State with round_idx > n_splits (seed exhausted) and holdings=Decimal("17")
And a Market with current_price=Decimal("12.34")
When MABStrategy.plan_orders(market, state, cfg) is called
Then it returns exactly one LOC SELL whose limit_price == Decimal("12.34") (the injected close)
And plan_orders read no wall clock and mutated neither state nor cfg
```

### AC-4 — (Preserved) backtest quarter-sell still fills at the close, identical ledger effect (R1)

```gherkin
Given a backtest cycle on a bar whose close == C reaches the MAB seed-exhausted quarter-sell
When run_backtest executes the quarter-sell
  (pre-fix: limit_price=None ; post-fix: limit_price=quantize_money(C))
Then the fill price, resulting holdings, realized P&L, and accrued tax for that quarter-sell
     are IDENTICAL pre-fix and post-fix
And the LOC fills at the close in both cases (limit == close ⇒ close fill)
```

### AC-5 — (Preserved) MAB daily legs and profit-take are unchanged (R1)

```gherkin
Given a MAB daily (non-exhausted) cycle with holdings > 0
When MABStrategy.plan_orders(market, state, cfg) is called
Then the LOC BUY legs (near-average and step-up) and the LOC profit-take SELL
     are produced exactly as before (same qty / limit_price / order_type)
And only the seed-exhausted quarter-sell path changed
```

---

## Cluster 2 — Strategy Contract + VR `V_n` (R2, R3)

### AC-6 — (Characterization) current Strategy.plan_orders returns only orders (R2)

```gherkin
Given the pre-fix Strategy protocol
Then plan_orders(market, state, cfg) is typed to return list[Order] (orders only)
And there is no channel by which the engine can observe a recomputed V_n
```

### AC-7 — (Corrected) the enriched Strategy contract exposes a namespace-scoped Decimal state delta (R2)

```gherkin
Given the enriched Strategy contract (RECOMMENDED option 1: plan_orders -> PlanResult(orders, state_delta))
Then a strategy makes its recomputed persist-worthy state observable as a Mapping[str, Decimal]
And the contract is a pure typing.Protocol: it prescribes no IO, no clock, ships no implementation
And the state_delta carries Decimal values only (no float on the money path)
And VR, MAB, the backtest engine, and the sweep harness all conform to the SAME single contract
    (no second Strategy / Order / result type is forked)
```

### AC-8 — (Corrected) VRStrategy surfaces the recomputed V_n through the channel (R3)

```gherkin
Given a VR State with seed V_n=Decimal("100.00"), pool, qty and a Market price
When VRStrategy.plan_orders(market, state, cfg) is called
Then it returns the order(s) it already returned before
And it ALSO surfaces a state delta {"V_n": V2} where V2 == next_value(...) for this cycle
And V2 is exactly the value VR used to compute this cycle's rebalance_decision
    (single-pass: next_value is evaluated once; the persisted V_n cannot diverge from the decision's)
And V2 is a Decimal normalized to 2 dp
```

### AC-9 — (Preserved) VR single-cycle orders are identical before and after the fix (R3)

```gherkin
Given identical seed V_n, pool, qty, price and config
When VRStrategy.plan_orders is called for ONE cycle, before and after the fix
Then the order(s) returned are byte-for-byte identical (FD6)
And next_value, rebalance_decision, and order_from_decision are unchanged
```

### AC-10 — (Corrected) MABStrategy conforms with an empty delta (R2)

```gherkin
Given any MABStrategy.plan_orders(market, state, cfg) call (daily or seed-exhausted)
When it is invoked under the enriched contract
Then it returns its orders AND an EMPTY state delta ({})
And MAB is not forced to invent any evolving state to satisfy the contract
```

---

## Cluster 2 — Backtest Engine Multi-Cycle Fidelity (R4)

### AC-11 — (Characterization) current multi-cycle VR keeps V_n stale at the seed (R4)

```gherkin
Given a multi-cycle run_backtest with VRStrategy over N >= 3 cycles
And a seed start_state with V_n = Decimal("100.00")
When the pre-fix engine runs
Then the V_n exposed to the strategy via _build_state is Decimal("100.00") on EVERY cycle
And ledger.v_n is never advanced (documents the bug: same stale seed fed each cycle)
```

### AC-12 — (Corrected) the engine applies the V_n delta so multi-cycle VR evolves faithfully (R4)

```gherkin
Given a multi-cycle run_backtest with the enriched VRStrategy over N >= 3 cycles
And a seed start_state with V_n = Decimal("100.00")
When the engine triggers VR on a cycle and receives {"V_n": V2}
Then it writes V2 into _Ledger.v_n (via _apply_state_delta)
And the V_n exposed to cycle n+1 equals the V2 VR computed in cycle n (faithful evolution)
And after >= 1 advancing cycle the V_n differs from the Decimal("100.00") seed
And the engine never mutates Config or the passed-in State (it writes only its own _Ledger)
```

### AC-13 — (Preserved) MAB backtest result is unchanged by the contract change (R4)

```gherkin
Given an identical MAB run_backtest before and after the contract change
When the engine receives MAB's empty state delta each cycle
Then it applies NOTHING to MAB bookkeeping (avg_price/holdings/seed_remaining/round_idx stay fill-derived)
And the BacktestResult (equity_curve, fills, trades, realized_pnl_usd, total_tax_usd, fingerprint inputs)
    is identical pre- and post-change
```

### AC-14 — (Corrected) the engine stays generic — no VR-specific calls (R4, A8)

```gherkin
Given the RECOMMENDED option 1 (or option 2) is adopted
When the engine advances strategy state
Then it applies an opaque, namespace-scoped Decimal delta to its ledger
And the engine does NOT import or call vr.next_value directly (no engine -> vr internal coupling)
And next_value is evaluated exactly once per VR cycle (inside VRStrategy)
```

---

## Purity / Decimal Invariants (R1–R4)

### AC-15 — float is rejected on the money path (R1, R3)

```gherkin
Given any quarter-sell ref_price or any state-delta V_n value
When a Python float would be introduced (e.g. ref_price=12.34 instead of Decimal("12.34"))
Then it is rejected / normalized via quantize_money (no silent float coercion onto the money path)
And every limit_price and V_n that survives is a Decimal normalized to 2 dp
```

### AC-16 — core and engine read no wall clock and do no IO (R1–R4)

```gherkin
Given mab_on_seed_exhausted, VRStrategy.plan_orders, MABStrategy.plan_orders, and the engine loop
When they run
Then none reads the wall clock (no datetime.now) and none performs network/filesystem IO
And the quarter-sell reference price and all VR inputs arrive via the injected Market / State
```

---

## Design Decision (NOT an acceptance gate — flagged for review)

The `Strategy`-contract **mechanism** (option 1 = richer `plan_orders` return / option 2 = `advance_state`
method / option 3 = engine calls `next_value`) is a design decision documented in `plan.md` with a
**RECOMMENDATION of option 1**. It is **flagged for orchestrator/user review** and is not finalized by
this acceptance suite. The scenarios above are written against the required **outcome** (FD3) and hold for
options 1 and 2; AC-14 additionally rules out the dispreferred option 3 coupling.

## Quality Gates

- Coverage ≥ 85% (`pytest --cov=src/ballast`); touched core/engine target ~100%.
- `mypy --strict src` → 0 errors; `ruff` (lint + format) clean.
- Characterization tests committed before corrected behavior (DDD); no network in the suite.

## Traceability

- `@SPEC:SPEC-STRATEGY-001` → `spec.md`
- `@TEST:SPEC-STRATEGY-001` → `tests/unit/core/{test_mab,test_vr,test_strategy}.py`,
  `tests/unit/backtest/{test_engine,test_engine_paths}.py`
- `@CODE:SPEC-STRATEGY-001` → `src/ballast/core/{strategy,vr,mab}.py`,
  `src/ballast/backtest/engine.py` (+ `sweep.py` type conformance)
