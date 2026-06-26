# Implementation Plan — SPEC-MAB-001 (MAB 무한매수법 / Infinite Buying Core)

`@SPEC:SPEC-MAB-001`

This plan covers the MAB strategy core only: two pure functions plus one `Strategy`-conforming class
in a single module, `src/ballast/core/mab.py`. It builds on the contracts fixed in SPEC-CORE-001 and
adds no new domain types. It mirrors the order-construction patterns already proven in
`src/ballast/core/vr.py`. VR, the backtest engine/cost model, adapters, and the scheduler remain in
other SPECs.

## Module Layout

One new module under the existing pure core. It MUST stay pure (no network, no filesystem, no
`datetime.now()` — time/prices arrive via `Market`, state via `State`, knobs via `Config`).

```
src/ballast/core/
├── models.py          # (CORE-001) Order, Market, State, OrderType.LOC, Side, quantize_money
├── strategy.py        # (CORE-001) Strategy Protocol
├── instrument.py      # (CORE-001) InstrumentRegistry.resolve_target_pct (target chain)  [T10]
├── config.py          # (CORE-001) Config schema (+ OPTIONAL MAB knobs, see below)
├── vr.py              # (SPEC-VR-001) next_value, rebalance_decision, order_from_decision, VRStrategy
└── mab.py             # (THIS SPEC) mab_daily_orders, mab_on_seed_exhausted, MABStrategy
```

Dependency direction (no cycles; `mab.py` is a leaf that consumes CORE-001 only — it does NOT import
`vr.py`, it only mirrors its construction style):

```
mab.py ─► models.py
       ─► strategy.py
       ─► instrument.py
       ─► config.py
```

## Function Signatures (target)

```python
from decimal import Decimal
from typing import Literal

from ballast.core.config import Config
from ballast.core.models import Market, Order, State

HalftimeRule = Literal["standard"]            # [T9]; extensible later
MABVersion = Literal["v1.0", "v1.1", "v2.0", "v2.1", "v2.2"]   # [T9]

def mab_daily_orders(
    avg_price: Decimal,
    holdings: Decimal,
    seed: Decimal,
    seed_remaining: Decimal,
    round_idx: int,
    n_splits: int,
    target_pct: Decimal,
    alpha: Decimal,
    split_ratio: Decimal,
    *,
    halftime_rule: HalftimeRule = "standard",   # [T9]
    version: MABVersion = "v2.2",               # [T9]
    ticker: str,
    account_seq: str,
) -> list[Order]: ...

def mab_on_seed_exhausted(
    holdings: Decimal,
    *,
    version: MABVersion = "v2.2",               # [T9]
    ticker: str,
    account_seq: str,
) -> Order: ...

class MABStrategy:
    cadence: Literal["daily", "cycle"] = "daily"
    ns: str = "mab"
    def plan_orders(self, market: Market, state: State, cfg: Config) -> list[Order]: ...
```

Notes on the signatures:

- `mab_daily_orders` adds **`seed`** to the prompt's sketch. The per-round budget is `seed / n_splits`,
  which cannot be computed from `seed_remaining` and `n_splits` alone, so `seed` is a required
  argument supplied by `plan_orders` from `cfg`. This mirrors the VR plan's earlier interface
  decision to add `ticker`/`account_seq` that the original sketch omitted but an `Order` requires.
- `ticker` and `account_seq` are keyword-only and supplied by `plan_orders` from `cfg.strategies.mab`,
  because building a CORE-001 `Order` requires them.
- `halftime_rule` and `version` are keyword-only knobs with provisional defaults [T9]; only
  `"standard"` / `"v2.2"` are wired by this SPEC, but the `Literal` is widened to leave room for the
  later variants without an interface break.
- `MABStrategy` declares `cadence`/`ns` as class attributes so it satisfies the `Strategy` protocol
  structurally (type-checks under `mypy --strict`, passes `isinstance` since `Strategy` is
  `runtime_checkable`).

## Decimal / Rounding Policy

- All money/qty arithmetic uses `Decimal`. No `float` ever touches the money path.
- Per-leg share quantity is discretized **toward zero** before the zero-check:
  `qty = (budget_portion / leg_price).quantize(Decimal(1), rounding=ROUND_FLOOR)`. Flooring (never
  rounding up) keeps the seed-remaining cap intact through the `Order`'s own 2-place quantization, so
  cash committed never exceeds `seed_remaining`. This mirrors `vr.py`'s `order_from_decision`.
- `limit_price`s (`avg_price`, `avg_price*(1+alpha)`, `avg_price*(1+target_pct)`) are kept at full
  `Decimal` precision and quantized to 2 places at the `Order` boundary via the CORE-001
  `quantize_money` (which `Order.__post_init__` already applies); `mab.py` relies on the type's
  `__post_init__` rather than re-implementing rounding.
- The quarter-sell quantity is `(holdings / Decimal(4)).quantize(Decimal(1), ROUND_FLOOR)` — floored
  toward zero, no dust.
- `n_splits / 2` for the halftime split uses **integer floor division** (`n_splits // 2`) so the split
  point is deterministic for odd `n_splits`.

## Dependency on CORE-001 Types

`mab.py` imports and REUSES (never redefines):

- `Order`, `Market`, `State`, `Config` (`models.py`, `config.py`).
- `Side` (BUY/SELL), `OrderType.LOC` (`models.py`).
- `quantize_money` (`models.py`) — the single rounding helper (applied at the `Order` boundary).
- `Strategy` protocol (`strategy.py`) — `MABStrategy` conforms to it.
- `InstrumentRegistry.resolve_target_pct(ticker, explicit)` (`instrument.py`) — resolves `target_pct`
  via the explicit > `default_target_pct` > error chain [T10]. `plan_orders` uses it so a 3x TQQQ and
  a 3x SOXL carry different profit targets without any hardcoding.

## StrategyConfig fields to add (OPTIONAL, default-bearing, non-breaking)

For the run phase, `StrategyConfig` (`src/ballast/core/config.py`) gains the following OPTIONAL,
default-bearing fields so existing CORE-001 / VR configs keep loading unchanged (mirrors how the VR
knobs were already added to the same model):

```python
class StrategyConfig(BaseModel):
    ...
    # MAB-specific knobs (SPEC-MAB-001); optional so CORE-001 / VR configs still load.
    seed: Money = Decimal("0")                       # total MAB cycle capital
    n_splits: int = 40                               # parts the seed is split into
    alpha: Money = Decimal("0.05")                   # step-up of the 2nd buy price [T10]
    split_ratio: Money = Decimal("0.5")              # day-budget split A vs B [T10]
    halftime_rule: Literal["standard"] = "standard"  # [T9] provisional
    version: Literal["v1.0", "v1.1", "v2.0", "v2.1", "v2.2"] = "v2.2"  # [T9] provisional
```

`target_pct` already exists on `StrategyConfig` and is resolved through `resolve_target_pct`; MAB
reuses it as the explicit override.

## Task Decomposition

Ordered by dependency; priority labels only (no time estimates).

### Primary Goal — `mab_daily_orders` (two-point LOC buys + LOC profit-take) [Priority High]

1. Compute `budget = min(quantize(seed / n_splits), seed_remaining)`; if `seed_remaining <= 0`, place
   no BUY legs (REQ-MAB-001-R3).
2. Build leg A (near-average): `price_a = avg_price`, `budget_a = budget * split_ratio`,
   `qty_a = floor(budget_a / price_a)`; drop if `qty_a <= 0`.
3. Build leg B (step-up): `price_b = avg_price * (1 + alpha)`, `budget_b = budget * (1 - split_ratio)`,
   `qty_b = floor(budget_b / price_b)`; drop if `qty_b <= 0`.
4. Apply the halftime rule (REQ-MAB-001-R2 [T9]): include leg B only in 전반전
   (`round_idx <= n_splits // 2`); in 후반전 keep leg A only.
5. When `holdings > 0`, append the LOC profit-take SELL of `holdings` at `avg_price * (1 + target_pct)`
   (REQ-MAB-001-R1); independent of phase.
6. Stamp every order `OrderType.LOC`; return the assembled list (possibly empty).

### Secondary Goal — `mab_on_seed_exhausted` (quarter-sell) [Priority High]

7. Return a single LOC SELL of `floor(holdings / 4)` (REQ-MAB-001-R4). Note the v2.x
   quarter-stop-loss variant as provisional [T9]; default behavior is the seed-exhausted quarter-sell.

### Final Goal — `MABStrategy.plan_orders` wiring [Priority High]

8. Read `avg_price` / `holdings` / `seed_remaining` / `round_idx` from `state.data`; read
   `seed` / `n_splits` / `alpha` / `split_ratio` / `halftime_rule` / `version` / `account_seq` /
   `ticker` from `cfg.strategies.mab`; resolve `target_pct` via
   `InstrumentRegistry.from_config(cfg).resolve_target_pct(ticker, mab.target_pct)` [T10].
9. If the seed is exhausted (`round_idx > n_splits`, equivalently no budget remains after the final
   buy), call `mab_on_seed_exhausted` and return that single quarter-sell; otherwise call
   `mab_daily_orders` and return its list. Keep it pure (no mutation of `state`/`cfg`).

### Optional Goal — `alpha` / `split_ratio` / `target_pct` grid surface for sweeps [Priority Low]

10. Expose `alpha`, `split_ratio`, and `target_pct` as plain parameters so a later sweep SPEC can grid
    them [T10]; no sweep logic in this SPEC.

## Technical Approach

- Pure functions first: the two free functions are deterministic and side-effect-free; `MABStrategy`
  is a thin wiring layer over them.
- Mirror `vr.py`: reuse the exact clamp/floor/zero-check/quantize pattern from `order_from_decision`
  for each buy leg and for the quarter-sell, so the two strategy modules read consistently.
- One rounding helper: reuse `quantize_money` (via `Order.__post_init__`) for every 2-place result;
  never hand-roll rounding. Floor share counts with `ROUND_FLOOR` against `Decimal(1)`.
- A single private `_loc_buy_leg(...)` helper builds one BUY leg (price, budget portion → floored qty →
  `Order` or `None`), so the two legs share one code path and one zero-check.
- Halftime as data, not magic: the split point is `n_splits // 2`; the rule is selected by
  `halftime_rule` so the later [T9] variants slot in without rewriting the buy path.
- `target_pct` as data, not a literal: it enters via `resolve_target_pct`; `mab.py` contains no
  per-instrument target numbers [T10].
- Protocol over inheritance: `MABStrategy` satisfies `Strategy` structurally; no base class.

## Risk Analysis

| Risk                                                           | Impact                                                  | Mitigation                                                                                                                                             |
| -------------------------------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Halftime boundary off-by-one at `round_idx == n_splits/2`      | Wrong phase → leg B placed/dropped on the wrong day     | Fix the boundary explicitly: `round_idx <= n_splits // 2` is 전반전 (both legs); unit tests at `round_idx ∈ {N/2, N/2+1}` and odd `n_splits`           |
| `seed_remaining` cap not enforced before split                 | A day commits more cash than the cycle has → over-spend | `budget = min(seed/n_splits, seed_remaining)` BEFORE splitting; floor each leg; `hypothesis` invariant: total buy cash ≤ `seed_remaining`              |
| Buy-leg qty floors to 0 but order still emitted                | Dust/zero LOC orders                                    | Floor toward zero then drop legs with `qty <= 0` (per-leg, independent); test the tiny-budget case → both legs dropped                                 |
| Profit-take emitted while `holdings <= 0`                      | Sell with nothing to sell                               | Guard `holdings > 0` before appending the SELL; test `holdings = 0` → no SELL leg                                                                      |
| `target_pct` hardcoded instead of resolved                     | 3x TQQQ vs 3x SOXL get the same target → wrong exits    | Resolve via `resolve_target_pct` (explicit > default > error) [T10]; test explicit override, instrument default, and the unresolved-error path         |
| `float` leaking via division (`seed/n_splits`, `budget/price`) | Precision loss on the money path                        | `Decimal` everywhere; `mypy --strict` + a test asserting `type(...) is Decimal` and no `math`/`float` use in `mab.py`                                  |
| Quarter-sell rounds up (`holdings/4` not floored)              | Oversell beyond the intended quarter                    | `(holdings / Decimal(4)).quantize(Decimal(1), ROUND_FLOOR)`; test `holdings = 41` → `10.00` (floored), `holdings = 40` → `10.00`                       |
| `version` / `halftime_rule` still TBD [T9]                     | Interface churn when [T9] resolves                      | `"v2.2"` / `"standard"` are the defaults/primary path; variants are isolated behind the `Literal` knobs and noted provisional; primary mechanics fixed |
| Accidental IO/clock in `mab.py`                                | Backtest ≠ live divergence                              | `Market`/`State`/`Config` are the only inputs; review + test asserts no `datetime`/IO imports in `core/mab.py`; MAB emits `LOC` only                   |

## Test Approach

- Framework: `pytest` + `pytest-cov`; property-based invariants with `hypothesis`.
- Location: `tests/unit/core/test_mab.py` (mirrors the module name).
- Deterministic unit tests (one per acceptance scenario):
  - `mab_daily_orders` mid-game day (전반전, holdings>0): two LOC buys at `avg` and `avg*(1+alpha)` +
    one LOC profit-take SELL of `holdings` at `avg*(1+target_pct)`, with concrete numbers.
  - Profit-take price equals `avg * (1 + target_pct)`; second-buy price equals `avg * (1 + alpha)`.
  - Seed-remaining cap: `seed_remaining < seed/n_splits` → budget capped; total buy cash ≤
    `seed_remaining`.
  - `seed_remaining <= 0` → no BUY legs (SELL may remain).
  - 후반전 (`round_idx > n_splits//2`) → leg B dropped, leg A only [T9].
  - `holdings = 0` → no profit-take SELL.
  - Tiny budget → a leg's qty floors to 0 → that leg dropped (no dust).
  - `mab_on_seed_exhausted` → one LOC SELL of `floor(holdings/4)`; `order_type == OrderType.LOC`.
  - `MABStrategy`: conforms to `Strategy`; `cadence == "daily"`, `ns == "mab"`; `plan_orders` wires
    the daily path and the seed-exhausted quarter-sell path; returns `[]` when no orders.
- Property-based (`hypothesis`) invariants:
  - **Never buy beyond `seed_remaining`**: for any inputs, `sum(buy.qty * buy.limit_price)` over the
    BUY legs is `<= seed_remaining` (and `<= seed/n_splits`).
  - **Profit-take only when `holdings > 0`**: a SELL profit-take appears iff `holdings > 0`.
  - **LOC-only**: every returned order has `order_type == OrderType.LOC`.
  - **Quantized / never float**: every `qty`/`limit_price` has exactly 2 decimal places, is a
    `Decimal`, and re-quantizing is idempotent.
  - **Quarter-sell = floor(holdings/4)**: `mab_on_seed_exhausted(holdings).qty == floor(holdings/4)`.
  - **No-dust**: no returned order has `qty == 0`.
- Purity check: a test asserts `mab.py` imports no `datetime`, `math`, network, or filesystem modules.

## Quality Gates (must pass before merge)

- `uv run ruff check .` → 0 errors; `uv run ruff format --check .` → clean.
- `uv run mypy --strict src` → 0 errors.
- `uv run pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% (core targets ~100%).
- No `float` on money/qty paths; no hardcoded `target_pct`/`alpha`/`split_ratio`/tickers; no IO/clock
  in `src/ballast/core/mab.py`; MAB emits `LOC` only.

## Traceability

- `@SPEC:SPEC-MAB-001` → `@TEST:SPEC-MAB-001` (`tests/unit/core/test_mab.py`) →
  `@CODE:SPEC-MAB-001` (`src/ballast/core/mab.py`) → `@DOC:SPEC-MAB-001`.
- Depends on `@SPEC:SPEC-CORE-001` (`src/ballast/core/{models,instrument,config,strategy}.py`).
