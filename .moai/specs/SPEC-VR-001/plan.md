# Implementation Plan — SPEC-VR-001 (VR Value Rebalancing Core)

`@SPEC:SPEC-VR-001`

This plan covers the VR strategy core only: four pure functions plus one `Strategy`-conforming class
in a single module, `src/ballast/core/vr.py`. It builds on the contracts fixed in SPEC-CORE-001 and
adds no new domain types. MAB, the backtest engine/cost model, adapters, and the scheduler remain in
later SPECs.

## Module Layout

One new module under the existing pure core. It MUST stay pure (no network, no filesystem, no
`datetime.now()` — time/prices arrive via `Market`, state via `State`, knobs via `Config`).

```
src/ballast/core/
├── models.py          # (CORE-001) Order, Decision, Market, State, OrderType, quantize_money
├── strategy.py        # (CORE-001) Strategy Protocol
├── instrument.py      # (CORE-001) InstrumentRegistry.resolve_band (band chain)  [T1]
├── config.py          # (CORE-001) Config schema
└── vr.py              # (THIS SPEC) next_value, rebalance_decision, order_from_decision, VRStrategy
```

Dependency direction (no cycles; `vr.py` is a leaf that consumes CORE-001 only):

```
vr.py ─► models.py
      ─► strategy.py
      ─► instrument.py
      ─► config.py
```

## Function Signatures (target)

```python
from decimal import Decimal
from typing import Literal

from ballast.core.config import Config
from ballast.core.models import Decision, Market, Order, State

TargetMode = Literal["center", "edge"]

def next_value(
    V1: Decimal,
    pool: Decimal,
    E: Decimal,
    G: int,
    flow: Decimal,
    *,
    use_skill: bool = True,
    r: Decimal = Decimal("0"),   # PROVISIONAL placement [T3]; used only when use_skill is False
) -> Decimal: ...

def rebalance_decision(
    E: Decimal,
    V: Decimal,
    min_band: Decimal,
    max_band: Decimal,
    target_mode: TargetMode = "center",   # [T4]
) -> Decision: ...

def order_from_decision(
    decision: Decision,
    price: Decimal,
    holdings: Decimal,
    pool: Decimal,
    *,
    allow_fractional: bool = False,
    ticker: str,
    account_seq: str,
) -> Order | None: ...

class VRStrategy:
    cadence: Literal["daily", "cycle"] = "cycle"
    ns: str = "vr"
    def plan_orders(self, market: Market, state: State, cfg: Config) -> list[Order]: ...
```

Notes on the signatures:

- `next_value` keeps the positional shape `(V1, pool, E, G, flow)` from the SPEC and adds `use_skill`
  and the provisional `r` as keyword-only knobs. `use_skill=True` is the primary path.
- `order_from_decision` needs `ticker` and `account_seq` to build a CORE-001 `Order`; these are
  keyword-only and are supplied by `plan_orders` from `cfg.strategies.vr` (interface decision, since
  the original 6-arg sketch omitted them but an `Order` requires them).
- `VRStrategy` declares `cadence`/`ns` as class attributes so it satisfies the `Strategy` protocol
  structurally (it type-checks under `mypy --strict` and passes `isinstance` since `Strategy` is
  `runtime_checkable`).

## Decimal / sqrt Precision Policy

- All money/qty arithmetic uses `Decimal`. No `float` ever touches the money path — including
  `sqrt(G)`.
- `sqrt(G)` is computed as `Decimal(G).sqrt(context)` under a `decimal.localcontext()` with a high
  working precision (e.g. `prec=50`), NOT `math.sqrt` (which returns `float`). `G` is an `int`, so
  `Decimal(G)` is exact going in.
- Intermediate terms (`pool/G`, the skill correction) are kept at full `Decimal` precision; only the
  final `V2` is quantized — via the CORE-001 `quantize_money` helper — to 2 decimal places using
  `ROUND_HALF_UP`. This avoids compounding double-rounding error across terms.
- `Decision.target_amount`, `Order.qty`, and `Order.limit_price` are all normalized to 2 places at
  CORE-001 construction; `vr.py` relies on those types' `__post_init__` rather than re-implementing
  rounding.
- Whole-share flooring (`allow_fractional=False`) uses `Decimal` floor division / `quantize` with
  `ROUND_FLOOR` against `Decimal(1)`, never `int(float(...))`.

## Dependency on CORE-001 Types

`vr.py` imports and REUSES (never redefines):

- `Decision`, `Order`, `Market`, `State`, `Config` (`models.py`, `config.py`).
- `Side` (BUY/SELL), `DecisionSide` (BUY/SELL/HOLD), `OrderType.RESERVED_LIMIT` (`models.py`).
- `quantize_money` (`models.py`) — the single rounding helper.
- `Strategy` protocol (`strategy.py`) — `VRStrategy` conforms to it.
- `InstrumentRegistry.resolve_band(ticker, explicit)` (`instrument.py`) — resolves `band` via the
  explicit > `default_band` > error chain [T1]. `plan_orders` uses it to obtain leverage-dependent
  bands when the strategy config does not set them explicitly.

## Task Decomposition

Ordered by dependency; priority labels only (no time estimates).

### Primary Goal — `next_value` (skill + basic) [Priority High]

1. Implement the skill formula `V2 = V1 + pool/G + (E - V1)/(2*sqrt(G)) ± flow` with `Decimal` `sqrt`
   under a high-precision context; quantize `V2` to 2 places.
2. Implement the provisional basic formula `V2 = V1*(1 + r) + pool/G ± flow` for `use_skill=False`
   [T3]; mark it provisional in the docstring.
3. Apply `flow` sign last so accumulate (+), withdraw (−), and hold (0) all move `V2` correctly.

### Secondary Goal — `rebalance_decision` (band test + mode) [Priority High]

4. Compute the band edges `V*(1 - min_band)` and `V*(1 + max_band)`; classify `E` into SELL / BUY /
   HOLD.
5. Set `target_amount` by `target_mode` [T4]: `center` → distance to `V`; `edge` → distance to the
   nearest band edge. Return a CORE-001 `Decision` (HOLD ⇒ `target_amount = 0`).

### Tertiary Goal — `order_from_decision` (clamp + floor) [Priority High]

6. Convert `side + target_amount` to `qty = target_amount / price`.
7. Clamp BUY so `qty * price <= pool`; clamp SELL so `qty <= holdings`.
8. Floor to whole shares when `allow_fractional=False`; if `qty` rounds to `0`, return `None`.
   Otherwise build a `reserved_limit` `Order` at `price`.

### Final Goal — `VRStrategy.plan_orders` wiring [Priority High]

9. Read `V_n`, `pool`, and `qty` from `state.data`; read `ticker`, `account_seq`, `G`, `flow`,
   `use_skill`, `target_mode`, `allow_fractional`, and explicit band from `cfg`; resolve band via the
   CORE-001 registry when not explicit [T1].
10. Compute `E = qty * market.current_price`, then `V` (current line from state, or `next_value` per
    the engine's cadence), call `rebalance_decision`, then `order_from_decision`; return the order in
    a list (or `[]` on HOLD/None). Keep it pure (no mutation of `state`/`cfg`).

### Optional Goal — band/gradient grid surface for sweeps [Priority Low]

11. Expose `G` and band as plain parameters so a later sweep SPEC can grid them [T1]; no sweep logic
    in this SPEC.

## Technical Approach

- Pure functions first: the three free functions are deterministic and side-effect-free; `VRStrategy`
  is a thin wiring layer over them.
- One rounding helper: reuse `quantize_money` for every 2-place result; never hand-roll rounding.
- Decimal `sqrt`: a single private helper (e.g. `_dec_sqrt(g: int) -> Decimal`) isolates the
  high-precision context so the formula reads cleanly and the precision policy lives in one place.
- Clamp as explicit min/floor steps, in a fixed order (convert → clamp to capacity → floor →
  zero-check), so each step is independently testable.
- Bands as data, not literals: bands enter as arguments resolved through the CORE-001 chain; `vr.py`
  contains no band numbers [T1].
- Protocol over inheritance: `VRStrategy` satisfies `Strategy` structurally; no base class.

## Risk Analysis

| Risk                                                          | Impact                                              | Mitigation                                                                                                                             |
| ------------------------------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `Decimal.sqrt` precision / context misuse                     | Skill correction off → wrong `V2` → wrong rebalance | Single `_dec_sqrt` helper under `localcontext(prec=50)`; quantize only the final `V2`; property test: skill term sign matches `E - V1` |
| Band resolution from registry ambiguity (explicit vs default) | Wrong band → premature/late rebalance               | Resolve via CORE-001 `resolve_band` chain (explicit > default > error); table-driven tests for explicit, default, and unresolved [T1]  |
| Clamp edge cases (BUY > pool, SELL > holdings, qty rounds 0)  | Over-spend, oversell, or dust orders                | Fixed clamp order with unit tests at each boundary; `qty==0 ⇒ None`; floor with `ROUND_FLOOR` when `allow_fractional=False`            |
| `float` leaking via `sqrt`/division                           | Precision loss on money path                        | `Decimal` everywhere incl. `sqrt(G)`; `mypy --strict` + a test asserting `type(V2) is Decimal` and no `math.sqrt` import in `vr.py`    |
| Quantization / double-rounding drift                          | Off-by-0.01 in `V2`/qty/price                       | Quantize once at the boundary via `quantize_money`; `hypothesis` invariant: quantization is idempotent and yields exactly 2 places     |
| `flow` sign error (accumulate vs withdraw)                    | Target moves the wrong way                          | `flow` applied as a signed add; `hypothesis` invariant: increasing `flow` never decreases `V2` (all else equal)                        |
| Basic-formula `r` placement still TBD [T3]                    | Interface churn when [T3] resolves                  | `use_skill=True` is the default/primary path; basic form is provisional and isolated behind `use_skill=False` + keyword `r`            |
| Accidental IO/clock in `vr.py`                                | Backtest ≠ live divergence                          | `Market`/`State`/`Config` are the only inputs; review + test asserts no `datetime`/IO/`math.sqrt` imports in `core/vr.py`              |

## Test Approach

- Framework: `pytest` + `pytest-cov`; property-based invariants with `hypothesis`.
- Location: `tests/unit/core/test_vr.py` (mirrors the module name).
- Deterministic unit tests (one per acceptance scenario):
  - `next_value` skill path: known inputs → known `V2` (e.g. the worked example → `Decimal("1085.81")`).
  - `next_value` basic path: provisional form → known `V2` [T3].
  - `rebalance_decision`: SELL above band, BUY below band, HOLD inside band; `center` vs `edge`
    `target_amount` magnitudes [T4].
  - `order_from_decision`: BUY clamped to `pool`, SELL clamped to `holdings`, floor to whole shares,
    and `qty==0 ⇒ None`; asserts `order_type == reserved_limit`.
  - `VRStrategy`: conforms to `Strategy`; `cadence == "cycle"`, `ns == "vr"`; `plan_orders` wires the
    three functions and returns `[]` on HOLD.
- Property-based (`hypothesis`) invariants:
  - **HOLD inside band**: for any `E` within `[V*(1-min_band), V*(1+max_band)]`, the decision is HOLD
    with `target_amount == 0`.
  - **Sell raises cash, lowers `E` toward `V`**: applying a SELL order's qty reduces holdings and the
    post-trade valuation moves toward `V` (never overshoots past `V` in `center` mode).
  - **Flow sign correctness**: `next_value` is monotonic non-decreasing in `flow`, all else equal.
  - **Quantization**: every returned `V2`/`qty`/`target_amount` has exactly 2 decimal places and
    re-quantizing is idempotent.
  - **Skill correction sign**: the skill term has the same sign as `(E - V1)`.
- Purity check: a test asserts `vr.py` imports no `datetime`, `math`, network, or filesystem modules.

## Quality Gates (must pass before merge)

- `uv run ruff check .` → 0 errors; `uv run ruff format --check .` → clean.
- `uv run mypy --strict src` → 0 errors.
- `uv run pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% (core targets ~100%).
- No `float` on money/qty paths (incl. `sqrt`); no hardcoded bands/`G`/tickers; no IO/clock in
  `src/ballast/core/vr.py`; VR emits `reserved_limit` only.

## Traceability

- `@SPEC:SPEC-VR-001` → `@TEST:SPEC-VR-001` (`tests/unit/core/test_vr.py`) →
  `@CODE:SPEC-VR-001` (`src/ballast/core/vr.py`) → `@DOC:SPEC-VR-001`.
- Depends on `@SPEC:SPEC-CORE-001` (`src/ballast/core/{models,instrument,config,strategy}.py`).
