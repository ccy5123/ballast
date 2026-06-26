# Implementation Plan — SPEC-CORE-001 (Shared Domain Layer)

`@SPEC:SPEC-CORE-001`

This plan covers the foundation domain layer only. Everything else in the backlog (VR/MAB math,
backtest engine, adapters, scheduler) depends on the contracts defined here, so this SPEC ships
first and is intentionally narrow.

## Module Layout

All modules live under `src/ballast/core/` and MUST stay pure (no network, no filesystem, no
`datetime.now()` — time/prices arrive via `Market`).

```
src/ballast/core/
├── __init__.py        # re-export public domain types + Strategy + validate_instrument
├── models.py          # Order, Decision, Market, State, InstrumentMeta (immutable, Decimal)
├── strategy.py        # Strategy Protocol (cadence, ns, plan_orders)
├── instrument.py      # registry resolution chain + validate_instrument guardrails
└── config.py          # pydantic Config schema (instruments/strategies/common/execution)
```

Dependency direction (no cycles):

```
config.py  ─┐
            ├─►  instrument.py  ─►  models.py
strategy.py ─┘                      ▲
            └──────────────────────-┘
```

- `models.py` depends on nothing in the package (only `decimal`, `enum`, `dataclasses`/`pydantic`, `typing`).
- `instrument.py` consumes `InstrumentMeta` and config values; owns the resolution chain + guardrails.
- `config.py` defines the pydantic schema and produces `InstrumentMeta` instances for the registry.
- `strategy.py` defines the `Strategy` Protocol referencing `Market`, `State`, `Config`, `Order`.

## Dependencies

- `pydantic` v2 (`>=2.6`) — `Config`, `InstrumentMeta`, and (optionally) the domain types.
  - If pydantic models are used for domain types, set `model_config = ConfigDict(frozen=True,
str_strip_whitespace=True)`. Otherwise use `@dataclass(frozen=True, slots=True)`.
- `decimal.Decimal` — the only allowed numeric type for money/quantity. No `float` anywhere on the
  money/qty path.
- `typing` — `Protocol`, `Literal`, `runtime_checkable` (optional) for the `Strategy` contract.
- `PyYAML` (or pydantic-settings YAML loader) — only at the config-loading boundary, not in pure
  math. Loading lives in `config.py`; the loaded result is plain data passed into the core.

Decimal policy:

- `round_digits` (default 2) controls quantization (`Decimal.quantize(Decimal("0.01"),
rounding=ROUND_HALF_UP)`).
- Tick-size handling is applied at construction/normalization of `Order.limit_price`.

## Task Decomposition

Ordered by dependency; priority labels only (no time estimates).

### Primary Goal — Domain types (`models.py`) [Priority High]

1. Define enums: `Side` (BUY/SELL), `DecisionSide` (BUY/SELL/HOLD), `OrderType`
   (reserved_limit/LOC/market).
2. Implement `InstrumentMeta` (immutable): `ticker`, `leverage: int`, `underlying: str`,
   `default_target_pct: Decimal | None`, `default_band: Decimal | None`.
3. Implement `Order`, `Decision`, `Market`, `State` as frozen/immutable types with `Decimal`
   normalization to `round_digits` for all money/qty fields.
4. Centralize a `quantize_money(value, digits)` helper using `ROUND_HALF_UP`.

### Secondary Goal — Config schema (`config.py`) [Priority High]

5. Define pydantic models: `CommonConfig`, `ExecutionConfig`, `InstrumentConfig`,
   `StrategyConfig`, and the top-level `Config` (`instruments`, `strategies.vr`,
   `strategies.mab`, `common`, `execution`).
6. Force `Decimal` coercion (strings in YAML → `Decimal`); reject `float` inputs; `extra="forbid"`
   so unknown keys fail loudly.
7. Implement `Config.load(path)` (YAML → validated `Config`) with clear field-addressable errors.

### Tertiary Goal — Instrument registry + guardrails (`instrument.py`) [Priority High]

8. Build `InstrumentRegistry` from `Config.instruments` → `resolve(ticker) -> InstrumentMeta`
   (raise on unknown ticker).
9. Implement `resolve_target_pct` / `resolve_band` with the explicit > default > error chain
   (REQ-CORE-001-R3).
10. Implement `validate_instrument(meta, strict)` covering leverage<2, non-index underlying, and
    unresolved target/band (REQ-CORE-001-R4). Warnings via `warnings.warn`; blocks via raised
    exceptions when `strict`.

### Final Goal — Strategy protocol + public surface (`strategy.py`, `__init__.py`) [Priority Medium]

11. Define `Strategy` `Protocol` with `cadence: Literal["daily","cycle"]`, `ns: str`,
    `plan_orders(market, state, cfg) -> list[Order]`.
12. Re-export the public surface from `core/__init__.py`.

### Optional Goal — Index registry / underlying classification [Priority Low]

13. A small set/helper of known index symbols (e.g. NDX, SOX) so guardrail R4 can classify
    "index vs single/theme". Kept data-driven (in config or a constant table), not hardcoded
    per-ticker logic.

## Technical Approach

- Immutability first: frozen dataclasses (preferred for the smallest, pure-data types) or pydantic
  `frozen=True`. No setters, no mutation after construction.
- Decimal discipline: every money/qty value is `Decimal`, quantized to `round_digits` on the way in.
  A single helper enforces `ROUND_HALF_UP` so behavior is consistent across types.
- Resolution as data, not branches: the target/band chain is a clear left-to-right "first non-None
  wins, else raise", easy to unit-test and reason about.
- Guardrails separated from data errors: leverage/underlying issues are guardrails (warn or block by
  `strict`); a missing target/band after the chain is a hard error regardless of `strict`.
- Protocol over inheritance: VR/MAB engines satisfy `Strategy` structurally; the core ships no
  concrete strategy.

## Risk Analysis

| Risk                                                                | Impact                                                     | Mitigation                                                                                                                          |
| ------------------------------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `Decimal` rounding drift (half-up vs banker's, double-quantization) | Wrong qty/price → wrong orders                             | Single `quantize_money` helper, fixed `ROUND_HALF_UP`, quantize once at the boundary; property tests assert idempotent quantization |
| `float` leaking in via YAML (e.g. `0.5` not `"0.5"`)                | Precision loss on money fields                             | Require string/Decimal in schema; reject/repr-check `float`; `extra="forbid"`; tests feed both `"0.50"` and reject `0.5` floats     |
| Registry resolution ambiguity (explicit vs default)                 | Strategy uses wrong band/target                            | Deterministic chain (explicit > default > error); table-driven unit tests for all three branches                                    |
| Unknown ticker / partial instrument block                           | Silent misconfig                                           | `resolve(ticker)` raises a clear error; schema validation names the offending field                                                 |
| Guardrail strictness misconfigured                                  | Bad instrument silently traded (warn when it should block) | `strict_instrument` wired from `common`; explicit tests for strict=true (block) and strict=false (warn)                             |
| Accidental IO/clock in core                                         | Backtest ≠ live divergence                                 | `Market` is the only time/price source; review + tests assert no `datetime.now`/IO imports in `core/`                               |
| Tick-size / fractional shares mismatch                              | Rejected or mis-sized orders                               | `allow_fractional` + tick-size applied at `Order` construction; covered by acceptance criteria                                      |

## Test Approach

- Framework: `pytest` + `pytest-cov`; property-based invariants with `hypothesis`.
- Location: `tests/unit/core/` mirroring module names
  (`test_models.py`, `test_config.py`, `test_instrument.py`, `test_strategy.py`).
- Deterministic unit tests:
  - Construct each domain type; assert immutability (mutation raises) and `Decimal` typing.
  - Resolution chain: explicit-wins, default-fallback, both-missing-raises.
  - Guardrails: leverage<2 strict→raise / non-strict→warn; non-index strict→raise; unresolved→error.
  - Config: valid YAML loads; invalid YAML raises field-addressable error; `float` money rejected.
- Property-based (`hypothesis`):
  - Quantization is idempotent: `quantize(quantize(x)) == quantize(x)`.
  - Any constructed money/qty value has exactly `round_digits` decimal places.
  - `resolve_*` never returns `None` (it either returns a value or raises).
- Protocol conformance: a tiny stub strategy (`cadence`, `ns`, `plan_orders`) is recognized as a
  `Strategy` (structural typing / `isinstance` if `runtime_checkable`).

## Quality Gates (must pass before merge)

- `uv run ruff check .` → 0 errors; `uv run ruff format --check .` → clean.
- `uv run mypy --strict src` → 0 errors.
- `uv run pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% (core targets ~100%).
- No `float` on money/qty paths; no hardcoded tickers/leverage; no IO/clock in `src/ballast/core/`.

## Traceability

- `@SPEC:SPEC-CORE-001` → `@TEST:SPEC-CORE-001` (`tests/unit/core/`) →
  `@CODE:SPEC-CORE-001` (`src/ballast/core/{models,instrument,config,strategy}.py`) →
  `@DOC:SPEC-CORE-001`.
