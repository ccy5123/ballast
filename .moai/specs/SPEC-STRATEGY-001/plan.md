# SPEC-STRATEGY-001 — Implementation Plan

`@SPEC:SPEC-STRATEGY-001`

> Two pre-live correctness fixes (HANDOFF §4). **DDD ANALYZE-PRESERVE-IMPROVE**: characterization tests
> pin the current (buggy) behavior first, then the corrected behavior is asserted with new assertions.
> Cluster 1 (MAB LOC price) is MAB-local. Cluster 2 (VR `V_n`) changes the shared `Strategy` contract —
> its mechanism has **three candidate options**, presented below with a **RECOMMENDATION**, and is
> **flagged as a design decision for orchestrator/user review** (not unilaterally finalized here).

## Current Behavior (ANALYZE) — what is wrong today

### Cluster 1 — MAB quarter-sell LOC price

`src/ballast/core/mab.py::mab_on_seed_exhausted` returns:

```text
Order(side=SELL, ticker=..., qty=floor(holdings/4), limit_price=None,
      order_type=OrderType.LOC, account_seq=...)
```

`limit_price=None` ⇒ effectively MOC. In backtest `simulate_fill` fills at the bar **close**, so it
works. But via SPEC-ORDER-001 mapping `LOC → (kind=LIMIT, tif=CLS)` and SPEC-ADAPTER-002, a price-less
`LIMIT + timeInForce=CLS` is **not a valid Toss order** (Toss has no MOC) → it surfaces as `FAILED`. The
MAB **daily** LOC BUY legs and the LOC **profit-take** SELL already carry a derived `limit_price` (around
`avg_price`); only the quarter-sell is price-less. So the fix is **MAB-local**.

### Cluster 2 — VR `V_n` multi-cycle evolution

`src/ballast/core/strategy.py::Strategy.plan_orders(market, state, cfg) -> list[Order]` returns **only**
orders. In `src/ballast/backtest/engine.py`:

- `_Ledger.v_n` is **seeded once** from `start_state.data["V_n"]` (`_seed_ledger`, line ~134).
- `_build_state` rebuilds the VR `State` each cycle from `ledger.v_n` (line ~149).
- **`ledger.v_n` is never reassigned anywhere in the engine** (verified: no `ledger.v_n =` write). VR
  recomputes `V2 = next_value(...)` _inside_ `plan_orders` to make the decision, but that `V2` is
  **discarded** (only the order is returned). So every cycle the engine feeds the **same stale seed**
  `V_n` back in → **multi-cycle VR replay is not faithful**. Single-cycle is correct.

MAB is unaffected: the engine derives MAB state (`avg_price`/`holdings`/`seed_remaining`/`round_idx`) from
**fills** (`_apply_buy`/`_apply_sell`), not from a strategy-returned state.

## Module Layout / Touch Points

```
src/ballast/core/strategy.py   # [R2] enrich the Strategy protocol (state-delta channel)
src/ballast/core/vr.py         # [R3] VRStrategy.plan_orders surfaces {"V_n": V2}; next_value UNCHANGED
src/ballast/core/mab.py        # [R1] mab_on_seed_exhausted(+ref_price) priced LOC; MABStrategy empty delta
src/ballast/backtest/engine.py # [R4] capture delta, write ledger.v_n each cycle; MAB path untouched
src/ballast/backtest/sweep.py  # [R2] type-only conformance (passes strategy to run_backtest; no call site)
```

Tests mirror under `tests/unit/`:
`core/test_mab.py`, `core/test_vr.py`, `core/test_strategy.py`, `backtest/test_engine.py`,
`backtest/test_engine_paths.py` (+ `backtest/conftest.py` fixtures).

### Exact touch-point list

| File                             | Change                                                                                                                                               | REQ   |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `src/ballast/core/mab.py`        | `mab_on_seed_exhausted` gains a `ref_price: Decimal` arg; sets `limit_price=quantize_money(ref_price)`. `MABStrategy` passes `market.current_price`. | R1    |
| `src/ballast/core/mab.py`        | `MABStrategy.plan_orders` conforms to enriched contract by returning orders **+ empty delta**.                                                       | R2/R4 |
| `src/ballast/core/strategy.py`   | Enrich `Strategy` (chosen option below). RECOMMENDED: `plan_orders -> PlanResult(orders, state_delta)`.                                              | R2    |
| `src/ballast/core/vr.py`         | `VRStrategy.plan_orders` returns orders **+** `{"V_n": V2}` (the same `V2` it already computed). `next_value` UNCHANGED.                             | R3    |
| `src/ballast/backtest/engine.py` | At the call site (line ~104) capture the delta; add `_apply_state_delta(ledger, delta)` writing `ledger.v_n` for VR keys.                            | R4    |
| `src/ballast/backtest/sweep.py`  | No `plan_orders` call site; only the `strategy: Strategy` type annotation must still satisfy the enriched protocol (type-only).                      | R2    |
| `src/ballast/core/__init__.py`   | Export any new public type (e.g. `PlanResult`) if option 1 introduces one.                                                                           | R2    |

No new third-party deps. No change to `fills.py`/`costs.py`/`types.py` (the quarter-sell still fills at
close because its limit equals the close).

## Cluster 1 — MAB Quarter-Sell LOC Price Rule (REQ-STRATEGY-001-R1)

**Rule (FD1):** the seed-exhausted quarter-sell LOC limit = the injected reference price = the cycle's
current/close price (`Market.current_price`), `quantize_money`-normalized (2 dp).

```text
mab_on_seed_exhausted(holdings, ref_price, *, version, ticker, account_seq) -> Order
    qty   = floor(holdings / 4)            # ROUND_FLOOR, unchanged
    price = quantize_money(ref_price)      # NEW: injected close/current price (2 dp)
    return Order(side=SELL, ticker, qty, limit_price=price, order_type=LOC, account_seq)

MABStrategy.plan_orders(...):
    ...
    if round_idx > n_splits:
        return mab_on_seed_exhausted(holdings, market.current_price, ...), <empty delta>
```

**Why this rule:**

- **Backtest-preserving:** an LOC with `limit == close` fills at the close exactly as the price-less LOC
  did (same fill price, holdings, realized P&L, tax). Characterization test pins the equivalence.
- **Live-valid:** a priced `LIMIT + CLS` is a valid Toss order, so SPEC-ADAPTER-002 produces a real
  submission instead of `FAILED`.
- **Pure / injected:** `ref_price` comes from `Market`, never from a clock or network. No `float`.

**Alternative considered (documented, not chosen):** price at `avg_price`-derived value (like the
profit-take). Rejected — it would change the backtest fill (limit ≠ close ⇒ different fill semantics) and
is less faithful to "sell into the close" than the close price. (Adopting MOC outright is also rejected:
Toss has no MOC, so the task requires a _priced_ LOC.)

## Cluster 2 — `Strategy`-Contract Options (REQ-STRATEGY-001-R2..R4) — DESIGN DECISION FOR REVIEW

> The three HANDOFF §4 candidate forms for letting the engine observe the recomputed `V_n`. **All three
> achieve FD3's outcome.** They are presented with trade-offs; a RECOMMENDATION follows. **This choice is
> flagged for orchestrator/user review and is intentionally NOT finalized in this SPEC.** `spec.md`
> pins the _outcome_ (FD3), not the mechanism.

### Option 1 — Richer `plan_orders` return carrying a state delta (RECOMMENDED)

`plan_orders` returns a small immutable result instead of a bare list:

```text
@dataclass(frozen=True)
class PlanResult:
    orders: tuple[Order, ...]
    state_delta: Mapping[str, Decimal]   # namespace-scoped; VR: {"V_n": V2}; MAB: {}

Strategy.plan_orders(market, state, cfg) -> PlanResult
```

Engine: `result = strategy.plan_orders(...)`; execute `result.orders`; `_apply_state_delta(ledger,
result.state_delta)`.

| Pros                                                                                           | Cons                                                                                         |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **Single-pass / atomic**: orders + `V_n` come from the _same_ snapshot computation (FD4 free). | Changes `plan_orders` **return type** → touches the protocol, VR, MAB, engine, sweep, tests. |
| VR already computes `V2` internally — surfacing it is ~free; no double `next_value`.           | A new public `PlanResult` type to export/maintain.                                           |
| MAB conforms trivially with `state_delta={}`.                                                  | Slightly larger one-time diff than option 3 (but cleaner long-term).                         |
| Engine stays **generic** (opaque `Decimal` delta; no VR-specific calls).                       |                                                                                              |
| Backward-shaped for a future State Store (delta is exactly what you'd persist).                |                                                                                              |

### Option 2 — Separate `advance_state` method on the protocol

Add `advance_state(market, state, cfg) -> Mapping[str, Decimal]`; `plan_orders` keeps returning a list.
Engine calls both each cycle.

| Pros                                                         | Cons                                                                                                                                                                            |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Clean separation of "what to trade" vs "how state evolves".  | **Double computation**: `next_value` runs in `plan_orders` (for the decision) _and_ in `advance_state` (for the delta) → risk of divergence unless refactored (FD4 needs care). |
| `plan_orders` signature unchanged (smaller call-site churn). | Adds a **protocol method** every strategy must implement (MAB needs a no-op `advance_state` returning `{}`).                                                                    |
| Engine still generic (opaque delta).                         | Two call sites + ordering contract (`plan` then `advance`) to keep consistent.                                                                                                  |

### Option 3 — Engine calls `next_value` directly

The engine, on a VR cycle, calls `vr.next_value(...)` itself to advance `ledger.v_n`.

| Pros                             | Cons                                                                                                                                         |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Smallest protocol change (none). | **Breaks the abstraction**: the generic engine hardcodes VR-specific knowledge (which knobs, which state keys, `use_skill`/`r`/`flow`, ...). |
| No new return type.              | Doesn't generalize to any future stateful strategy; couples `engine.py` → `vr.py` internals.                                                 |
|                                  | **Double computation** again (VR computes `V2` inside `plan_orders`; engine recomputes it) with divergence risk.                             |
|                                  | Engine purity/genericity (A8) violated.                                                                                                      |

### RECOMMENDATION

**Adopt Option 1** (richer `plan_orders` return with a namespace-scoped `state_delta`). It is single-pass
(no double `next_value`, satisfies FD4 for free), keeps the engine generic (opaque `Decimal` delta — no
`engine.py → vr.py` coupling), lets MAB conform with an empty delta (FD5), and is the natural shape for a
future State Store. The cost is a one-time return-type change rippled through the protocol + VR + MAB +
engine + tests — enumerated in the touch-point table and mechanical under `mypy --strict`.

**Open question for review:** confirm Option 1 vs Option 2. (Option 3 is dispreferred and only listed for
completeness.) If a future strategy needs to evolve state _without_ emitting orders on the same trigger,
Option 2's separation could be revisited — but no such strategy exists today (VR always evaluates a
decision and its `V2` on the same cycle).

## Engine Delta Application (REQ-STRATEGY-001-R4)

```text
# engine.py inner loop (cycle trigger), under the RECOMMENDED option 1:
result = strategy.plan_orders(market, state, cfg)
for order in result.orders:
    _execute(order, bar, costs, ledger, fills, trades, year_gains)
_apply_state_delta(strategy.ns, ledger, result.state_delta)

def _apply_state_delta(ns, ledger, delta):
    # Generic, namespace-scoped. VR's {"V_n": V2} -> ledger.v_n = quantize_money(V2).
    # MAB's {} -> no-op (state stays fill-derived). Unknown keys are ignored (forward-safe).
    if "V_n" in delta:
        ledger.v_n = quantize_money(delta["V_n"])
```

Invariants: engine never mutates `Config` or the passed-in `State`; it writes only its own `_Ledger`. MAB
result carries an empty delta ⇒ `ledger.v_n`/MAB fields untouched. `next_value` is computed once (in VR).

## Decimal / Purity Policy

- Core stays pure: no IO, no clock (`datetime.now` forbidden), no `float`. `ref_price` and all VR inputs
  are injected via `Market`/`State`. The state delta carries `Decimal` values only.
- Money/quantity is `Decimal` (2 dp) via `quantize_money` at the `Order`/`Market`/ledger boundaries. The
  `next_value` `Decimal.sqrt` path is unchanged. Backtest curve/ratio math may stay `numpy`/`float`
  (curve only; never the money ledger) — unchanged by this SPEC.

## Risk Analysis

| Risk                                     | Description                                                      | Mitigation                                                                                                                                        |
| ---------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backtest result drift (MAB quarter-sell) | Pricing the quarter-sell changes the backtest fill/ledger.       | Limit == close ⇒ LOC fills at close identically; characterization test pins fill price / holdings / realized P&L / tax equal pre- vs post-fix.    |
| Backtest result drift (MAB overall)      | The contract change leaks into MAB state.                        | MAB returns an empty delta; engine applies nothing to MAB fields; characterization test pins MAB `BacktestResult` (curve/fills/trades) unchanged. |
| VR single-cycle order change             | The fix accidentally alters the order VR plans for one cycle.    | `next_value`/`rebalance_decision`/`order_from_decision` untouched; characterization test pins one-cycle orders identical (FD6).                   |
| Double `V_n` computation / divergence    | The persisted `V_n` differs from the `V2` used for the decision. | Option 1 surfaces the _same_ `V2` (single-pass, FD4); options 2/3 explicitly flagged as requiring a shared compute to avoid this.                 |
| Engine couples to VR internals           | Engine hardcodes VR knobs (option 3 hazard).                     | Adopt Option 1/2 (opaque namespace-scoped `Decimal` delta); engine applies, never recomputes; A8 invariant test.                                  |
| Float on money path                      | `ref_price` or `V_n` arrives/leaks as `float`.                   | `quantize_money` at every boundary; `Decimal`-only delta; AC asserts float rejection on the money path.                                           |
| Protocol fork                            | A second `Strategy`/`Order`/result type appears.                 | Single enriched protocol; VR/MAB/engine/sweep all conform under `mypy --strict`; no duplicate types.                                              |
| Sweep harness breakage                   | `sweep.py` stops type-checking after the return-type change.     | `sweep.py` only passes the strategy to `run_backtest` (no `plan_orders` call); update the `Strategy` annotation only; type-only test.             |

## Test Approach (DDD: characterization first, then corrected)

### Cluster 1 — MAB LOC price (`tests/unit/core/test_mab.py`, `tests/unit/backtest/test_engine.py`)

- **Characterization (current):** pin today's `mab_on_seed_exhausted` → `limit_price is None`, `qty ==
floor(holdings/4)`, `order_type == LOC`; and the **backtest** quarter-sell fill at close (fill price,
  holdings, realized P&L, tax). _(These run against the pre-fix code; after the fix the price-None
  assertion is replaced by the priced-LOC assertion, while the backtest fill assertions stay green.)_
- **Corrected (new):** `mab_on_seed_exhausted(holdings, ref_price=close, ...)` →
  `limit_price == quantize_money(close)`, non-`None`; `qty` unchanged. `MABStrategy.plan_orders` on the
  seed-exhausted path passes `market.current_price`. Backtest fill/ledger effect **identical** to the
  characterization values (equivalence assertion). Daily legs / profit-take unchanged.

### Cluster 2 — VR `V_n` multi-cycle (`tests/unit/core/test_vr.py`, `test_strategy.py`, `tests/unit/backtest/test_engine*.py`)

- **Characterization (current):** a multi-cycle `run_backtest` with VR pins that `ledger.v_n` (as exposed
  via `_build_state`) **stays at the seed** across N cycles (documents the bug); and that VR single-cycle
  orders are computed from the seed `V_n`. Pin `Strategy` protocol shape (`plan_orders -> list[Order]`).
- **Corrected (new):**
  - `VRStrategy.plan_orders` returns orders **+** `{"V_n": V2}` where `V2 == next_value(...)` (the same
    value used for the decision); single-cycle orders **identical** to characterization (FD6).
  - `MABStrategy.plan_orders` returns orders **+ empty delta** (conforms; FD5).
  - `run_backtest` over N VR cycles: `V_n` fed into cycle _n+1_ equals VR's `V2` from cycle _n_
    (faithful evolution); assert it **differs** from the seed after ≥1 advancing cycle.
  - MAB `run_backtest` result (curve/fills/trades/realized P&L/tax) **unchanged** vs characterization.
  - Engine generality: with Option 1/2, assert the engine applies an opaque delta and does **not** import/
    call `vr.next_value` (A8).

Inject `Market` (close/`ref_price`), `State`, and `Config` into every test for determinism; no network.

## Milestones (priority-ordered, no time estimates)

- **Primary Goal (Priority High)** — Cluster 1: price the MAB quarter-sell LOC (R1) with characterization
  - corrected-behavior + backtest-equivalence tests. (Self-contained, MAB-local, unblocks the live path.)
- **Secondary Goal (Priority High)** — Decide Option 1/2/3 (orchestrator/user review), then enrich the
  `Strategy` contract (R2) and have `VRStrategy` surface `V_n` (R3); MAB conforms with an empty delta.
- **Final Goal (Priority High)** — Engine observes/applies the `V_n` delta each cycle (R4); multi-cycle VR
  fidelity test green; MAB backtest result proven unchanged.
- **Optional Goal (Priority Low)** — Generalize `_apply_state_delta` for future stateful strategies / a
  State Store; export any new public type cleanly.

## Quality Gates (per Constitution)

- `ruff check .` → 0 errors; `ruff format --check .` clean.
- `mypy --strict src` → 0 errors (the protocol/return-type change must type-check across VR/MAB/engine/sweep).
- `pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% (core/engine are core-like;
  target ~100%).
- DDD ANALYZE-PRESERVE-IMPROVE: characterization tests committed before the corrected behavior; the
  preserved behaviors (MAB unaffected; VR single-cycle orders unchanged) and the intentionally-changed
  behaviors (MAB quarter-sell now priced; multi-cycle VR `V_n` now advances) are each asserted explicitly.
