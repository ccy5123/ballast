# SPEC-STATE-001 — Implementation Plan

`@SPEC:SPEC-STATE-001`

> The **persistence foundation for 24/7 autonomous operation**: the durable contract between an
> always-on worker (sole writer) and a (Streamlit) dashboard, and what makes the worker restart-safe.
> The pure reconciliation core stays pure (inject fills + snapshot → new snapshot); the store backends
> are the IO boundary. Money is `Decimal` (2 dp via `quantize_money`); secrets/DB creds are env-only.
> **Backend choice (SQLite vs Postgres) is flagged below as a decision for orchestrator/user review.**

## Module Layout

All code lives under `src/ballast/state/` — the persistence IO boundary (alongside
`src/ballast/adapters/` and `src/ballast/orders/`). It imports CORE-001 / STRATEGY-001 / ORDER-001 /
ADAPTER-001 contracts; the pure core never imports it. `reconcile.py` is **pure** (no clock, no IO);
only the store backends touch IO.

```
src/ballast/state/
├── __init__.py
├── ports.py        # StateStorePort (load/apply strategy state, upsert/load/resolve orders,
│                   #   get/set config, acquire/release writer lease) typing.Protocol         [R1,R2,R3,R5]
├── models.py       # StrategyStateRecord, OrderLedgerRecord, OrderState (StrEnum),
│                   #   ConfigSnapshotRecord, FillRecord, StateSnapshot, ReconResult, Lease
│                   #   (frozen, Decimal money, float-rejecting, Decimal<->str lossless)       [R1,R2,R4,R5]
├── reconcile.py    # PURE reconcile(snapshot, fills, order_statuses) -> ReconResult           [R4]
├── memory.py       # InMemoryStateStore (in-memory, network-free, credential-free)            [R5]
└── sqlite.py       # SqliteStateStore (stdlib sqlite3, WAL)   <-- RECOMMENDED persistent backend [R1-R3,R5]
    #  (alt) pg.py  # PgStateStore (managed Postgres; Neon/Supabase) — only if Postgres is chosen
```

Tests mirror the layout under `tests/unit/state/`:
`test_models.py`, `test_reconcile.py`, `test_memory.py`, `test_store_conformance.py` (shared
in-memory ↔ backend parity suite), and `test_sqlite.py` (in-process SQLite temp file).
A `conftest.py` parametrizes the conformance suite over `{InMemoryStateStore, SqliteStateStore(tmp)}`.

## Dependencies to Add

| Dependency              | Where                 | Version | Reason                                                                            |
| ----------------------- | --------------------- | ------- | --------------------------------------------------------------------------------- |
| _(none)_                | in-memory + reconcile | —       | No new deps for `memory.py` / `reconcile.py` / `models.py`. `pydantic` v2 reused. |
| stdlib `sqlite3`        | `sqlite.py`           | stdlib  | If SQLite is chosen (RECOMMENDED): **zero** new third-party deps; WAL mode.       |
| `psycopg[binary]` (alt) | `pg.py`               | `>=3.2` | **Only if Postgres is chosen** — adds one driver dep + `DATABASE_URL` (env-only). |

`httpx`/`respx` are not needed (no broker network in this layer; fills are injected). `pandas`/`numpy`
are not used (Constitution: backtest-only).

## Types (REQ-STATE-001-R1/R2/R4/R5)

`OrderState` (`StrEnum`) — the ORDER-001 `SubmissionStatus` **extended** with reconciliation terminals:

```text
class OrderState(StrEnum):
    RECORDED  = "RECORDED"   # ORDER-001 dry-run preview
    SUBMITTED = "SUBMITTED"  # ORDER-001 live, accepted
    DUPLICATE = "DUPLICATE"  # ORDER-001 idempotent dedup
    BLOCKED   = "BLOCKED"    # ORDER-001 guard-blocked
    FAILED    = "FAILED"     # ORDER-001 port failure
    PARTIAL   = "PARTIAL"    # reconciliation: filled_qty < ordered_qty
    FILLED    = "FILLED"     # reconciliation: fully filled (terminal)
    CANCELED  = "CANCELED"   # reconciliation: canceled at broker (terminal)
    EXPIRED   = "EXPIRED"    # reconciliation: expired unfilled (terminal)
```

Frozen, `Decimal`-money, float-rejecting DTOs (reuse the `_reject_float` BeforeValidator pattern; money
serialized as a canonical decimal **string**, round-tripped to `Decimal`):

```text
StrategyStateRecord:  ns: str;  data: Mapping[str, Decimal];  version: int
OrderLedgerRecord:    client_order_id: str;  account_seq: str;  broker_order_id: str | None;
                      status: OrderState;  ordered_qty: Decimal;  filled_qty: Decimal;  ts: <injected stamp>
ConfigSnapshotRecord: data: Mapping[str, Any]  # money as decimal strings;  version: int
FillRecord:           fill_id: str;  client_order_id: str | None;  broker_order_id: str | None;
                      side: Side;  qty: Decimal;  price: Decimal;  ts: <injected stamp>
StateSnapshot:        strategy: Mapping[str, StrategyStateRecord];  orders: Mapping[str, OrderLedgerRecord]
ReconResult:          snapshot: StateSnapshot;  mutations: tuple[ReconMutation, ...]  # inspectable
Lease:                owner: str;  expires_at: <injected stamp>   # writer-lease handle
```

`StateStorePort` (`runtime_checkable typing.Protocol`, mirrors ADAPTER-001 / ORDER-001 `ports.py`):

```text
class StateStorePort(Protocol):
    # strategy state (R1)
    def load_strategy_state(self, ns: str) -> StrategyStateRecord: ...
    def apply_strategy_delta(self, ns: str, delta: Mapping[str, Decimal], *, expected_version: int) -> int: ...
    # order/idempotency ledger (R2)
    def upsert_order(self, client_order_id: str, *, account_seq: str, broker_order_id: str | None,
                     status: OrderState, ordered_qty: Decimal, filled_qty: Decimal, ts) -> None: ...
    def load_order(self, client_order_id: str) -> OrderLedgerRecord | None: ...
    def resolve_order_id(self, client_order_id: str) -> str | None: ...
    def list_open_orders(self, account_seq: str) -> tuple[OrderLedgerRecord, ...]: ...
    # config snapshot (R5)
    def get_config(self) -> ConfigSnapshotRecord: ...
    def set_config(self, snapshot: Mapping[str, Any], *, expected_version: int) -> int: ...
    # single-writer (R3)
    def acquire_writer_lease(self, owner: str, *, ttl) -> Lease: ...
    def release_writer_lease(self, lease: Lease) -> None: ...
```

## Persisted Schemas (REQ-STATE-001-R1/R2/R5)

Backend-neutral logical schema (a SQLite/Postgres backend materializes these as tables; the in-memory
store as dicts). Money is stored as TEXT/`Decimal`-string, never a binary float.

| Record family   | Key                | Columns / fields (logical)                                                                                                                                     |
| --------------- | ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| strategy_state  | `ns`               | `ns TEXT PK`, `data JSON (key→decimal-string)`, `version INTEGER`                                                                                              |
| order_ledger    | `client_order_id`  | `client_order_id TEXT PK`, `account_seq TEXT`, `broker_order_id TEXT NULL`, `status TEXT`, `ordered_qty TEXT`, `filled_qty TEXT`, `ts TEXT`, `version INTEGER` |
| config_snapshot | singleton (`id=1`) | `id INTEGER PK`, `data JSON (money as decimal-string)`, `version INTEGER`                                                                                      |
| writer_lease    | singleton (`id=1`) | `id INTEGER PK`, `owner TEXT`, `expires_at TEXT`                                                                                                               |
| applied_fills   | `fill_id`          | `fill_id TEXT PK`, `client_order_id TEXT`, `applied_qty TEXT` (idempotency watermark, R4)                                                                      |

`applied_fills` is the **idempotency watermark** for R4: a fill already in this table (at its filled-qty
watermark) is not re-applied, guaranteeing reconciliation converges on re-run.

## Apply-Delta Semantics (REQ-STATE-001-R1, FD3)

`apply_strategy_delta(ns, delta, *, expected_version) -> new_version` mirrors the backtest engine's
`_apply_state_delta` (`src/ballast/backtest/engine.py`):

1. Load the current `StrategyStateRecord(ns)`; assert `record.version == expected_version` else raise a
   clear `ConcurrencyError` (optimistic concurrency, FD6).
2. For each `(key, value)` in `delta`: if `key` is known for `ns` (VR: `V_n`/`pool`/`qty`; MAB:
   `avg_price`/`holdings`/`seed_remaining`/`round_idx`), set `data[key] = quantize_money(value)`; ignore
   unknown keys (forward-compatible — a future stateful strategy is non-breaking).
3. Bump `version`, write **atomically**, return the new `version`. The merge is delta-only (never a blind
   whole-record overwrite), and a subsequent read observes it (read-your-writes).

VR's `{"V_n": V2}` advances the persisted value line exactly as the engine advances `ledger.v_n`; MAB's
empty delta is a no-op (its state is fill-derived, advanced by R4 reconciliation).

## Order-Ledger + Cross-Process Cancel Resolution (REQ-STATE-001-R2, FD4)

The durable form of the ORDER-001 in-memory ledger and the ADAPTER-002 in-process
`client_order_id → orderId` map:

- `upsert_order(...)` writes `{client_order_id → account_seq, broker_order_id, status, ordered_qty,
filled_qty, ts}` keyed by `client_order_id` (the deterministic ORDER-001 idempotency key). Status
  advances monotonically (SUBMITTED → PARTIAL → FILLED/CANCELED/EXPIRED); a terminal status never
  regresses; a same-status re-upsert is a no-op.
- `resolve_order_id(client_order_id) -> broker_order_id | None` is the **cross-process cancel hook**:
  after a worker restart (or from the dashboard), `TossOrderAdapter.cancel_order` no longer finds the id
  in its in-process `_submitted` map and returns `FAILED`; resolving it from the **persisted** ledger
  yields the `orderId` so the cancel succeeds. This directly resolves the SPEC-ADAPTER-002 follow-up
  (whose `cancel_order` docstring notes "cross-restart resolution is a pre-live follow-up").
- Integration shape (for the Runner/adapter SPEC, not implemented here): the live `OrderManager` writes
  each `SubmissionResult` to the store via `upsert_order`; `cancel_order` first consults
  `resolve_order_id` before falling back to its in-process map.

## Single-Writer / Restart-Safe Semantics (REQ-STATE-001-R3, FD6)

- **Writer lease**: `acquire_writer_lease(owner, *, ttl)` grants the sole-writer right (a row in
  `writer_lease`, or an in-memory flag). A second concurrent `acquire` while the lease is held by
  another owner **fails** (`WriterLeaseHeldError`) — a stray second process cannot become a second
  writer. The lease has a `ttl` so a crashed owner's lease expires and the restarted worker can re-take
  it. Reads never need the lease.
- **Optimistic concurrency**: every mutating call carries `expected_version`; a mismatch raises
  `ConcurrencyError`. Lease + version together are belt-and-suspenders against double-writes.
- **Atomic + read-your-writes**: SQLite uses a transaction per mutation (WAL); Postgres uses a
  transaction; the in-memory store mutates under a lock and replaces the record reference atomically.
  A crash mid-write leaves the old or new record, never a torn one.
- **Restart-safety**: on restart the worker reloads strategy state + order ledger + config snapshot via
  the same port; the in-process state is rebuilt from durable truth.

## Reconciliation Flow + Idempotency (REQ-STATE-001-R4, FD7)

`reconcile(snapshot, fills, order_statuses) -> ReconResult` is a **pure** function (no clock, no IO, no
`float`; fills/statuses are injected — sourced by the caller via the ADAPTER-001 `BrokerAccountPort`:
`list_orders(status=CLOSED)` / `get_order` / `get_holdings`):

```text
for each fill in fills:
    if fill already in applied_fills at its filled-qty watermark:  continue   # idempotent, no double-count
    locate the ledger order by client_order_id / broker_order_id
    advance ledger status: SUBMITTED -> PARTIAL (filled_qty < ordered_qty) -> FILLED (filled_qty == ordered_qty)
    update strategy/position state from the fill:
        MAB BUY:  avg_price = quantize_money((avg_price*holdings + price*qty)/(holdings+qty));
                  holdings += qty; seed_remaining = max(0, seed_remaining - (price*qty + commission));
                  round_idx += 1     # matches engine _apply_buy
        MAB SELL: holdings -= qty; if holdings <= 0: avg_price = 0    # matches engine _apply_sell
        VR:       qty/pool updated to the post-fill position; mark cycle reconciled
                  (VR's V_n advances via R1 apply-delta, NOT from fills)
    record the fill in applied_fills (watermark)

for each (order_id, broker_status) in order_statuses:
    if order is SUBMITTED/unfilled and broker_status in {CANCELED, EXPIRED}:
        transition the ledger order to that terminal status (reconcile against broker truth)

return ReconResult(new_snapshot, mutations)   # mutations is inspectable; caller persists via the store
```

Idempotency is guaranteed by (a) the `applied_fills` watermark (a re-observed fill is skipped) and
(b) the monotonic, non-regressing status lifecycle (a terminal status is never overwritten). The
position/avg-cost math is **byte-for-byte the same** as the backtest engine's `_apply_buy`/`_apply_sell`
(`quantize_money` throughout) so live and backtest state evolve identically. The store applies the
returned `mutations` atomically (R3); re-running `reconcile` then re-persisting is a no-op.

## Config Snapshot (REQ-STATE-001-R5, FD5)

`get_config()` / `set_config(snapshot, *, expected_version)` persist a **generic** JSON-serializable
mapping (ticker / account / per-strategy allocation / `dry_run` / kill-switch) with money values as
decimal strings and a `version`. This SPEC deliberately keeps it **minimal and untyped** — the typed
Config model and its validation are the **Runner SPEC's** concern; the store only durably records the
worker↔dashboard shared settings so both sides see one source of truth (single-writer-safe via the
lease + `expected_version`).

## Backend Options + RECOMMENDATION (flagged for orchestrator/user review)

An **in-memory** store always ships for tests. For the persistent backend, two options sit behind the
identical `StateStorePort`. **This is a design decision flagged for orchestrator/user review — the SPEC
pins the port + semantics, not the final pick.**

### Option A — SQLite on a persistent volume (RECOMMENDED for v1)

- **Deps**: stdlib `sqlite3` (zero new third-party deps). WAL mode for concurrent readers (dashboard) +
  one writer (worker).
- **Topology fit**: single always-on worker on one host (e.g. Railway with a mounted volume) + a
  co-located Streamlit dashboard reading the same DB file / WAL. Matches A1 (single-writer) exactly.
- **Pros**: simplest; zero deps; trivially restart-safe (file persists on the volume); atomic
  transactions; no network, no credentials, no `DATABASE_URL`; fastest to ship and to test (a temp file
  IS the integration test).
- **Cons**: single-host (writer and dashboard must share the volume / same host); not suited to a
  remotely-hosted dashboard on a different host; concurrent-writer story is "one writer" by design
  (which is exactly the requirement here).
- **Why recommended**: the target is **one** 24/7 worker (sole writer) + a dashboard. SQLite + WAL on a
  volume satisfies single-writer / restart-safe / atomic with the least complexity, zero new deps, and
  no secrets — and the port abstraction means migrating to Postgres later is a backend swap, not a
  rewrite.

### Option B — Managed Postgres (Neon / Supabase)

- **Deps**: a Postgres driver (`psycopg[binary]`) + `DATABASE_URL` (env-only, `from_env` like
  ADAPTER-001). Repo has `moai-platform-neon` and `moai-platform-supabase` skills.
- **Topology fit**: worker + a **remotely-hosted** dashboard on a different host; both connect over the
  network. Better real concurrency, PITR/branching (Neon), RLS/realtime (Supabase).
- **Pros**: networked (decouples worker and dashboard hosts); robust concurrency; managed backups /
  point-in-time recovery; scales beyond one host.
- **Cons**: adds a dependency + a secret (`DATABASE_URL`); needs network; CI cannot hit a live cloud DB
  (must mock or run a local Postgres) — heavier than SQLite for a single-worker v1.
- **When to pick**: choose B if the dashboard will be hosted separately from the worker, or if multi-host
  / managed-backup requirements appear. The single-writer lease + optimistic concurrency port semantics
  are identical; only the backend file changes (`pg.py` instead of `sqlite.py`).

**RECOMMENDATION**: ship **Option A (SQLite-on-volume)** for v1 (single-worker-on-Railway + co-located
Streamlit), keeping `StateStorePort` clean so **Option B (managed Postgres)** is a drop-in later if the
dashboard moves off-host. **Confirm with orchestrator/user before implementation.**

## Decimal / Purity / Secrets Policy

- All money/quantity is `Decimal` (2 dp) via `quantize_money`; `float` is rejected on the money path
  (reuse the `_reject_float` BeforeValidator pattern, like CORE / ORDER / ADAPTER models). The store
  serializes `Decimal` as a canonical decimal **string** and round-trips it back to `Decimal` — never a
  binary float column.
- `reconcile.py` reads **no** wall clock and does **no** IO (fills / statuses / snapshot / `ts` injected).
  Only the store backends touch IO; the in-memory store touches none.
- DB credentials (`DATABASE_URL`) are read from **environment variables only** (mirroring ADAPTER-001
  `from_env`); never logged, never in a `repr`, never committed. The SQLite file path is config/env-driven
  and carries no secret.

## Risk Analysis

| Risk                              | Description                                                                  | Mitigation                                                                                                                |
| --------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Double-counted fill               | Reconciliation re-applies a fill (e.g. after restart / overlapping cycle).   | `applied_fills` watermark keyed by `fill_id` + filled-qty; pure `reconcile` skips already-applied; convergence test.      |
| Status regression                 | A terminal ledger status (FILLED/CANCELED) is overwritten back to SUBMITTED. | Monotonic non-regressing lifecycle in `upsert_order` + `reconcile`; explicit "re-run is no-op" test.                      |
| Double-write / two writers        | A stray second process writes concurrently, corrupting state.                | Writer lease (sole owner) + `expected_version` optimistic concurrency; second `acquire` fails; mismatch raises.           |
| Lost cross-process cancel mapping | `client_order_id → orderId` lost on restart ⇒ `cancel_order` fails.          | Durable order ledger; `resolve_order_id` from the store; restart test resolves the id and cancels (resolves ADAPTER-002). |
| Float on money path               | A `float` qty/price silently corrupts precision in persistence.              | `_reject_float` + `quantize_money`; Decimal stored/loaded as string; AC asserts float construction is rejected.           |
| Torn / partial write              | A crash mid-write leaves an inconsistent record.                             | Atomic transaction per mutation (SQLite WAL / Postgres txn / in-memory lock+swap); read-your-writes test.                 |
| Apply-delta drift from engine     | Live apply-delta diverges from the backtest `_apply_state_delta`.            | Same known-key set + `quantize_money`; unknown keys ignored; parity test against the engine's behavior.                   |
| Backend divergence                | SQLite/Postgres behaves differently from in-memory.                          | Shared `test_store_conformance.py` parametrized over both; identical port semantics asserted.                             |
| Secret leak                       | `DATABASE_URL` logged / committed / in a `repr`.                             | Env-only (`from_env`); redaction; SQLite path carries no secret; no live cloud DB in CI.                                  |
| Scope creep                       | Tempting to build the Runner / Scheduler / dashboard here.                   | Scope/Dependencies fence them as downstream; this SPEC ships only port + in-memory + one backend + pure reconcile.        |

## Test Approach (per EARS module, in-memory first)

- **R1 (`test_memory.py`, `test_store_conformance.py`)**: `load_strategy_state` defaults to an empty
  record at `version=0`; `apply_strategy_delta({"V_n": V2}, expected_version=0)` advances VR and bumps
  the version; unknown delta keys are ignored; a wrong `expected_version` raises `ConcurrencyError`; a
  reload after a **simulated restart** (new store instance over the same backing, or re-read) returns the
  last value; float rejected.
- **R2 (`test_store_conformance.py`)**: `upsert_order` then `resolve_order_id` returns the `orderId`;
  after a **simulated restart** a fresh store resolves the same `client_order_id → orderId` (cross-process
  cancel); status advances SUBMITTED→FILLED and never regresses; unknown key resolves to `None` (no
  guessing); `list_open_orders` returns submitted-unresolved orders.
- **R3 (`test_store_conformance.py`)**: a second `acquire_writer_lease` while held fails; a mutating call
  with a stale `expected_version` raises; read-your-writes (read after write observes it); reads need no
  lease; reload returns last-committed values.
- **R4 (`test_reconcile.py`)**: a MAB BUY fill recomputes `avg_price`/`holdings`/`seed_remaining`/
  `round_idx` identically to the engine's `_apply_buy`; a MAB SELL reduces holdings and zeroes avg_price
  at flat (`_apply_sell`); a VR fill updates `qty`/`pool` and marks the cycle (V_n untouched here); a
  CANCELED/EXPIRED broker status transitions an unfilled SUBMITTED order; **re-running `reconcile` over
  the same fills is a no-op** (idempotent, no double-count); the core reads no clock / does no IO.
- **R5 (`test_models.py`, `test_memory.py`)**: `InMemoryStateStore` satisfies `StateStorePort`
  (`runtime_checkable`); `Decimal` round-trips losslessly via string; `set_config`/`get_config` persist
  the generic snapshot with a version; float rejected; in-memory store needs no DB/credentials/network.
- **SQLite backend (`test_sqlite.py`)**: the same conformance suite against a **temp-file** SQLite DB
  in-process (WAL); atomicity / restart (reopen the file) / Decimal-as-string round-trip — **no live
  cloud DB**.

Inject `ts`, fills, order statuses, `expected_version`, and the lease `owner`/`ttl` into every test for
determinism.

## Milestones (priority-ordered, no time estimates)

- **Primary Goal (Priority High)**: `StateStorePort` + persisted DTOs + `OrderState` (`R1` types),
  namespaced strategy-state load + `apply_strategy_delta` with optimistic concurrency (`R1`), and the
  `InMemoryStateStore` (`R5`).
- **Secondary Goal (Priority High)**: the **pure** `reconcile` core (fills → ledger statuses +
  positions/avg-cost, idempotent) matching the engine's ledger math (`R4`), and the durable
  order/idempotency ledger + `resolve_order_id` cross-process cancel resolution (`R2`).
- **Final Goal (Priority Medium)**: single-writer lease + atomic / read-your-writes semantics (`R3`),
  the generic config snapshot (`R5`), and **one** persistent backend (`SqliteStateStore`, RECOMMENDED)
  validated by the shared conformance suite over a SQLite temp file.
- **Optional Goal (Priority Low)**: the alternative `PgStateStore` (only if Postgres is chosen at
  review), and richer `ReconResult.mutations` inspection / dashboard read helpers.

## Quality Gates (per Constitution)

- `ruff check .` → 0 errors; `ruff format --check .` clean.
- `mypy --strict src` → 0 errors.
- `pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% overall; the port + in-memory
  impl + pure reconciliation target **~100%**.
- TDD RED→GREEN→REFACTOR with the **in-memory** store (no real DB in unit tests). Any SQLite/Postgres
  backend is integration-tested **in-process** (SQLite temp file) or **mocked** — **never** against a
  live cloud DB in CI. Secrets/DB creds env-only; never logged or committed.
