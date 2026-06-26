---
id: SPEC-STATE-001
version: 0.1.0
status: draft
created: 2026-06-26
updated: 2026-06-26
author: ccy5123
priority: high
lifecycle_level: spec-anchored
---

## HISTORY

### v0.1.0 (2026-06-26)

- Initial draft. Defines the **State Store + Reconciliation** layer (P-state) — the **persistence
  foundation for 24/7 autonomous operation**. It is the durable contract between an always-on trading
  **worker** (scheduler → strategy core → Order Manager → Toss adapter) and a (Streamlit)
  control/monitoring **dashboard**, and it is what makes the worker **restart-safe**.
- Two clusters:
  1. **State Store** — a `StateStorePort` `typing.Protocol` (in `src/ballast/state/`) for durable,
     namespaced persistence of (a) per-strategy state (VR `V_n`; MAB `avg_price`/`holdings`/
     `seed_remaining`/`round_idx`), shaped to accept the SPEC-STRATEGY-001 `PlanResult.state_delta`
     channel via **apply-delta** semantics; (b) the order/idempotency ledger
     (`client_order_id → {orderId, status, ts}`) so cancel/cross-process resolution survives a restart
     (resolves the SPEC-ADAPTER-002 follow-up); and (c) a generic config snapshot
     (ticker/account/allocation/dry_run/kill-switch) shared between worker and dashboard. **Single-writer,
     restart-safe** semantics (writer lease + optimistic concurrency + atomic, read-your-writes updates)
     keep a stray second process from double-writing. **Swappable backends** sit behind the port; an
     **in-memory** store always ships for tests.
  2. **Reconciliation** — a routine that reads executed **fills** (via the SPEC-ADAPTER-001 read port)
     for an account/cycle and **updates persisted state**: it marks ledger orders
     SUBMITTED→FILLED/PARTIAL/CANCELED/EXPIRED against broker truth, and updates positions / avg-cost
     (MAB) or marks cycles (VR). The decision core is **pure** (inject fills + current state → new
     state); IO lives at the boundary. Re-running over the same fills **converges** (idempotent; no
     double-counting).
- The two persistent backend options (**SQLite on a volume** vs **managed Postgres**) are laid out in
  `plan.md` with a **RECOMMENDATION**, flagged as a **design decision for orchestrator/user review** —
  this SPEC pins the required port surface and semantics, not the final backend choice.
- Reuses SPEC-CORE-001 (`State`, `Order`, `quantize_money`), SPEC-STRATEGY-001 (`PlanResult.state_delta`
  apply-delta keys), SPEC-ORDER-001 (`client_order_id`, `SubmissionStatus`, `SubmissionResult`), and
  SPEC-ADAPTER-001 (`BrokerAccountPort` fills/order reads). It **forks none** of these types. It mirrors
  the ADAPTER-001 / ORDER-001 port-and-adapter style (`typing.Protocol` port + frozen `Decimal`-money
  DTO + in-memory impl).
- **Out of scope** (later SPECs): the typed Runner/Config entry point, the Scheduler, the Notifier, the
  Streamlit dashboard, and deployment. They are noted as **downstream consumers** in Dependencies; the
  State Store is **shaped to support them** (single-writer worker, restart-safe, swappable backend) but
  ships none of them here.

---

# SPEC-STATE-001 — State Store + Reconciliation (P-state)

`@SPEC:SPEC-STATE-001`

> The persistence foundation for set-and-forget 24/7 operation. The State Store is the **durable shared
> contract** between an always-on worker (the sole writer) and a read/write dashboard. The pure
> reconciliation core stays **pure** (inject fills + persisted state → new state; no clock, no network,
> no `float`); the State Store backends are the **IO boundary** (like adapters). Money/quantity are
> `Decimal` normalized to 2 dp via `quantize_money`; `float` is rejected on the money path. Secrets / DB
> credentials are read from **environment variables only** (mirroring ADAPTER-001 `from_env`) and never
> logged or committed.

## Environment

- Language: Python `>=3.11` (single language; financial values use `Decimal`).
- Packaging/deps: `uv` + `pyproject.toml`. `pydantic` v2 (`>=2.6`) is already present and is reused for
  the persisted DTOs. The **in-memory** store and the **pure reconciliation core** add **no** new
  third-party runtime deps. The chosen persistent backend adds at most ONE dependency, flagged for
  review (SQLite via the stdlib `sqlite3` ⇒ **zero** new deps; managed Postgres ⇒ a driver such as
  `psycopg[binary]`). The final pick is a `plan.md` decision (see "Backend Options").
- Module location: `src/ballast/state/` — the persistence **IO boundary** (Hexagonal port-adapter,
  alongside `src/ballast/adapters/` and `src/ballast/orders/`). It depends on the CORE-001 /
  STRATEGY-001 / ORDER-001 / ADAPTER-001 contracts; the pure core never depends on it. The **pure
  reconciliation core** (`reconcile.py`) reads no wall clock and does no IO — fills, the current
  persisted snapshot, and any "now"/`ts` are **injected**; only the store backends touch IO.
- Money/quantity: `Decimal` only (never `float`), normalized to 2 dp via
  `ballast.core.models.quantize_money`. A bare `float` on the money path is rejected at construction
  (mirroring CORE `Config._reject_float`, ORDER `models._reject_float`, ADAPTER `models._reject_float`).
- Single-writer: exactly one process (the worker) mutates strategy/order state at a time, enforced by a
  **writer lease/lock** plus **optimistic concurrency** (a monotonically increasing `version` /
  revision per namespace). The dashboard reads freely and writes only the generic config snapshot
  through the same single-writer discipline.
- Secrets: any DB credential (e.g. `DATABASE_URL`) is read from **environment variables ONLY** —
  never committed, never written to the repo, never logged or placed in a `repr`. The SQLite file path
  is config/env-driven and contains no secret.
- Tests: `pytest`, `pytest-cov`. Unit tests run entirely against the **in-memory** store (no real DB).
  Any SQLite/Postgres backend is **integration-tested in-process** (a SQLite temp file) or **mocked** —
  **never against a live cloud DB in CI**.
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: **Single always-on writer.** In the target topology exactly one worker process is running and is
  the **sole mutator** of strategy state and the order ledger. The State Store enforces this with a
  writer lease (a process acquires it; a second concurrent acquire fails or waits) plus optimistic
  concurrency on every write. The dashboard is a reader (and a writer of the generic config snapshot
  only).
- A2: **Restart-safe.** The worker can crash/redeploy at any point; on restart it loads the last
  durable snapshot (strategy state + order ledger + config snapshot) and resumes exactly where it left
  off. Persisted strategy state is the live analogue of the backtest engine's `_Ledger` (`V_n`, cash,
  holdings, avg cost, `seed_remaining`, `round_idx`) — what the strategies' `PlanResult` deltas and
  observed fills accumulate.
- A3: **Apply-delta is the write path for strategy state.** The worker advances persisted strategy
  state by applying the SPEC-STRATEGY-001 `PlanResult.state_delta` (a namespace-scoped
  `Mapping[str, Decimal]`) exactly like the backtest engine's `_apply_state_delta` — generically and
  opaquely: known keys (e.g. `V_n`) are applied, unknown keys are ignored, so a future stateful
  strategy is forward-compatible. VR surfaces `{"V_n": V2}`; MAB surfaces an empty delta (its state is
  **fill-derived**, advanced by Reconciliation, not by a strategy delta).
- A4: **Order ledger is the cross-process idempotency record.** The persisted ledger maps
  `client_order_id → {orderId(broker_order_id), status, ts}` (and the originating `account_seq`). This
  is the durable form of the SPEC-ORDER-001 in-memory ledger and the SPEC-ADAPTER-002 in-process
  `client_order_id → orderId` map; persisting it lets `cancel_order` resolve `client_order_id → orderId`
  **after a restart / from another process** (the explicit SPEC-ADAPTER-002 follow-up).
- A5: **Fills are injected for reconciliation.** Executed fills (and order statuses) are read from the
  broker via the SPEC-ADAPTER-001 `BrokerAccountPort` (`list_orders(status=CLOSED)` / `get_order` /
  `get_holdings`) by the caller, then **passed into** the pure reconciliation core together with the
  current persisted snapshot. The reconciliation core itself reads no clock and does no network IO.
- A6: **Idempotent reconciliation.** Re-running reconciliation over the same fills (e.g. after a
  restart, or two overlapping cycles) **converges**: a fill already applied is not double-counted. This
  is achieved by keying applied fills (e.g. by broker fill/order id + filled quantity watermark) and by
  reconciling order-ledger statuses as a monotonic lifecycle (SUBMITTED → PARTIAL → FILLED / CANCELED /
  EXPIRED), never regressing a terminal status.
- A7: **Money is `Decimal`, 2 dp.** Every persisted quantity/price/amount is `Decimal`, normalized via
  `quantize_money`. `float` is rejected on the money path. The store serializes `Decimal` losslessly
  (e.g. as a canonical decimal **string**, never a binary float) and round-trips it back to `Decimal`.
- A8: **Strategy isolation = namespace + account.** Strategy state is addressed by namespace
  (`vr` / `mab`, the CORE `State.ns`); the order ledger and reconciliation are addressed by
  `account_seq` (CORE-001 A2: one account per strategy). The store holds no global mutable state beyond
  its backend handle and the in-memory contents (for the in-memory impl).
- A9: **Backend is swappable and undecided.** The port is backend-agnostic. Two persistent options
  (SQLite-on-volume vs managed Postgres) are enumerated with a recommendation in `plan.md` and flagged
  for orchestrator/user review; an **in-memory** store always ships for tests regardless of the choice.

## Fixed Definitions

These are pinned for P-state and MUST NOT drift silently. Changing any requires a HISTORY entry.

- **FD1 — `StateStorePort` is backend-agnostic.** A `runtime_checkable typing.Protocol` in
  `src/ballast/state/ports.py` prescribing durable, namespaced reads/writes for three record families
  (strategy state, order ledger, config snapshot) plus the single-writer/lease primitives. It prescribes
  **no** concrete transport (in-memory, SQLite, and Postgres impls all satisfy it structurally).
- **FD2 — Strategy-state record.** Per namespace `ns` (`vr` / `mab`), a `Decimal`-valued map plus a
  monotonically increasing `version`. VR persists at least `V_n` (and the live analogues
  `pool`/`qty`); MAB persists `avg_price`/`holdings`/`seed_remaining`/`round_idx`. It is the durable
  form of `ballast.core.models.State.data` and the backtest `_Ledger`. The write path is **apply-delta**
  (FD3), not blind overwrite.
- **FD3 — Apply-delta semantics.** `apply_strategy_delta(ns, delta: Mapping[str, Decimal], *,
expected_version) -> new_version` merges a `PlanResult.state_delta` into the persisted record:
  known keys are set (2-dp `quantize_money`'d), unknown keys are ignored (forward-compatible), the write
  is **atomic** and **read-your-writes**, and it **fails on a version mismatch** (optimistic
  concurrency) so a stale/second writer cannot clobber. This mirrors the engine's `_apply_state_delta`.
- **FD4 — Order-ledger record.** Keyed by `client_order_id`, carrying
  `{account_seq, broker_order_id (orderId) | None, status: SubmissionStatus-extended, ts}`. `status`
  spans the SPEC-ORDER-001 `SubmissionStatus` (`RECORDED|SUBMITTED|DUPLICATE|BLOCKED|FAILED`) **extended**
  for reconciliation with terminal fill states (`PARTIAL|FILLED|CANCELED|EXPIRED`). It is the durable,
  cross-process form of the ORDER-001 in-memory ledger and the ADAPTER-002 `client_order_id → orderId`
  map. `resolve_order_id(client_order_id) -> broker_order_id | None` is the cross-process cancel hook.
- **FD5 — Config-snapshot record.** A **generic** durable key→value snapshot of the worker↔dashboard
  shared settings (ticker, account, per-strategy allocation, `dry_run`, kill-switch). It is deliberately
  **minimal and untyped-here**: the typed Config model is the Runner SPEC's concern. The store persists
  an opaque, JSON-serializable mapping (money values as decimal strings) plus a `version`.
- **FD6 — Single-writer lease + optimistic concurrency.** `acquire_writer_lease(owner, *, ttl) -> Lease`
  grants the sole-writer right; a second concurrent acquire **fails** (or blocks) until release/expiry.
  Every mutating call additionally carries `expected_version` and fails on mismatch (FD3). Together they
  make a stray second process unable to double-write. Reads never need the lease.
- **FD7 — Reconciliation is pure + idempotent.** `reconcile(snapshot, fills, order_statuses) ->
ReconResult` is a **pure** function: given the current persisted snapshot (strategy state + order
  ledger), the executed fills, and the broker order statuses, it returns the **new** snapshot plus the
  set of ledger/state mutations — with **no** clock, **no** IO, **no** `float`. Re-running over the same
  inputs is a **no-op** beyond the first application (idempotent; fills keyed by a stable id + filled-qty
  watermark; ledger statuses advance monotonically and never regress a terminal status).
- **FD8 — In-memory store is the test vehicle.** `InMemoryStateStore` structurally satisfies
  `StateStorePort` with no DB, no network, no credentials, and is the sole vehicle for unit tests. Any
  persistent backend is exercised only by an in-process integration test (a SQLite temp file) or a mock
  — never a live cloud DB in CI. The in-memory and persistent backends MUST be behaviorally identical
  (a shared port-conformance test suite).

## Requirements

The State Store + Reconciliation layer MUST satisfy the following EARS requirements (≤5 modules). All
are tagged to `@SPEC:SPEC-STATE-001`.

### REQ-STATE-001-R1 — `StateStorePort` + Namespaced Strategy-State Persistence with Apply-Delta (Ubiquitous + Event-driven)

`@SPEC:SPEC-STATE-001` `REQ-STATE-001-R1`

The system **shall always** define, in `src/ballast/state/ports.py`, a backend-agnostic
`runtime_checkable typing.Protocol` `StateStorePort` (FD1) and, in `src/ballast/state/models.py`,
immutable persisted DTOs whose money/quantity fields are `Decimal` (never `float`):

- **Strategy-state surface** (FD2):
  - `load_strategy_state(ns: str) -> StrategyStateRecord` — returns the durable `Decimal`-valued map
    (defaulting to an empty record at `version=0` when unseen), shaped as the live analogue of CORE
    `State.data` and the backtest `_Ledger` (VR: `V_n`/`pool`/`qty`; MAB: `avg_price`/`holdings`/
    `seed_remaining`/`round_idx`).
  - `apply_strategy_delta(ns: str, delta: Mapping[str, Decimal], *, expected_version: int) -> int`
    (FD3): **when** the worker has a recomputed `PlanResult.state_delta`, the system **shall** merge it
    into the persisted record — applying known keys (`quantize_money`'d), ignoring unknown keys
    (forward-compatible), **atomically**, **read-your-writes** — and **shall** return the new `version`.
- The `delta` semantics **shall** match the backtest engine's `_apply_state_delta` (`{"V_n": V2}`
  advances VR; an empty delta is a no-op, leaving MAB's fill-derived state untouched) so the live worker
  advances strategy state **exactly** like the backtest engine.
- A persisted strategy-state read after a (simulated) **restart** **shall** return the last applied
  value (durability / restart-safety).
- The system **shall not** persist a `float` on the money path, **shall not** overwrite the whole record
  on an apply (delta-merge only), and **shall not** apply a delta whose `expected_version` does not
  match the current version (it **shall** raise a clear concurrency error — FD6).

### REQ-STATE-001-R2 — Order / Idempotency Ledger Persistence + Cross-Process Cancel Resolution (Event-driven + State-driven)

`@SPEC:SPEC-STATE-001` `REQ-STATE-001-R2`

The system **shall** persist the order/idempotency ledger (FD4) in `src/ballast/state/` so that
idempotency and cancel resolution survive a restart and work across processes:

- **When** an order is recorded/submitted, the system **shall** `upsert_order(client_order_id, *,
account_seq, broker_order_id, status, ts)` durably, keyed by `client_order_id` (the SPEC-ORDER-001
  deterministic idempotency key), storing the broker `orderId`, the lifecycle `status`, and a `ts`.
- **When** `resolve_order_id(client_order_id) -> broker_order_id | None` is called (e.g. by a
  `cancel_order` issued **after a restart or from another process**), the system **shall** return the
  persisted `broker_order_id` if known, or `None` if the key is unknown — directly resolving the
  SPEC-ADAPTER-002 cross-process cancel follow-up (the in-process map alone returns `None` after a
  restart and the cancel fails; the persisted ledger makes it succeed).
- **While** an order key already exists, an `upsert` **shall** advance its `status` along the monotonic
  lifecycle (SUBMITTED → PARTIAL → FILLED / CANCELED / EXPIRED) and **shall not** regress a terminal
  status; a re-`upsert` with the same status is a no-op (idempotent).
- The system **shall** expose `load_order(client_order_id) -> OrderLedgerRecord | None` and a scoped
  listing (e.g. `list_open_orders(account_seq) -> tuple[OrderLedgerRecord, ...]`) so Reconciliation and
  the dashboard can read submitted-but-unresolved orders.
- The system **shall not** invent a `broker_order_id` for an unknown key (no guessing) and **shall not**
  lose the `client_order_id → orderId` mapping across a restart.

### REQ-STATE-001-R3 — Single-Writer / Restart-Safe Semantics (State-driven + Unwanted)

`@SPEC:SPEC-STATE-001` `REQ-STATE-001-R3`

The system **shall** guarantee that exactly one writer mutates strategy/order state at a time and that
all state is durable and consistent across restarts (FD6), in `src/ballast/state/`:

- The system **shall** provide `acquire_writer_lease(owner: str, *, ttl) -> Lease` and
  `release_writer_lease(lease)` (or a context manager). **While** a lease is held by one owner, a second
  concurrent `acquire_writer_lease` **shall** fail (or block until release/expiry) so a stray second
  process **cannot** become a second writer.
- Every mutating call (`apply_strategy_delta`, `upsert_order`, `set_config`) **shall** carry an
  `expected_version` and **shall** fail with a clear concurrency error on a version mismatch (optimistic
  concurrency, FD3) — so even without the lease a stale writer cannot clobber a newer write.
- Writes **shall** be **atomic** (a crash mid-write leaves either the old or the new record, never a
  torn one) and **read-your-writes** (a read immediately after a successful write observes it).
- **While** the worker restarts, a reload of strategy state + order ledger + config snapshot **shall**
  return the last durably committed values (restart-safety).
- The system **shall not** allow two concurrent writers to both succeed, **shall not** expose a
  non-atomic mutating path, and **shall not** require the lease for **reads** (the dashboard reads
  freely).

### REQ-STATE-001-R4 — Reconciliation: Fills → State, Pure + Idempotent (Event-driven + State-driven + Unwanted)

`@SPEC:SPEC-STATE-001` `REQ-STATE-001-R4`

The system **shall** provide a **pure** reconciliation core in `src/ballast/state/reconcile.py` that
turns executed fills + the current persisted snapshot into the next snapshot (FD7):

- `reconcile(snapshot: StateSnapshot, fills: Sequence[FillRecord], order_statuses:
Mapping[str, str]) -> ReconResult` **shall** be a **pure** function (inject fills + current snapshot →
  new snapshot + mutation list; no clock, no network, no `float`).
- **When** a fill for a submitted order is observed, the system **shall** mark the corresponding ledger
  order as `FILLED` (or `PARTIAL` while `filled_qty < ordered_qty`), and **shall** update persisted
  position state: for **MAB**, recompute `avg_price`/`holdings`/`seed_remaining`/`round_idx` from the
  fill (volume-weighted avg cost on a BUY; reduce holdings on a SELL) exactly as the backtest engine's
  `_apply_buy`/`_apply_sell` do; for **VR**, update `qty`/`pool` and mark the cycle reconciled (VR's
  `V_n` itself advances via the R1 apply-delta channel, not from fills).
- **When** a broker order status reports CANCELED/EXPIRED for a submitted-but-unfilled order, the system
  **shall** transition the ledger order to that terminal status (reconciling SUBMITTED against broker
  truth).
- **While** reconciliation is re-run over the same fills (after a restart or overlapping cycles), the
  system **shall** converge: a fill already applied (keyed by a stable fill/order id + a filled-quantity
  watermark) **shall not** be counted twice, and a terminal ledger status **shall not** regress
  (idempotency).
- Position/avg-cost recomputation **shall** use `Decimal` + `quantize_money` only and **shall** match
  the backtest engine's ledger math so live and backtest state evolve identically.
- The system **shall not** double-count a re-observed fill, **shall not** regress a terminal status, and
  **shall not** read a clock or perform IO inside the pure core (fills/statuses/`ts` are injected).

### REQ-STATE-001-R5 — In-Memory Store + Config Snapshot + Decimal / Secrets Constraints (Optional + Ubiquitous + Unwanted)

`@SPEC:SPEC-STATE-001` `REQ-STATE-001-R5`

- The system **shall** provide an in-memory, network-free, credential-free `InMemoryStateStore` (in
  `src/ballast/state/memory.py`) that structurally satisfies `StateStorePort` (FD8) and is the sole
  vehicle for unit tests. It **shall** honor the same single-writer lease, optimistic-concurrency,
  apply-delta, atomic, and read-your-writes semantics as a persistent backend (a **shared
  port-conformance suite** asserts in-memory ↔ backend parity).
- The system **shall** persist a **generic** config-snapshot record (FD5): `set_config(snapshot:
Mapping[str, Any], *, expected_version) -> int` and `get_config() -> ConfigSnapshotRecord`, storing an
  opaque JSON-serializable mapping (ticker / account / allocation / `dry_run` / kill-switch) with money
  values as decimal strings and a `version`. This SPEC **shall not** define the typed Config model (that
  is the Runner SPEC's concern) — it persists only the minimal generic snapshot.
- The system **shall always** serialize `Decimal` losslessly (canonical decimal **string**, never a
  binary `float`) and round-trip it back to `Decimal`; a bare `float` on the money path **shall** be
  rejected at construction.
- The system **shall** read any DB credential (e.g. `DATABASE_URL`) from **environment variables only**
  (mirroring ADAPTER-001 `from_env`), and **shall not** log, `repr`, or commit a secret or connection
  string; the SQLite file path is config/env-driven and carries no secret.
- The system **shall not** require any DB, credential, or network for the in-memory store, and
  **shall not** run any persistent backend against a live cloud DB in CI (SQLite temp file or mock only).

## Specifications

Reused input → State Store / Reconciliation surface → implementing module.

| Concern (REQ)                                                    | Input (reused)                                                                                | Module                                        |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `StateStorePort` Protocol + persisted DTOs `R1,R2,R3,R5`         | CORE `State`/`quantize_money`, ORDER `SubmissionStatus`                                       | `src/ballast/state/ports.py`, `models.py`     |
| Namespaced strategy-state load + apply-delta `R1`                | STRATEGY-001 `PlanResult.state_delta`, backtest `_apply_state_delta` semantics                | `src/ballast/state/ports.py` (+ each backend) |
| Order/idempotency ledger + cross-process cancel resolve `R2`     | ORDER-001 `client_order_id`/`SubmissionResult`, ADAPTER-002 `client_order_id → orderId` map   | `src/ballast/state/ports.py` (+ each backend) |
| Single-writer lease + optimistic concurrency + atomic `R3`       | n/a (store-owned)                                                                             | `src/ballast/state/ports.py` (+ each backend) |
| Pure reconciliation (fills → state, idempotent) `R4`             | ADAPTER-001 `BrokerAccountPort` fills/orders, backtest `_apply_buy`/`_apply_sell` ledger math | `src/ballast/state/reconcile.py`              |
| In-memory store (test vehicle) `R5`                              | n/a (in-memory)                                                                               | `src/ballast/state/memory.py`                 |
| Persistent backend (SQLite **or** Postgres — flagged) `R1-R3,R5` | stdlib `sqlite3` **or** a Postgres driver (decision in `plan.md`)                             | `src/ballast/state/sqlite.py` **or** `pg.py`  |

### DTOs / types introduced (REQ-STATE-001-R1/R2/R4/R5)

| Type                   | Kind       | Key fields                                                                                                |
| ---------------------- | ---------- | --------------------------------------------------------------------------------------------------------- |
| `StrategyStateRecord`  | frozen DTO | `ns: str`, `data: Mapping[str, Decimal]`, `version: int`                                                  |
| `OrderLedgerRecord`    | frozen DTO | `client_order_id: str`, `account_seq: str`, `broker_order_id: str\|None`, `status: OrderState`, `ts: ...` |
| `OrderState` (enum)    | `StrEnum`  | ORDER `SubmissionStatus` ∪ `PARTIAL`, `FILLED`, `CANCELED`, `EXPIRED` (reconciliation lifecycle)          |
| `ConfigSnapshotRecord` | frozen DTO | `data: Mapping[str, Any]` (money as decimal strings), `version: int`                                      |
| `FillRecord`           | frozen DTO | `fill_id: str`, `client_order_id\|order_id`, `side: Side`, `qty: Decimal`, `price: Decimal`, `ts`         |
| `StateSnapshot`        | frozen DTO | `strategy: Mapping[str, StrategyStateRecord]`, `orders: Mapping[str, OrderLedgerRecord]`                  |
| `ReconResult`          | frozen DTO | `snapshot: StateSnapshot`, `mutations: tuple[...]` (applied ledger/state changes, inspectable)            |
| `Lease`                | frozen DTO | `owner: str`, `expires_at`/`ttl` (writer-lease handle; FD6)                                               |
| `StateStorePort`       | Protocol   | load/apply strategy state, upsert/load/resolve orders, get/set config, acquire/release writer lease       |

## Dependencies

- **Depends on SPEC-CORE-001**: reuses `ballast.core.models.{State, Order, Side, quantize_money}`. The
  persisted strategy-state record is the durable form of `State.data` and the backtest `_Ledger`; money
  normalization always calls `quantize_money` (never re-implemented).
- **Depends on SPEC-STRATEGY-001**: the strategy-state write path consumes `PlanResult.state_delta` via
  **apply-delta** semantics identical to the engine's `_apply_state_delta` (VR `{"V_n": V2}`; MAB empty).
- **Depends on SPEC-ORDER-001**: the order ledger keys on the deterministic `client_order_id` and stores
  `SubmissionResult` outcomes; `OrderState` extends `SubmissionStatus` with fill-terminal states.
- **Depends on SPEC-ADAPTER-001 (read ports)**: Reconciliation consumes executed fills / order statuses
  / holdings obtained by the caller via the read-only `BrokerAccountPort`
  (`list_orders(status=CLOSED)` / `get_order` / `get_holdings`); these are **injected** into the pure
  reconciliation core (the store/reconciler do their own IO only at the backend boundary).
- **Resolves the SPEC-ADAPTER-002 follow-up**: the in-process `client_order_id → orderId` map is made
  durable here so cross-process / post-restart `cancel_order` resolution works (R2).
- **Downstream consumers (OUT of scope, later SPECs)** — the State Store is **shaped to support** these
  but ships none of them:
  - **Runner / typed Config entry point** — reads the config snapshot, owns the typed Config model,
    acquires the writer lease, drives the cycle.
  - **Scheduler** — triggers MAB daily / VR cycle; reads/writes strategy state through the store.
  - **Notifier** — reads ledger/state to emit alerts.
  - **Streamlit dashboard** — reads strategy state / ledger / config snapshot and writes the config
    snapshot (single-writer-safe), the worker's read/write peer.
  - **Deployment** — provisions the persistent backend (volume for SQLite, or managed Postgres).
- New third-party deps: **none** for the in-memory store + pure reconciliation; the persistent backend
  adds **at most one** dependency, flagged for review in `plan.md` (SQLite ⇒ stdlib, zero deps;
  Postgres ⇒ a driver).

## Scope

### In scope (persistence foundation, P-state)

- The `StateStorePort` `typing.Protocol`; persisted DTOs (`StrategyStateRecord`, `OrderLedgerRecord`,
  `OrderState`, `ConfigSnapshotRecord`, `FillRecord`, `StateSnapshot`, `ReconResult`, `Lease`).
- Namespaced strategy-state persistence with **apply-delta** (R1); durable order/idempotency ledger with
  **cross-process cancel resolution** (R2); **single-writer lease + optimistic concurrency + atomic /
  read-your-writes** semantics (R3); the **pure, idempotent reconciliation** core (R4); the **in-memory**
  store + generic config snapshot + Decimal/secrets constraints (R5).
- **One** persistent backend behind the port (SQLite **or** Postgres — the choice is a `plan.md`
  decision flagged for review), integration-tested in-process / mocked, never against a live cloud DB.

### Out of scope (deferred to later SPECs)

The system **shall not** implement any of the following in this SPEC:

- The **typed Runner / Config entry point** (this SPEC persists only a generic config snapshot; the
  typed Config model and the cycle driver are the Runner SPEC's concern).
- The **Scheduler** (cadence triggering, DST-aware), the **Notifier** (ntfy/Discord), the **Streamlit
  dashboard**, and **deployment** (volume / managed-DB provisioning, Railway).
- Any **broker write** call (that is SPEC-ADAPTER-002) and any **strategy decision logic** (CORE / VR /
  MAB / STRATEGY-001). Reconciliation only _reads_ fills and _updates persisted state_; it issues no
  orders.
- **Finalizing the persistent backend choice** — both options are presented with a recommendation in
  `plan.md` and explicitly flagged for orchestrator/user review.

## Backend Options (decision deferred to `plan.md` — flagged for review)

Two persistent backends sit behind `StateStorePort`; an **in-memory** store always ships for tests
regardless. The trade-offs and a **RECOMMENDATION** (for the 24/7 single-worker-on-Railway + Streamlit
topology) are in `plan.md` and are a **design decision for orchestrator/user review** — this SPEC pins
the port + semantics, not the final pick:

1. **SQLite on a persistent volume** — single-worker, simplest, zero new deps (stdlib `sqlite3`, WAL
   mode), co-located dashboard reads the same file/WAL.
2. **Managed Postgres** (Neon / Supabase — repo has `moai-platform-neon` / `moai-platform-supabase`
   skills) — networked, better concurrency, suits a worker + remotely-hosted dashboard; adds a driver
   dep and `DATABASE_URL` (env-only).

## Reality Constraints

- Unit tests run entirely against the **in-memory** store (no real DB, no network, no credentials). Any
  persistent backend is exercised **in-process** (a SQLite temp file) or **mocked** — there is **no live
  cloud DB in CI**, and a real managed-Postgres soak is a step the user performs locally with their own
  `DATABASE_URL`.
- The pure reconciliation core reads **no** wall clock and does **no** IO — fills, order statuses, the
  current snapshot, and any `ts` are injected; only the store backends touch IO. Money is `Decimal`
  (2 dp) end-to-end; `float` is rejected on the money path. Secrets / DB credentials are env-only and
  never logged or committed.

## Traceability

- `@SPEC:SPEC-STATE-001` — this document.
- `@TEST:SPEC-STATE-001` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/state/`.
- `@CODE:SPEC-STATE-001` — `src/ballast/state/{ports,models,memory,reconcile}.py` and the chosen
  persistent backend (`sqlite.py` **or** `pg.py`).
- `@DOC:SPEC-STATE-001` — generated during `/moai:3-sync`.

### Requirement Index

| Requirement ID   | EARS Type                              | Summary                                                                                                 |
| ---------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| REQ-STATE-001-R1 | Ubiquitous + Event-driven              | `StateStorePort` + namespaced strategy-state persistence with apply-delta (VR `V_n`; MAB state)         |
| REQ-STATE-001-R2 | Event-driven + State-driven            | Order/idempotency ledger persistence + cross-process cancel resolution (resolves ADAPTER-002 follow-up) |
| REQ-STATE-001-R3 | State-driven + Unwanted                | Single-writer lease + optimistic concurrency + atomic / read-your-writes / restart-safe                 |
| REQ-STATE-001-R4 | Event-driven + State-driven + Unwanted | Pure, idempotent reconciliation: fills → ledger statuses + positions/avg-cost; converges on re-run      |
| REQ-STATE-001-R5 | Optional + Ubiquitous + Unwanted       | In-memory store (test vehicle) + generic config snapshot + Decimal-lossless + env-only secrets          |
