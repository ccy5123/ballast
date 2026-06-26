---
id: SPEC-CORE-001
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

- Initial draft. Defines the shared domain layer for ballast: immutable domain types
  (`Order`, `Decision`, `Market`, `State`, `InstrumentMeta`), the `Strategy` protocol,
  the instrument registry with the explicit-then-default resolution chain, instrument
  guardrails (`validate_instrument`), and the pydantic-based `Config` schema.
- Scope intentionally excludes VR/MAB math, backtest engine, broker/market adapters,
  and the scheduler — this SPEC only fixes the contracts those later SPECs implement.

---

# SPEC-CORE-001 — Shared Domain Layer (Foundation)

`@SPEC:SPEC-CORE-001`

## Environment

- Language: Python `>=3.11` (single language).
- Packaging/deps: `uv` + `pyproject.toml`.
- Validation/modeling: `pydantic` v2 (`>=2.6`) OR frozen `dataclasses` for immutable domain types.
- Money/quantity: `Decimal` only. Rounding to 2 decimal places; tick-size respected at the boundary.
- This module lives in `src/ballast/core/` and is PURE: no network, no filesystem, no clock.
  Time and prices are injected as arguments (via the `Market` snapshot).
- Tests: `pytest`, `pytest-cov`, `hypothesis` (property-based for financial invariants).
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: Laoer strategies (VR, MAB) target **leveraged ETFs** that track a broad **index**.
  1x / single-stock / theme instruments break the strategy's behavioral assumptions.
- A2: Each strategy is isolated by a **separate brokerage account**, addressed by `account_seq` (str).
- A3: Strategy state is persisted per namespace `ns` ∈ {`vr`, `mab`} and is passed into pure
  functions as a read-only-to-the-caller `State` value (this SPEC defines the container, not persistence IO).
- A4: Instrument parameters (`leverage`, `underlying`, default `target_pct`/`band`) are first-class
  config values flowing through the instrument registry — never hardcoded.
- A5: Per-strategy `target_pct`/`band` can be overridden in strategy config; otherwise the instrument
  default applies. If neither exists, resolution is an error (no implicit fallback to a magic number).
- A6: `Config` is loaded and validated from YAML before any strategy runs; validation failures are
  surfaced with clear, field-addressable messages.

## Requirements

The domain layer MUST satisfy the following EARS requirements. All are tagged to `@SPEC:SPEC-CORE-001`.

### REQ-CORE-001-R1 — Domain Types (Ubiquitous)

`@SPEC:SPEC-CORE-001` `REQ-CORE-001-R1`

The system **shall always** expose immutable domain types whose money and quantity fields use
`Decimal` (never `float`), rounded to 2 decimal places at construction/normalization:

- **`Order`**: `side` ∈ {`BUY`, `SELL`}; `ticker: str`; `qty: Decimal`; `limit_price: Decimal | None`;
  `order_type` ∈ {`reserved_limit`, `LOC`, `market`}; `account_seq: str`.
- **`Decision`**: `side` ∈ {`BUY`, `SELL`, `HOLD`}; `target_amount: Decimal` — the abstract
  rebalance/trade intent that precedes becoming an `Order`.
- **`Market`**: read-only snapshot for a ticker — `current price`, `fx rate`, and market-hours/holiday
  info — passed into pure functions (the only legitimate source of "now"/price for the core).
- **`State`**: per-strategy persisted-state container addressed by `ns` ∈ {`vr`, `mab`}.
- **`InstrumentMeta`**: `ticker: str`; `leverage: int`; `underlying: str` (index symbol);
  `default_target_pct: Decimal | None`; `default_band: Decimal | None`.

All five types **shall** be immutable (frozen dataclass or pydantic `model_config(frozen=True)`),
and any monetary/quantity value **shall** be normalized to 2 decimal places.

### REQ-CORE-001-R2 — Strategy Protocol (Ubiquitous)

`@SPEC:SPEC-CORE-001` `REQ-CORE-001-R2`

The system **shall always** define a `Strategy` `typing.Protocol` that both VR and MAB engines
implement, fixing the contract for later SPECs:

- attribute `cadence: Literal["daily", "cycle"]`
- attribute `ns: str` (the strategy namespace, e.g. `"vr"` or `"mab"`)
- method `plan_orders(self, market: Market, state: State, cfg: Config) -> list[Order]`

The protocol **shall** be a pure contract: it prescribes no IO and no concrete implementation,
only the shape that conforming strategies satisfy structurally.

### REQ-CORE-001-R3 — Instrument Registry Resolution (Event-driven)

`@SPEC:SPEC-CORE-001` `REQ-CORE-001-R3`

**When** the registry is asked to resolve a ticker, the system **shall** load the `instruments:`
config block and return the matching `InstrumentMeta`. **When** a strategy resolves `target_pct`
or `band` for a ticker, the system **shall** apply this resolution chain in order:

1. explicit strategy config value (if present), then
2. instrument default (`default_target_pct` / `default_band`), then
3. **error** if both are missing (no silent default).

The first non-null source in the chain wins; the chosen value **shall** be deterministic for a
given `Config`.

### REQ-CORE-001-R4 — Instrument Guardrails (State-driven + Optional)

`@SPEC:SPEC-CORE-001` `REQ-CORE-001-R4`

The system **shall** provide `validate_instrument(meta: InstrumentMeta, strict: bool) -> None`.

- **While** `meta.leverage < 2`, the system **shall** emit a warning
  ("Laoer strategies assume leveraged ETFs; 1x / single-stock breaks behavior") and, **while**
  `strict` is true, **shall** block (raise) instead of warning.
- **While** `meta.underlying` is not an index-tracking symbol (single/theme stock), the system
  **shall** emit a warning and, **while** `strict` is true, **shall** block.
- **Where** the caller provides the `strict_instrument` config flag, the system **shall** use it to
  choose block-vs-warn behavior (the `strict` argument is sourced from `common.strict_instrument`).
- **If** `target_pct`/`band` remain unresolved after the REQ-CORE-001-R3 chain, **then** the system
  **shall** raise an error regardless of `strict` (this is a hard data error, not a guardrail warning).

### REQ-CORE-001-R5 — Config Schema & Constraints (Unwanted/Constraint)

`@SPEC:SPEC-CORE-001` `REQ-CORE-001-R5`

The system **shall** define a pydantic `Config` schema, loaded and validated from YAML, with
top-level blocks:

- `instruments`: map of ticker → instrument definition (`leverage`, `underlying`,
  `default_target_pct`, `default_band`).
- `strategies.vr`, `strategies.mab`: per-strategy config (may override `target_pct`/`band`,
  carry `account_seq`).
- `common`: `allow_fractional: bool`, `round_digits: int`, `strict_instrument: bool`.
- `execution`: `broker: str`, `dry_run: bool`, `max_position_pct: Decimal`.

Constraints (prohibitions):

- The system **shall not** use `float` for any money/quantity field — every such field is `Decimal`.
- The system **shall not** hardcode tickers or leverage anywhere in the core; all instrument data
  **shall** flow through the registry/`Config`.
- The system **shall not** read the wall clock, network, or filesystem inside `src/ballast/core/`;
  time and price **shall** arrive only via the `Market` snapshot.
- On invalid YAML/schema, the system **shall** raise a clear validation error naming the offending
  field rather than silently coercing or defaulting.

## Specifications

| Capability                       | Module (planned)                 | Contract                                                                                               |
| -------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Domain types                     | `src/ballast/core/models.py`     | `Order`, `Decision`, `Market`, `State`, `InstrumentMeta` (immutable, `Decimal`)                        |
| Strategy protocol                | `src/ballast/core/strategy.py`   | `Strategy` Protocol with `cadence`, `ns`, `plan_orders(...)`                                           |
| Instrument registry + guardrails | `src/ballast/core/instrument.py` | `resolve(ticker) -> InstrumentMeta`, target/band resolution chain, `validate_instrument(meta, strict)` |
| Config schema                    | `src/ballast/core/config.py`     | pydantic `Config` with `instruments` / `strategies` / `common` / `execution`                           |

### Example Config (reference — TQQQ / SOXL / QLD)

```yaml
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true

execution:
  broker: toss
  dry_run: true
  max_position_pct: "0.95"

instruments:
  TQQQ:
    leverage: 3
    underlying: NDX # Nasdaq-100 index
    default_target_pct: "0.50"
    default_band: "0.15"
  SOXL:
    leverage: 3
    underlying: SOX # PHLX Semiconductor index
    default_target_pct: "0.40"
    default_band: "0.20"
  QLD:
    leverage: 2
    underlying: NDX
    default_target_pct: "0.60"
    default_band: "0.10"

strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
    # target_pct / band omitted -> instrument defaults apply
  mab:
    account_seq: "0002"
    ticker: SOXL
    target_pct: "0.45" # explicit override beats instrument default
```

## Out of Scope (later SPECs)

- VR math: `next_value`, `rebalance_decision`, `order_from_decision`.
- MAB math: `mab_daily_orders`, `mab_on_seed_exhausted`.
- Backtest engine + cost model (fees, 22% capital-gains tax, FX, slippage).
- Broker / market-data adapters (Toss / KIS), order manager, reconciliation, scheduler.

The `Strategy` protocol and the domain types here only define the contracts those modules implement.

## Traceability

- `@SPEC:SPEC-CORE-001` — this document.
- `@TEST:SPEC-CORE-001` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/core/`.
- `@CODE:SPEC-CORE-001` — `src/ballast/core/{models,instrument,config,strategy}.py`.
- `@DOC:SPEC-CORE-001` — generated during `/moai:3-sync`.

### Requirement Index

| Requirement ID  | EARS Type               | Summary                                                                              |
| --------------- | ----------------------- | ------------------------------------------------------------------------------------ |
| REQ-CORE-001-R1 | Ubiquitous              | Immutable `Decimal`-based domain types (Order/Decision/Market/State/InstrumentMeta)  |
| REQ-CORE-001-R2 | Ubiquitous              | `Strategy` Protocol (cadence, ns, plan_orders)                                       |
| REQ-CORE-001-R3 | Event-driven            | Instrument registry + explicit > default > error resolution chain                    |
| REQ-CORE-001-R4 | State-driven + Optional | `validate_instrument` guardrails (leverage<2, non-index, strict block/warn)          |
| REQ-CORE-001-R5 | Unwanted/Constraint     | `Config` schema + prohibitions (no float money, no hardcoded tickers, no IO in core) |
