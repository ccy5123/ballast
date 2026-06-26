# SPEC-ORDER-001 — Implementation Plan

`@SPEC:SPEC-ORDER-001`

> Broker-neutral Order Manager (P2). **Dry-run is the default; no real order is submitted.**
> The Toss write adapter is deferred to P3 (SPEC-ADAPTER-002). The only Toss facts used here are the
> carry-forward mapping note, grounded in `docs/reference/toss-openapi.json`.

## Module Layout

All code lives under `src/ballast/orders/` — a thin application/orchestration layer at the IO boundary.
It imports CORE-001 types and ADAPTER-001 read ports; the pure core never imports it. The manager reads
no wall clock and does no network IO (clock and prices/positions are injected).

```
src/ballast/orders/
├── __init__.py
├── models.py        # OrderIntent, OrderPlan, SubmissionResult + Tif, OrderKind,
│                    #   SubmissionStatus enums (Decimal money, frozen, float-rejecting)  [R1]
├── ports.py         # BrokerOrderPort (place_order / cancel_order) typing.Protocol      [R1]
├── mapping.py       # order_to_intent(order, *, ns, cycle_key) + derive_client_order_id [R1]
├── guards.py        # kill-switch, max_position_pct clamp/block, dry-run gate           [R4]
├── manager.py       # OrderManager: dry-run record-only path + live submit + ledger     [R2,R3]
└── recording.py     # RecordingBrokerOrderPort (in-memory, network-free)                [R5]
```

Tests mirror the layout under `tests/unit/orders/`:
`test_models.py`, `test_mapping.py`, `test_guards.py`, `test_manager_dryrun.py`,
`test_manager_live.py`, `test_recording.py`.

## Dependencies to Add

| Dependency | Where | Version | Reason                                                                |
| ---------- | ----- | ------- | --------------------------------------------------------------------- |
| _(none)_   | —     | —       | No new third-party runtime/test deps. `pydantic` v2 (`>=2.6`) reused. |

`httpx`/`respx` are **not** needed (no network in P2). `pandas`/`numpy` are not used (Constitution:
backtest-only).

## Types (REQ-ORDER-001-R1)

Broker-neutral enums (mirror CORE `StrEnum` style):

```text
class Tif(StrEnum):            DAY = "DAY";  CLS = "CLS"
class OrderKind(StrEnum):      LIMIT = "LIMIT";  MARKET = "MARKET"
class SubmissionStatus(StrEnum):
    RECORDED  = "RECORDED"   # dry-run preview only
    SUBMITTED = "SUBMITTED"  # live, accepted by port
    DUPLICATE = "DUPLICATE"  # idempotent dedup hit
    BLOCKED   = "BLOCKED"    # guard blocked (kill-switch / no headroom)
    FAILED    = "FAILED"     # port raised / returned failure
```

`OrderIntent` (frozen; `Decimal` money normalized via `quantize_money`; bare `float` rejected like
CORE `Config._reject_float` / ADAPTER `models._reject_float`):

```text
OrderIntent:
    client_order_id: str        # deterministic; ^[a-zA-Z0-9\-_]+$, <= 36 chars
    account_seq: str
    side: Side                  # reuse ballast.core.models.Side (BUY/SELL)
    ticker: str
    qty: Decimal                # quantize_money, 2 dp
    kind: OrderKind             # LIMIT | MARKET
    tif: Tif                    # DAY | CLS
    limit_price: Decimal | None # required for LIMIT, must be None for MARKET
```

`SubmissionResult` (frozen): `client_order_id`, `status: SubmissionStatus`,
`broker_order_id: str | None`, `reason: str | None`.

`OrderPlan` (frozen): `intents: tuple[OrderIntent, ...]`, `results: tuple[SubmissionResult, ...]`
(the inspectable dry-run / live preview).

`BrokerOrderPort` (`runtime_checkable typing.Protocol`, mirrors ADAPTER-001 `ports.py` style):

```text
class BrokerOrderPort(Protocol):
    def place_order(self, intent: OrderIntent) -> SubmissionResult: ...
    def cancel_order(self, account_seq: str, client_order_id: str) -> SubmissionResult: ...
```

## Mapping Table (REQ-ORDER-001-R1, FD2)

`order_to_intent` consumes a CORE `Order` (`side/ticker/qty/limit_price/order_type/account_seq`) and
applies exactly this `OrderType → (kind, tif)` table:

| CORE `OrderType` | `OrderKind` | `Tif` | `limit_price`                       | Notes                                  |
| ---------------- | ----------- | ----- | ----------------------------------- | -------------------------------------- |
| `reserved_limit` | `LIMIT`     | `DAY` | required (carried from CORE order)  | VR reserved limit order                |
| `LOC`            | `LIMIT`     | `CLS` | optional (`None` ⇒ MOC-style close) | MAB Limit-On-Close; US-only at Toss/P3 |
| `market`         | `MARKET`    | `DAY` | dropped (must be `None`)            | both strategies default to not using   |

Invariants enforced in mapping:

- `LIMIT` kinds with a `None` `limit_price` are allowed only for `tif=CLS` (LOC/MOC); `tif=DAY` LIMIT
  requires a non-null `limit_price` (raise a clear `ValueError` otherwise).
- `MARKET` carries no `limit_price` (any incoming value is dropped to `None`).
- `side`/`ticker`/`qty`/`account_seq` pass through unchanged; `qty`/`limit_price` are re-normalized via
  `quantize_money`.

## Idempotency Key Derivation (REQ-ORDER-001-R1, FD3)

`derive_client_order_id(ns: str, cycle_key: str, signature: str) -> str` is a **pure** function:

1. **signature** = a stable digest of the order-defining fields
   `f"{side}|{ticker}|{qty}|{kind}|{tif}|{limit_price}|{account_seq}"`. Use a deterministic hash
   (e.g. `hashlib.sha1`/`sha256` of the UTF-8 bytes, hex-encoded) — `Decimal` is stringified via its
   canonical 2-dp form so equal money values hash equally. **No** randomness, UUID, or `datetime.now`.
2. **cycle_key** = an injected date/cycle stamp (e.g. `"2026-06-26"` for daily, or a VR cycle index) —
   passed in by the caller (clock is injected; the manager never reads the wall clock).
3. Compose `raw = f"{ns}-{cycle_key}-{signature_hex}"`, then **constrain** to the Toss `clientOrderId`
   shape: keep only `[a-zA-Z0-9\-_]`, and truncate the hex tail so the total length is ≤ 36 chars while
   preserving a namespace/cycle prefix for human readability (e.g. `vr-20260626-<12 hex>` or
   `mab-c0007-<...>`). The result MUST match `^[a-zA-Z0-9\-_]+$`.

Determinism guarantees: identical `(ns, cycle_key, order fields)` ⇒ byte-identical id across processes
and runs. This is what makes the R3 dedup and Toss's 10-minute idempotency (P3) reliable. Property
tests assert: same inputs → same id; any field change → different id; output always matches the regex
and length bound.

## Safety-Guard Logic (REQ-ORDER-001-R4)

Guards run in a fixed order before any recording or live submission; implemented in `guards.py` as
pure functions taking injected inputs (no IO):

```text
1. kill_switch_guard(intent, *, engaged: bool) -> GuardOutcome
   engaged is True  -> BLOCK (reason "kill-switch"); precedence over everything (FD7).
   engaged is False -> PASS.

2. position_cap_guard(intent, *, current_position_value, base_value, max_position_pct) -> GuardOutcome
   Only constrains BUYs that increase a position.
   cap_value      = quantize_money(base_value * max_position_pct)
   headroom_value = cap_value - current_position_value
   headroom <= 0  -> BLOCK (reason "max_position_pct overflow").       (FD8)
   order_value    = intent.qty * reference_price  (reference price injected)
   order_value <= headroom -> PASS (unchanged).
   else           -> CLAMP: max_qty = quantize_money(headroom / reference_price)
                     (floor, never rounds up); replace qty; reason "clamped <old>->><new>".
   Clamp never increases qty; a clamp to 0 degrades to BLOCK.

3. dry_run_gate(intent, *, dry_run: bool) -> route
   dry_run True  -> RECORD-ONLY (R2 path); no port call.
   dry_run False -> LIVE (R3 path).
```

`OrderManager.place(orders, *, cfg, dry_run, kill_switch, positions, base_values, prices, ns,
cycle_key, port=None)` threads each CORE `Order` through `order_to_intent` → guard 1 → guard 2 →
guard 3, appending a `SubmissionResult` per order to the `OrderPlan`. The kill-switch short-circuits to
`BLOCKED` (no record/submit); a blocked cap yields `BLOCKED`; a clamped cap continues with reduced qty;
the dry-run gate selects record-only vs live.

## Dry-Run vs Live Path (REQ-ORDER-001-R2 / R3)

- **Dry-run (default, FD4)**: build `OrderIntent`s, run guards, set surviving results to `RECORDED`
  (or `BLOCKED`), **return** the `OrderPlan`. No `port` is touched; `port` may even be `None`.
- **Live (`dry_run=False`)**: for each guard-passing intent, look up `client_order_id` in the in-memory
  **ledger** (`dict[str, SubmissionResult]`):
  - present & terminal-success/duplicate → return prior result as `DUPLICATE` (no second `place_order`).
  - absent → call `port.place_order(intent)`; on success store `SUBMITTED`(+`broker_order_id`); on
    raise/failure store `FAILED` with reason. A `FAILED` key may be retried explicitly; `SUBMITTED`/
    `DUPLICATE` keys are never re-submitted.
- The ledger is the lightweight submission-result tracker queried by `status_of(client_order_id)`.

## Recording Port (REQ-ORDER-001-R5)

`RecordingBrokerOrderPort` (in `recording.py`) structurally satisfies `BrokerOrderPort`, holds an
in-memory `list[OrderIntent]` and its own `dict[str, SubmissionResult]`:

- `place_order(intent)`: if `client_order_id` already recorded → return prior result as `DUPLICATE`;
  else append intent, synthesize a `broker_order_id` (e.g. `rec-<n>`), return `SUBMITTED`.
- `cancel_order(account_seq, client_order_id)`: record the cancel and return a `SubmissionResult`.
- Exposes `recorded_intents` / `results` for test assertions. **No** network, credentials, or base URL.
  This makes the manager's R3 dedup testable against a faithful idempotent stand-in (so both the manager
  ledger and the port dedup are verified).

## Decimal / Purity Policy

- All money/quantity is `Decimal` (2 dp) via `quantize_money`; `float` is rejected on the money path
  (reuse the `_reject_float` BeforeValidator pattern for pydantic DTOs, or a frozen dataclass with
  `quantize_money` in `__post_init__` like CORE `Order`).
- The manager reads **no** wall clock and does **no** network IO. `cycle_key` (time) and
  `positions`/`base_values`/`prices` (sourced via ADAPTER-001 read ports by the caller) are injected.

## Risk Analysis

| Risk                           | Description                                                                      | Mitigation                                                                                                               |
| ------------------------------ | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Non-deterministic id           | Hidden randomness/clock leaks into `client_order_id`, breaking idempotency.      | Pure `derive_client_order_id` over injected inputs; property tests (same in→same id, diff field→diff id); no UUID/now.   |
| Id violates Toss constraint    | Key exceeds 36 chars or contains illegal chars, breaking future P3 pass-through. | Sanitize to `^[a-zA-Z0-9\-_]+$` + truncate to ≤ 36; regex/length property tests.                                         |
| Accidental live submission     | A dry-run path calls the port.                                                   | dry-run gate forces record-only; tests assert `port.place_order` is never called under `dry_run=True`; port may be None. |
| Clamp rounds up                | `max_position_pct` clamp increases qty or overshoots cap.                        | Floor division then `quantize_money`; assert clamped qty ≤ original and resulting value ≤ cap; clamp-to-0 ⇒ BLOCK.       |
| Kill-switch bypass             | An order slips past an engaged kill-switch.                                      | Kill-switch is guard #1 with precedence over dry-run; blocks record+submit; explicit BLOCKED-everything test.            |
| Double submission              | Same `client_order_id` placed twice issues two `place_order`s.                   | Ledger dedup in manager + idempotent recording port; assert exactly one `place_order` per key, second → DUPLICATE.       |
| Float on money path            | A `float` qty/price silently corrupts precision.                                 | `_reject_float` / `quantize_money` everywhere; AC asserts float construction is rejected.                                |
| CORE/Intent conflation         | `OrderIntent` conflated with CORE `Order` or ADAPTER `OrderRecord`.              | Distinct broker-neutral DTO; mypy-strict; mapping is the only bridge; no cross-import of the three `Order` shapes.       |
| Scope creep into write adapter | Tempting to call a real broker in P2.                                            | FD4/Scope forbid real writes; only `RecordingBrokerOrderPort`; Toss write is P3 (SPEC-ADAPTER-002).                      |

## Test Approach (per EARS module, in-memory only)

- **R1 (`test_models.py`, `test_mapping.py`)**: enum values; `OrderIntent` rejects `float`, normalizes
  to 2 dp; mapping table for each `OrderType` (kind/tif/limit_price); LIMIT-DAY without price raises;
  MARKET drops price; `derive_client_order_id` determinism + regex/length property tests.
- **R2 (`test_manager_dryrun.py`)**: `dry_run=True` returns an `OrderPlan` with `RECORDED` results and
  never calls the port (use a spy/`RecordingBrokerOrderPort` and assert zero `place_order` calls); plan
  contains the deterministic id, mapped kind/tif/limit_price, and guard outcome.
- **R3 (`test_manager_live.py`)**: `dry_run=False` calls `place_order` once per new id and records
  `SUBMITTED`+`broker_order_id`; re-placing the same id returns `DUPLICATE` with no second call; a port
  failure records `FAILED` with a reason; `status_of` returns the right status.
- **R4 (`test_guards.py`)**: kill-switch blocks everything (record + live) with precedence over dry-run;
  cap clamp reduces qty (never up) and records the clamp; no-headroom blocks with overflow reason;
  clamp-to-0 degrades to BLOCK.
- **R5 (`test_recording.py`)**: recording port satisfies `BrokerOrderPort` (`runtime_checkable`);
  records intents; idempotent `DUPLICATE` on repeat; exposes recorded intents/results; needs no network.

Inject `cycle_key`, prices, positions, buying-power, kill-switch, and `dry_run` into every test for
determinism.

## Milestones (priority-ordered, no time estimates)

- **Primary Goal (Priority High)**: DTOs + enums + `BrokerOrderPort` (`R1`), the `order_to_intent`
  mapping and deterministic `derive_client_order_id` (`R1`).
- **Secondary Goal (Priority High)**: dry-run record-only manager path returning `OrderPlan` (`R2`) and
  the ordered safety guards (`R4`).
- **Final Goal (Priority Medium)**: live submission path via port with idempotent dedup + ledger
  status tracking (`R3`) and the `RecordingBrokerOrderPort` (`R5`).
- **Optional Goal (Priority Low)**: `cancel_order` path + cancel recording, and richer `OrderPlan`
  inspection helpers.

## Quality Gates (per Constitution)

- `ruff check .` → 0 errors; `ruff format --check .` clean.
- `mypy --strict src` → 0 errors.
- `pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% (this module is core-like;
  target ~100%).
- TDD RED→GREEN→REFACTOR; the live path is exercised only via the in-memory `RecordingBrokerOrderPort`
  (zero real network). No real broker write call is implemented (deferred to P3).
