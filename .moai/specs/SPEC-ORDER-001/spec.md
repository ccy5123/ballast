---
id: SPEC-ORDER-001
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

- Initial draft. Defines the **broker-neutral Order Manager** (P2) for ballast: it converts a CORE
  `ballast.core.models.Order` (`side`/`ticker`/`qty`/`limit_price`/`order_type`/`account_seq`) into a
  broker-neutral `OrderIntent`, derives a **deterministic idempotency key** (`client_order_id`),
  defaults to **dry-run** (record/log the order plan, no submission), and — only when
  `execution.dry_run=False` — submits through a broker-agnostic `BrokerOrderPort` with idempotent
  dedup and submission-result tracking. Safety guards (global kill-switch, `max_position_pct` clamp,
  dry-run gate) wrap every path.
- Scope is intentionally **NO REAL ORDER SUBMISSION**. P2 ships **no write adapter**. The concrete
  Toss write adapter (`POST /api/v1/orders` create / `/modify` / `/cancel`, `BrokerOrderPort`
  implementation) is explicitly deferred to **P3 (SPEC-ADAPTER-002)**. A grounded P3 Toss-mapping note
  is carried forward here for reference only (see "P3 Toss-Mapping Note").
- This SPEC reuses CORE-001 (`Order`, `Config`/`ExecutionConfig`) and ADAPTER-001's read ports
  (`BrokerAccountPort`) for position/buying-power lookups behind the safety clamp. It mirrors the
  ADAPTER-001 port-and-DTO style (`typing.Protocol` ports in `ports.py`, immutable `Decimal`-money
  DTOs in `models.py`).

---

# SPEC-ORDER-001 — Order Manager + dry-run (P2)

`@SPEC:SPEC-ORDER-001`

> P2 is **dry-run-first and broker-neutral**: no real order is submitted, and no broker-specific write
> code lives here. The Toss write adapter is P3 (SPEC-ADAPTER-002). The only grounded Toss facts used
> here are documentation of the **future** mapping (`POST /api/v1/orders`,
> `LIMIT + timeInForce=CLS = LOC`, `clientOrderId` idempotency ~10 min), taken from
> `docs/reference/toss-openapi.json` (토스증권 Open API, OpenAPI 3.1.0, v1.1.5).

## Environment

- Language: Python `>=3.11` (single language; financial values use `Decimal`).
- Packaging/deps: `uv` + `pyproject.toml`. **No new runtime third-party deps** — `pydantic` v2
  (`>=2.6`) is already present and is reused for the DTOs. `httpx`/`respx` are NOT needed in P2 (no
  network).
- Module location: `src/ballast/orders/` — a thin application/orchestration layer at the IO boundary.
  It depends on the CORE-001 contracts/types and on the ADAPTER-001 read ports; the pure core never
  depends on it. The Order Manager itself performs **no network IO and reads no wall clock**: the clock
  (`now`) and any price/position inputs are **injected**.
- Money/quantity: `Decimal` only (never `float`), normalized to 2 decimal places via
  `ballast.core.models.quantize_money`. A bare `float` on the money path is rejected at construction
  (mirroring CORE `Config._reject_float` and ADAPTER `models._reject_float`).
- Idempotency: `client_order_id` is a deterministic string derived from
  (strategy namespace + date/cycle + a stable order signature), constrained to be a valid Toss
  `clientOrderId` (`^[a-zA-Z0-9\-_]+$`, ≤ 36 chars) so P3 can pass it straight through.
- Config: `execution.dry_run` (bool, default ON in shipped configs) and `execution.max_position_pct`
  (`Decimal` ratio) from CORE-001 `ExecutionConfig` drive behavior. The global kill-switch is read
  from config/env at this boundary.
- Tests: `pytest`, `pytest-cov`. The live submission path is exercised entirely through an
  **In-Memory / Recording `BrokerOrderPort`** (no real network). TDD (RED→GREEN→REFACTOR).
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: **dry-run is the default.** Shipped configs set `execution.dry_run=True`. Under dry-run the Order
  Manager records/logs the `OrderIntent`(s) / order plan and returns them; it makes **no** call to any
  port. Live submission only happens when `execution.dry_run=False` is set explicitly.
- A2: **No write adapter exists in P2.** There is no concrete `BrokerOrderPort` implementation that
  talks to a broker in this SPEC. Live submission is validated against an in-memory recording port.
  The real Toss write adapter is P3 (SPEC-ADAPTER-002).
- A3: **Broker-neutral by construction.** `OrderIntent` and `BrokerOrderPort` contain no Toss-specific
  field. The CORE `OrderType`→(order kind, `tif`) mapping is broker-neutral; Toss's `LIMIT+CLS=LOC`
  encoding is applied only by a future P3 adapter, not here.
- A4: **Determinism / idempotency.** For identical inputs (same strategy ns, same date/cycle, same
  order signature) the derived `client_order_id` is byte-for-byte identical across processes and runs —
  it is a pure function of its inputs, with no randomness, UUID, or wall-clock read. Toss treats this
  key as valid for ~10 minutes (P3 concern), so within a cycle a retried submission is deduplicated.
- A5: **Time & prices are injected.** Any "now" used for date/cycle stamping and any current price /
  position / buying-power used by the `max_position_pct` clamp are passed in as arguments (the latter
  sourced via ADAPTER-001 read ports by the caller), never read from the wall clock or network inside
  the manager.
- A6: **Money is `Decimal`, 2 dp.** Every quantity/price/amount is `Decimal`, normalized via
  `quantize_money`. `float` is rejected on the money path.
- A7: **Safety guards are mandatory and ordered.** Every order flows through: (1) global kill-switch,
  (2) `max_position_pct` clamp, (3) dry-run gate, before any (recorded or live) submission. A guard
  that blocks an order prevents submission entirely; a guard that clamps reduces `qty` and continues.
- A8: **Strategy isolation = account.** Orders carry the CORE `account_seq`; the Order Manager is
  account-addressable and holds no global mutable state beyond an in-memory idempotency/submission
  ledger keyed by `client_order_id`.

## Fixed Definitions

These are pinned for P2 and MUST NOT drift silently. Changing any of them requires a HISTORY entry.

- **FD1 — `OrderIntent` is broker-neutral.** `OrderIntent` carries
  `client_order_id`, `account_seq`, `side` (`BUY|SELL`), `ticker`, `qty: Decimal`,
  `kind ∈ {LIMIT, MARKET}`, `tif ∈ {DAY, CLS}`, and `limit_price: Decimal | None`. It contains no
  broker-specific field. It is distinct from CORE `Order` and from ADAPTER `OrderRecord`.
- **FD2 — `OrderType` → (kind, tif) mapping** (the only mapping P2 owns):
  - `reserved_limit` → `kind=LIMIT`, `tif=DAY` (requires a non-null `limit_price`).
  - `LOC` → `kind=LIMIT`, `tif=CLS` (Limit-On-Close; `limit_price` may be `None` ⇒ MOC-style close).
  - `market` → `kind=MARKET`, `tif=DAY` (no `limit_price`; both strategies default to not using it).
- **FD3 — `client_order_id` is deterministic.** It is a pure function
  `derive_client_order_id(ns, cycle_key, signature) -> str` whose output matches `^[a-zA-Z0-9\-_]+$`
  and is ≤ 36 chars (Toss `clientOrderId` constraint, for P3 pass-through). `signature` is a stable
  digest of the order-defining fields (`side|ticker|qty|kind|tif|limit_price|account_seq`). Same inputs
  ⇒ same id; no randomness, no UUID, no wall-clock read.
- **FD4 — dry-run gate is default-ON.** When `execution.dry_run` is true (default), the manager returns
  an `OrderPlan` of recorded `OrderIntent`s and calls **no** port. When false, it submits via
  `BrokerOrderPort` with idempotent dedup.
- **FD5 — `BrokerOrderPort` shape.** `place_order(intent: OrderIntent) -> SubmissionResult` and
  `cancel_order(account_seq: str, client_order_id: str) -> SubmissionResult`. It is a
  `typing.Protocol` (broker-agnostic; structurally satisfied by the in-memory port and, later, by P3's
  Toss adapter). No transport is prescribed.
- **FD6 — `SubmissionResult` shape.** Immutable DTO with `client_order_id`, `status ∈
{RECORDED, SUBMITTED, DUPLICATE, BLOCKED, FAILED}`, optional `broker_order_id: str | None`, and an
  optional `reason` for blocked/failed/clamped outcomes. Money fields (if any) are `Decimal`.
- **FD7 — kill-switch blocks everything.** When the global kill-switch is engaged, **no** order is
  recorded or submitted; every order yields `status=BLOCKED` with a kill-switch reason. The kill-switch
  takes precedence over dry-run (it blocks even recording).
- **FD8 — `max_position_pct` clamp.** Given current position value, buying power / portfolio value, and
  `execution.max_position_pct`, a BUY whose resulting position exceeds the cap is **clamped** (qty
  reduced to the largest value within the cap, re-`quantize_money`'d); if the cap is already met/
  exceeded the order is **blocked** (`status=BLOCKED`, overflow reason). The clamp never increases qty.

## Requirements

The Order Manager MUST satisfy the following EARS requirements (5 modules). All are tagged to
`@SPEC:SPEC-ORDER-001`.

### REQ-ORDER-001-R1 — `OrderIntent` + `BrokerOrderPort` + CORE `Order` → `OrderIntent` Mapping (Ubiquitous)

`@SPEC:SPEC-ORDER-001` `REQ-ORDER-001-R1`

The system **shall always** define, in `src/ballast/orders/`, a broker-neutral `OrderIntent` model and
a broker-agnostic `BrokerOrderPort` `typing.Protocol`, and **shall always** provide a pure mapping from
a CORE `ballast.core.models.Order` to an `OrderIntent`:

- **`OrderIntent`** (immutable DTO; pydantic frozen or frozen dataclass) carries the FD1 fields with
  `qty`/`limit_price` as `Decimal` normalized to 2 dp via `quantize_money`; a bare `float` is rejected.
- **`Tif`** (`StrEnum`: `DAY`, `CLS`) and **`OrderKind`** (`StrEnum`: `LIMIT`, `MARKET`) are defined
  here (broker-neutral; mirroring CORE `OrderType`/`Side` style).
- **`BrokerOrderPort`** (`runtime_checkable typing.Protocol`) prescribes
  `place_order(intent: OrderIntent) -> SubmissionResult` and
  `cancel_order(account_seq: str, client_order_id: str) -> SubmissionResult` (FD5) — no concrete
  transport.
- **Mapping** `order_to_intent(order, *, ns, cycle_key)` **shall** apply FD2
  (`order_type → (kind, tif)`): `reserved_limit→(LIMIT,DAY)`, `LOC→(LIMIT,CLS)`,
  `market→(MARKET,DAY)`; preserve `side`/`ticker`/`qty`/`account_seq`; carry `limit_price` for LIMIT
  kinds (and **shall not** carry a `limit_price` for MARKET); and **shall** set `client_order_id` to
  the deterministic key from R1's `derive_client_order_id` (FD3).
- The system **shall** derive `client_order_id` deterministically from `(ns, cycle_key, signature)`
  where `signature` is a stable digest of `side|ticker|qty|kind|tif|limit_price|account_seq`; the result
  **shall** match `^[a-zA-Z0-9\-_]+$` and be ≤ 36 chars (FD3).
- The system **shall not** introduce any broker-specific field into `OrderIntent` or `BrokerOrderPort`.

### REQ-ORDER-001-R2 — Dry-Run: Default-ON, Records the Order Plan, No Submission (State-driven + Unwanted)

`@SPEC:SPEC-ORDER-001` `REQ-ORDER-001-R2`

The Order Manager lives in `src/ballast/orders/manager.py` and is dry-run-first.

- **While** `execution.dry_run` is true (the default), when the manager is asked to place one or more
  CORE `Order`s, the system **shall** map each to an `OrderIntent`, record/log the resulting
  `OrderPlan` (the ordered list of intents with their guard outcomes), and **return** it with each
  intent's `SubmissionResult.status = RECORDED` (or `BLOCKED` if a guard blocked it).
- **While** `execution.dry_run` is true, the system **shall not** call `BrokerOrderPort.place_order`
  or `cancel_order`, and **shall not** perform any network IO whatsoever.
- The recorded plan/log **shall** include the deterministic `client_order_id`, the mapped
  `kind`/`tif`/`limit_price`, and the applied guard outcome (recorded / clamped-from-X-to-Y / blocked),
  so a dry run is a faithful, inspectable preview of what a live run would submit.
- The system **shall not** mutate any external state under dry-run beyond appending to its in-memory
  plan/ledger.

### REQ-ORDER-001-R3 — Live Submission: via Port, Idempotent Dedup, Result/Status Tracking (Event-driven + State-driven)

`@SPEC:SPEC-ORDER-001` `REQ-ORDER-001-R3`

- **While** `execution.dry_run` is false, **when** the manager places an `OrderIntent` that has passed
  the safety guards, the system **shall** call `BrokerOrderPort.place_order(intent)` and record the
  returned `SubmissionResult` (status + optional `broker_order_id`) in its in-memory submission ledger
  keyed by `client_order_id`.
- **When** an intent with a `client_order_id` already present in the ledger (within the cycle) is
  placed again, the system **shall** treat it as a duplicate: it **shall not** issue a second
  `place_order` for the same key and **shall** return the prior `SubmissionResult` with
  `status=DUPLICATE` (deterministic idempotency, mirroring Toss's `clientOrderId` semantics).
- **When** `place_order` raises or returns a failure, the system **shall** record
  `status=FAILED` with a `reason`, **shall not** silently retry-forever, and **shall** keep the ledger
  consistent (a failed key may be retried explicitly; a succeeded/duplicate key is never re-submitted).
- The system **shall** expose the per-`client_order_id` submission status so a caller (or later
  reconciliation) can query `RECORDED | SUBMITTED | DUPLICATE | BLOCKED | FAILED`.

### REQ-ORDER-001-R4 — Safety Guards: Kill-Switch, `max_position_pct`, Dry-Run Gate (State-driven + Unwanted)

`@SPEC:SPEC-ORDER-001` `REQ-ORDER-001-R4`

The system **shall** route every order through ordered safety guards before any recorded or live
submission, in `src/ballast/orders/guards.py`:

- **While** the global kill-switch is engaged, the system **shall** block every order: no recording, no
  submission; each yields `SubmissionResult(status=BLOCKED, reason="kill-switch")` (FD7). The
  kill-switch takes precedence over dry-run.
- **While** an order would push the resulting position above `execution.max_position_pct` of the
  reference base (buying power / portfolio value, injected), the system **shall** clamp the order's
  `qty` down to the largest value within the cap (re-`quantize_money`'d) and continue, recording the
  clamp in the result `reason`; **if** the cap is already met or exceeded (no headroom), the system
  **shall** block the order (`status=BLOCKED`, overflow reason) (FD8). The clamp **shall never**
  increase `qty`.
- **While** `execution.dry_run` is true, the dry-run gate **shall** force the record-only path of R2
  (no port call), regardless of any other condition except the kill-switch.
- The system **shall not** submit (live) any order that failed the kill-switch or that has no headroom
  under `max_position_pct`, and **shall not** apply a clamp that would round `qty` up.

### REQ-ORDER-001-R5 — In-Memory / Recording `BrokerOrderPort` + Lightweight Submission-Result Tracking (Optional + Event-driven)

`@SPEC:SPEC-ORDER-001` `REQ-ORDER-001-R5`

- The system **shall** provide an in-memory, network-free `RecordingBrokerOrderPort` (in
  `src/ballast/orders/recording.py`) that structurally satisfies `BrokerOrderPort`, records every
  `place_order` / `cancel_order` call (the full `OrderIntent` and a synthetic `broker_order_id`), and
  is the sole vehicle for exercising the live path in tests — with **no real network**.
- **When** `place_order(intent)` is called on the recording port, the system **shall** append the
  intent to its recorded log and return `SubmissionResult(status=SUBMITTED, broker_order_id=...)`;
  **when** the same `client_order_id` is placed again, it **shall** return the prior result with
  `status=DUPLICATE` (so the manager's R3 dedup is exercised against a faithful idempotent stand-in).
- **Where** a test needs to assert submission/result tracking, the recording port and the manager's
  ledger **shall** expose the ordered list of intents and their `SubmissionResult`s for inspection.
- The system **shall not** require any broker credentials, base URL, or network access for R5.

## Specifications

CORE/ADAPTER input → Order Manager step → implementing module.

| Concern (REQ)                                                   | Input (reused)                                                      | Module                            |
| --------------------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------- |
| `OrderIntent`, `Tif`, `OrderKind`, `SubmissionResult` DTOs `R1` | CORE `Order`/`Side`/`OrderType`, `quantize_money`                   | `src/ballast/orders/models.py`    |
| `BrokerOrderPort` (place/cancel) Protocol `R1`                  | n/a (contract)                                                      | `src/ballast/orders/ports.py`     |
| `order_to_intent` + `derive_client_order_id` `R1`               | CORE `Order` (`side/ticker/qty/limit_price/order_type/account_seq`) | `src/ballast/orders/mapping.py`   |
| Dry-run record-only path + `OrderPlan` `R2`                     | `ExecutionConfig.dry_run`                                           | `src/ballast/orders/manager.py`   |
| Live submit + idempotent dedup + ledger `R3`                    | `BrokerOrderPort`, `ExecutionConfig.dry_run=False`                  | `src/ballast/orders/manager.py`   |
| Kill-switch / `max_position_pct` clamp / dry gate `R4`          | `ExecutionConfig.max_position_pct`, injected position/buying-power  | `src/ballast/orders/guards.py`    |
| `RecordingBrokerOrderPort` + result tracking `R5`               | n/a (test/in-memory)                                                | `src/ballast/orders/recording.py` |

### DTOs / types introduced (REQ-ORDER-001-R1)

| Type                      | Kind       | Key fields                                                                                                      |
| ------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------- |
| `Tif` (enum)              | `StrEnum`  | `DAY`, `CLS`                                                                                                    |
| `OrderKind` (enum)        | `StrEnum`  | `LIMIT`, `MARKET`                                                                                               |
| `SubmissionStatus` (enum) | `StrEnum`  | `RECORDED`, `SUBMITTED`, `DUPLICATE`, `BLOCKED`, `FAILED`                                                       |
| `OrderIntent`             | frozen DTO | `client_order_id`, `account_seq`, `side`, `ticker`, `qty: Decimal`, `kind`, `tif`, `limit_price: Decimal\|None` |
| `SubmissionResult`        | frozen DTO | `client_order_id`, `status: SubmissionStatus`, `broker_order_id: str\|None`, `reason: str\|None`                |
| `OrderPlan`               | frozen DTO | `intents: tuple[OrderIntent, ...]`, `results: tuple[SubmissionResult, ...]`                                     |

## Dependencies

- **Depends on SPEC-CORE-001**: reuses `ballast.core.models.{Order, Side, OrderType, quantize_money}`
  and `ballast.core.config.{Config, ExecutionConfig}` (`dry_run`, `max_position_pct`). The mapping and
  guards never re-implement money normalization — they call `quantize_money`.
- **Depends on SPEC-ADAPTER-001 (read ports)**: the `max_position_pct` clamp consumes position
  `quantity` (`get_holdings`) and buying power (`get_buying_power`) obtained by the caller via the
  read-only `BrokerAccountPort`; these are passed into the manager as injected values (the manager
  itself does no network IO).
- **Defers to SPEC-ADAPTER-002 (P3)**: the concrete Toss write adapter that implements
  `BrokerOrderPort` against `POST /api/v1/orders` (+ `/modify`, `/cancel`).
- **No new third-party deps**; `pydantic` v2 is reused.

## Scope

### In scope (broker-neutral, dry-run-first, P2)

- `OrderIntent` model, `Tif`/`OrderKind`/`SubmissionResult`/`OrderPlan` DTOs, `BrokerOrderPort`
  Protocol, the CORE `Order`→`OrderIntent` mapping with deterministic `client_order_id`, the dry-run
  record-only path (default), the live submission path via port with idempotent dedup + status
  tracking, the ordered safety guards (kill-switch / `max_position_pct` clamp / dry-run gate), and an
  in-memory `RecordingBrokerOrderPort`.

### Out of scope (deferred)

The system **shall not** implement any of the following in this SPEC:

- Any **real** broker write call (`POST /api/v1/orders` create / `/modify` / `/cancel`) — that is the
  concrete Toss write adapter, **P3 (SPEC-ADAPTER-002)**.
- Scheduling/cadence triggering, reconciliation against fills, persistent state stores, and
  notifications (separate backlog items).
- Resolving the MAB quarter-sell LOC price / MOC decision and the VR multi-cycle `V_n` evolution
  (HANDOFF §4 follow-ups) — these are strategy-contract concerns, not Order-Manager concerns.

## P3 Toss-Mapping Note (carry-forward; NOT implemented here)

For the future P3 write adapter only, grounded in `docs/reference/toss-openapi.json`
(`POST /api/v1/orders`, `OrderCreateRequest`):

- **LOC** = `orderType=LIMIT` **+** `timeInForce=CLS` ("At the Close"), currently **US-stock only**.
  The broker-neutral `OrderIntent(kind=LIMIT, tif=CLS)` from FD2 maps 1:1 to this Toss encoding.
- `OrderIntent(kind=LIMIT, tif=DAY)` → Toss `orderType=LIMIT`, `timeInForce=DAY` (default), `price`
  required (the `limit_price` string).
- `OrderIntent(kind=MARKET)` → Toss `orderType=MARKET`, no `price`.
- The deterministic `client_order_id` maps to Toss `clientOrderId` (the idempotency key): max 36 chars,
  pattern `^[a-zA-Z0-9\-_]+$`, **valid ~10 minutes** (re-requesting the same `clientOrderId` within the
  window returns the prior order; afterwards it is treated as a new order). FD3 is shaped to satisfy
  these constraints so P3 can pass `client_order_id` straight through.
- This note is carry-forward context, **not** a requirement of ORDER-001; no write code is shipped in
  P2.

## Reality Constraints

- The live path is validated **only** through the in-memory `RecordingBrokerOrderPort` (no network, no
  credentials). A real-broker submission (even a 1-share soak) is a P3 step the user performs locally —
  it is not, and cannot be, an automated gate in this environment.
- Money is `Decimal` (2 dp) end-to-end; `float` is rejected on the money path. The manager reads no
  wall clock and performs no network IO — time and prices/positions are injected.

## Traceability

- `@SPEC:SPEC-ORDER-001` — this document.
- `@TEST:SPEC-ORDER-001` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/orders/`.
- `@CODE:SPEC-ORDER-001` — `src/ballast/orders/{models,ports,mapping,manager,guards,recording}.py`.
- `@DOC:SPEC-ORDER-001` — generated during `/moai:3-sync`.

### Requirement Index

| Requirement ID   | EARS Type                   | Summary                                                                                             |
| ---------------- | --------------------------- | --------------------------------------------------------------------------------------------------- |
| REQ-ORDER-001-R1 | Ubiquitous                  | `OrderIntent` + `BrokerOrderPort` Protocol + CORE `Order`→`OrderIntent` mapping (type→tif, det. id) |
| REQ-ORDER-001-R2 | State-driven + Unwanted     | Dry-run default-ON: records the order plan only, makes no submission, no network IO                 |
| REQ-ORDER-001-R3 | Event-driven + State-driven | Live submit via port: idempotent dedup by `client_order_id`, submission result/status tracking      |
| REQ-ORDER-001-R4 | State-driven + Unwanted     | Safety guards: kill-switch (block-all), `max_position_pct` clamp/block, dry-run gate                |
| REQ-ORDER-001-R5 | Optional + Event-driven     | In-memory `RecordingBrokerOrderPort` + lightweight submission-result tracking (no real network)     |
