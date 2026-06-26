# Acceptance Criteria — SPEC-VR-001 (VR Value Rebalancing Core)

`@SPEC:SPEC-VR-001` `@TEST:SPEC-VR-001`

All scenarios use Given/When/Then. Money/quantity values are `Decimal`; example strings represent
`Decimal` literals (e.g. `"1085.81"` → `Decimal("1085.81")`). Results are quantized to 2 decimal
places via `quantize_money` (`ROUND_HALF_UP`).

## Scenario 1 — Skill formula computes V2 from known inputs (REQ-VR-001-R1)

**Given** the skill path with `use_skill = True`,
**And** inputs `V1 = "1000.00"`, `pool = "200.00"`, `E = "1100.00"`, `G = 10`, `flow = "50.00"`,
**When** `next_value(V1, pool, E, G, flow, use_skill=True)` is evaluated,
**Then** the cash-schedule term `pool/G = "20.00"`,
**And** the skill-correction term `(E - V1) / (2*sqrt(10)) ≈ 100 / 6.32455532 ≈ "15.81"` is **positive**
(price rose above `V1`, so the next target is raised to sell more),
**And** `V2 = 1000 + 20 + 15.8113883... + 50 = "1085.81"` (quantized to 2 places),
**And** `type(V2) is Decimal` (never `float`; `sqrt` is computed with `Decimal` precision).

**And (crash check)** with the same inputs but `E = "900.00"` and `flow = "0.00"`, the skill
correction is **negative** (`(900-1000)/6.3246 ≈ "-15.81"`), giving `V2 = "1004.19"` — the climb is
suppressed (buy less, preserve cash on a crash).

## Scenario 2 — Basic formula (provisional) path when use_skill is False (REQ-VR-001-R1, [T3])

**Given** the basic path with `use_skill = False` and the **provisional** form
`V2 = V1*(1 + r) + pool/G ± flow` [T3],
**And** inputs `V1 = "1000.00"`, `pool = "200.00"`, `G = 10`, `flow = "0.00"`, `r = "0.01"`,
**When** `next_value(V1, pool, E, G, flow, use_skill=False, r="0.01")` is evaluated,
**Then** the skill-correction term is **absent** (it does not appear in the basic formula),
**And** `V2 = 1000*1.01 + 20 + 0 = "1030.00"`,
**And** the test is explicitly marked as exercising the provisional `r` placement [T3], which may
change when [T3] is resolved (the skill formula remains the primary path).

## Scenario 3 — E above the band ⇒ SELL with center target_amount = E − V (REQ-VR-001-R2)

**Given** `E = "1100.00"`, `V = "1000.00"`, `min_band = "0.05"`, `max_band = "0.05"`,
`target_mode = "center"`,
**And** the upper band edge `V*(1 + max_band) = "1050.00"`,
**When** `rebalance_decision(E, V, min_band, max_band, target_mode="center")` is evaluated,
**Then** because `E (1100) > 1050`, the returned `Decision.side` is `SELL`,
**And** in `center` mode `Decision.target_amount = E - V = "100.00"`,
**And (edge mode)** with `target_mode = "edge"`, the same SELL has
`target_amount = E - V*(1 + max_band) = 1100 - 1050 = "50.00"` (rebalance back to the nearest edge) [T4].

## Scenario 4 — E below the band ⇒ BUY (REQ-VR-001-R2)

**Given** `E = "900.00"`, `V = "1000.00"`, `min_band = "0.05"`, `max_band = "0.05"`,
`target_mode = "center"`,
**And** the lower band edge `V*(1 - min_band) = "950.00"`,
**When** `rebalance_decision(...)` is evaluated,
**Then** because `E (900) < 950`, the returned `Decision.side` is `BUY`,
**And** in `center` mode `Decision.target_amount = V - E = "100.00"`,
**And (edge mode)** with `target_mode = "edge"`,
`target_amount = V*(1 - min_band) - E = 950 - 900 = "50.00"` [T4].

## Scenario 5 — E inside the band ⇒ HOLD, no order (REQ-VR-001-R2 / R5)

**Given** `E = "1020.00"`, `V = "1000.00"`, `min_band = "0.05"`, `max_band = "0.05"`,
**And** the band is `[V*(0.95), V*(1.05)] = ["950.00", "1050.00"]`,
**When** `rebalance_decision(...)` is evaluated,
**Then** because `950 <= 1020 <= 1050`, the returned `Decision.side` is `HOLD`,
**And** `Decision.target_amount == "0.00"`,
**And** passing this HOLD decision to `order_from_decision(...)` returns `None` (no order is placed
while `E` is inside the band).

## Scenario 6 — Order clamp: BUY exceeds pool, allow_fractional=False floors qty (REQ-VR-001-R3)

**Given** a BUY `Decision` with `target_amount = "100.00"`,
**And** `price = "50.00"`, `pool = "80.00"` (buying power), `holdings = "10.00"`,
`allow_fractional = False`, `ticker = "TQQQ"`, `account_seq = "0001"`,
**When** `order_from_decision(decision, price, holdings, pool, allow_fractional=False, ticker="TQQQ", account_seq="0001")`
is evaluated,
**Then** the raw qty `target_amount/price = 100/50 = 2` would cost `"100.00"`, which exceeds
`pool = "80.00"`, so the BUY is **clamped to buying power**: max affordable shares
`floor(80/50) = floor(1.6) = 1`,
**And** with `allow_fractional = False` the qty is floored to **`"1.00"`** whole share,
**And** the result is a `reserved_limit` `Order` with `side = BUY`, `qty = "1.00"`,
`limit_price = "50.00"`, `ticker = "TQQQ"`, `account_seq = "0001"`,
**And** `order.order_type == OrderType.RESERVED_LIMIT` (never `LOC` or `market`).

## Scenario 7 — Edge case: qty rounds to 0 ⇒ None (REQ-VR-001-R3 / R5)

**Given** a BUY `Decision` with `target_amount = "20.00"`,
**And** `price = "50.00"`, `pool = "1000.00"`, `holdings = "0.00"`, `allow_fractional = False`,
**When** `order_from_decision(...)` is evaluated,
**Then** the raw qty `20/50 = 0.4`, and flooring to whole shares yields `0`,
**And** because the quantity rounds to `0`, the system returns `None` (it never emits a zero/dust
order),
**And (sell clamp check)** a SELL whose `target_amount/price` exceeds `holdings` is clamped down to
`holdings`, and if `holdings` floors to `0` the result is likewise `None`.

## Scenario 8 — VRStrategy conforms and wires the pipeline (REQ-VR-001-R4)

**Given** a `VRStrategy()` instance,
**When** it is checked against the CORE-001 `Strategy` protocol and invoked,
**Then** `cadence == "cycle"` and `ns == "vr"`, and it satisfies `Strategy` structurally (type-checks
under `mypy --strict`, passes `isinstance` since the protocol is `runtime_checkable`),
**And** `plan_orders(market, state, cfg)` reads `V_n`/`pool`/`qty` from `state` and the
band/`G`/`flow`/`use_skill`/`account_seq`/`ticker` knobs from `cfg`, computes `E = qty * current_price`,
then chains `next_value → rebalance_decision → order_from_decision`,
**And** it returns `[]` when the decision is HOLD (or the order is `None`) and a one-element
`[Order]` list otherwise,
**And** `plan_orders` performs no IO, no clock access, and does not mutate `state` or `cfg`.

---

## Quality-Gate Criteria

A change implementing SPEC-VR-001 is accepted only when all of the following hold:

- **Tested**: `uv run pytest --cov=src/ballast --cov-report=term-missing` passes with coverage
  **≥ 85%** (core domain targets ~100%); each scenario above has a corresponding test, and the
  `hypothesis` invariants (HOLD-inside-band, sell-raises-cash-and-lowers-E-toward-V, flow-sign,
  quantization) hold.
- **Readable**: `uv run ruff check .` reports **0 errors**; naming follows the constitution
  (`snake_case` functions, `PascalCase` types, `*_usd`/`*_qty` unit suffixes where applicable).
- **Unified**: `uv run ruff format --check .` is clean (no formatting diffs).
- **Secured / Type-safe**: `uv run mypy --strict src` reports **0 errors**; no `float` on any
  money/qty path (including `sqrt(G)`); no hardcoded bands/`G`/tickers; no network/filesystem/
  `datetime.now()` inside `src/ballast/core/vr.py`; VR emits `reserved_limit` orders only.
- **Trackable**: `@SPEC:SPEC-VR-001` is linked to `@TEST` / `@CODE` / `@DOC` tags; commits use
  conventional-commit format referencing SPEC-VR-001.

## Definition of Done

- All eight scenarios pass as automated tests under `tests/unit/core/test_vr.py`.
- `src/ballast/core/vr.py` implements `next_value`, `rebalance_decision`, `order_from_decision`, and
  `VRStrategy` per `spec.md`, reusing CORE-001 types without redefining them.
- LSP run-phase gates clean: errors = 0, type errors = 0, lint errors = 0.
- Coverage ≥ 85%; the skill worked example resolves to `V2 = "1085.81"` and the order-clamp example
  resolves to a `reserved_limit` BUY of `"1.00"` share.
- The four TBD items (T1 band widths, T2 cycle length, T3 basic-formula `r`, T4 target_mode) remain
  parameters, not hardcoded values.

## Traceability

- `@SPEC:SPEC-VR-001` → `@TEST:SPEC-VR-001` (this file → `tests/unit/core/test_vr.py`) →
  `@CODE:SPEC-VR-001` (`src/ballast/core/vr.py`) → `@DOC:SPEC-VR-001`.
- Depends on `@SPEC:SPEC-CORE-001` (`src/ballast/core/{models,instrument,config,strategy}.py`).
