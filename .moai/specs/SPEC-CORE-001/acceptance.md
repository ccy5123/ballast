# Acceptance Criteria — SPEC-CORE-001 (Shared Domain Layer)

`@SPEC:SPEC-CORE-001` `@TEST:SPEC-CORE-001`

All scenarios use Given/When/Then. Money/quantity values are `Decimal`; example strings represent
`Decimal` literals (e.g. `"0.50"` → `Decimal("0.50")`).

## Scenario 1 — Registry resolves explicit over default (REQ-CORE-001-R3)

**Given** a `Config` whose `instruments.SOXL` defines `default_target_pct: "0.40"` and
`default_band: "0.20"`,
**And** `strategies.mab` explicitly sets `target_pct: "0.45"` for ticker `SOXL`,
**When** the registry resolves `target_pct` and `band` for the MAB strategy on `SOXL`,
**Then** `resolve_target_pct` returns `Decimal("0.45")` (explicit override wins),
**And** `resolve_band` returns `Decimal("0.20")` (falls back to the instrument default),
**And** both returned values are `Decimal`, never `float`.

## Scenario 2 — Missing target_pct/band after the chain raises an error (REQ-CORE-001-R3 / R4)

**Given** a `Config` whose `instruments.TQQQ` block omits both `default_target_pct` and
`default_band`,
**And** `strategies.vr` for ticker `TQQQ` also provides no explicit `target_pct`/`band`,
**When** resolution is attempted for `TQQQ`,
**Then** the system raises a clear error stating the value is unresolved for ticker `TQQQ`,
**And** the error is raised regardless of the `strict_instrument` flag (this is a hard data error,
not a guardrail warning),
**And** no `None` and no implicit magic-number default is returned.

## Scenario 3 — Guardrail: leverage < 2 blocks when strict, warns when not (REQ-CORE-001-R4)

**Given** an `InstrumentMeta` with `leverage = 1` (e.g. a 1x / single-stock instrument),
**When** `validate_instrument(meta, strict=True)` is called,
**Then** the system raises (blocks) with the message
"Laoer strategies assume leveraged ETFs; 1x / single-stock breaks behavior".

**Given** the same `InstrumentMeta` with `leverage = 1`,
**When** `validate_instrument(meta, strict=False)` is called,
**Then** the system emits a warning with the same message,
**And** does NOT raise (it returns `None`).

**And** the `strict` argument used in production is sourced from `common.strict_instrument`.

## Scenario 4 — Order/money uses Decimal rounded to 2 places (REQ-CORE-001-R1 / R5)

**Given** `common.round_digits = 2`,
**When** an `Order` is constructed with `qty = Decimal("3.005")` and
`limit_price = Decimal("42.119")` for `ticker = "QLD"`, `side = BUY`,
`order_type = reserved_limit`, `account_seq = "0001"`,
**Then** `order.qty == Decimal("3.01")` and `order.limit_price == Decimal("42.12")`
(quantized to 2 decimal places via `ROUND_HALF_UP`),
**And** `type(order.qty) is Decimal` and `type(order.limit_price) is Decimal` (never `float`),
**And** quantization is idempotent — re-normalizing an already-2-place value leaves it unchanged.

## Scenario 5 — Edge case: unknown ticker is rejected (REQ-CORE-001-R3 / R5)

**Given** a `Config` whose `instruments` block contains only `TQQQ`, `SOXL`, and `QLD`,
**When** the registry is asked to `resolve("SPY")` (a ticker not in the registry),
**Then** the system raises a clear error naming the unknown ticker `SPY`,
**And** the error message distinguishes "unknown ticker" from "unresolved target/band",
**And** no partial/empty `InstrumentMeta` is returned.

## Scenario 6 — Edge case: non-index underlying blocks under strict (REQ-CORE-001-R4)

**Given** an `InstrumentMeta` with `leverage = 2` but `underlying` set to a single/theme stock
symbol (not an index-tracking symbol),
**When** `validate_instrument(meta, strict=True)` is called,
**Then** the system raises (blocks), recommending against single/theme stocks,
**And** when `strict=False` the system warns instead of raising.

## Scenario 7 — Constraint: invalid Config surfaces a clear validation error (REQ-CORE-001-R5)

**Given** a YAML config where a money field is supplied as a `float` (e.g. `max_position_pct: 0.95`
as a bare float, or an unknown extra key is present),
**When** `Config.load(path)` validates the document,
**Then** the system raises a pydantic validation error that names the offending field/path,
**And** the system does NOT silently coerce a `float` into the money path or accept unknown keys
(`extra="forbid"`).

## Scenario 8 — Strategy protocol conformance (REQ-CORE-001-R2)

**Given** a stub object exposing `cadence = "daily"`, `ns = "vr"`, and a
`plan_orders(self, market, state, cfg) -> list[Order]` method,
**When** it is checked against the `Strategy` protocol,
**Then** it satisfies the `Strategy` contract structurally (type-checks under `mypy --strict`,
and passes `isinstance` if the protocol is `runtime_checkable`),
**And** the core ships no concrete strategy implementation (protocol only).

---

## Quality-Gate Criteria

A change implementing SPEC-CORE-001 is accepted only when all of the following hold:

- **Tested**: `uv run pytest --cov=src/ballast --cov-report=term-missing` passes with
  coverage **≥ 85%** (core domain targets ~100%); each scenario above has a corresponding test.
- **Readable**: `uv run ruff check .` reports **0 errors**; naming follows the constitution
  (`snake_case` functions, `PascalCase` types).
- **Unified**: `uv run ruff format --check .` is clean (no formatting diffs).
- **Secured / Type-safe**: `uv run mypy --strict src` reports **0 errors**; no `float` on any
  money/qty path; no hardcoded tickers or leverage; no network/filesystem/`datetime.now()` inside
  `src/ballast/core/`.
- **Trackable**: `@SPEC:SPEC-CORE-001` is linked to `@TEST` / `@CODE` / `@DOC` tags; commits use
  conventional-commit format referencing SPEC-CORE-001.

## Definition of Done

- All eight scenarios pass as automated tests under `tests/unit/core/`.
- `src/ballast/core/{models,instrument,config,strategy}.py` implement the contracts in `spec.md`.
- LSP run-phase gates clean: errors = 0, type errors = 0, lint errors = 0.
- Coverage ≥ 85% and the example Config (TQQQ/SOXL/QLD) loads, validates, and resolves end-to-end.

## Traceability

- `@SPEC:SPEC-CORE-001` → `@TEST:SPEC-CORE-001` (this file → `tests/unit/core/`) →
  `@CODE:SPEC-CORE-001` (`src/ballast/core/`) → `@DOC:SPEC-CORE-001`.
