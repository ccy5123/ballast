---
id: SPEC-RUNNER-001
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

- Initial draft. Defines the **Runner / typed Config entry point** (24/7 step 1) — the **single
  assembly point for every live action**. It is the worker's core "run one cycle" unit: acquire the
  single-writer lease, load/persist the typed operational config, then drive **one** cycle
  (strategy core → Order Manager → Toss order adapter) with the clock and market-hours **injected**.
  Everything downstream of it (Scheduler, Notifier, Streamlit dashboard, deployment) is **OUT OF
  SCOPE** and depends on this entry point existing first.
- Three responsibilities:
  1. **Typed Runner config** — a small typed model carrying the **operational** knobs the worker and
     dashboard share: `ticker`, `account_capital`, **per-strategy `allocation`**, the `dry_run` flag,
     and the **kill-switch**. It does **not fork** the existing `ballast.core.config.Config` (the
     strategy-domain source of truth: VR/MAB knobs, instruments, `account_seq`) nor the STATE-001
     `ConfigSnapshotRecord` (the generic durable snapshot). It **complements** them: `Config` stays the
     YAML strategy contract; the typed `RunnerConfig` is the typed lens over the generic
     `ConfigSnapshotRecord.data` that STATE-001 deliberately left to "the Runner SPEC" (STATE-001 FD5 /
     AC-18). Money is `Decimal` (2 dp); a bare `float` is rejected on the money path.
  2. **State Store integration** — read/write the config snapshot via the STATE-001 `StateStorePort`
     (`get_config` / `set_config`), **acquire a writer lease** (`acquire_writer_lease(owner, *, ttl)
-> Lease`) to guarantee a **single writer**, advance persisted strategy state via
     `apply_strategy_delta` (the STRATEGY-001 `PlanResult.state_delta` channel), and durably record
     each submission in the order ledger via `upsert_order`.
  3. **One cycle, purely coordinated** — assemble the existing parts: `Strategy.plan_orders(market,
state, cfg) -> PlanResult` (CORE/STRATEGY-001) → `OrderManager.place(...)` (ORDER-001,
     dry-run-first with the kill-switch / `max_position_pct` / dry-run guards already inside) → the
     `TossOrderAdapter` `BrokerOrderPort` (ADAPTER-002, the live submission backend that already
     exists). The Runner **coordinates**; it embeds **no** IO — the clock (`now` / `cycle_key`),
     the market snapshot (`Market`, carrying `is_open` / `is_holiday`), the `StateStorePort`, and the
     `BrokerOrderPort` are all **injected** and mockable.
- **Reuses, forks nothing**: `ballast.core.config.Config` / `ExecutionConfig`,
  `ballast.core.strategy.{Strategy, PlanResult}`, `ballast.core.models.{Market, State, Order, Side}`,
  `ballast.orders.manager.OrderManager` + `ballast.orders.guards`, `ballast.orders.ports.BrokerOrderPort`,
  `ballast.state.ports.StateStorePort` + `ballast.state.models.{ConfigSnapshotRecord, Lease,
OrderState}`, and the `ballast.adapters.toss.{orders.TossOrderAdapter, factory.from_env}` backend. It
  mirrors the established port-and-DTO style (frozen `Decimal`-money DTO, `_reject_float`,
  `quantize_money`).
- **Architecture rationale**: the **worker is the always-on engine**; **Streamlit is a dashboard**;
  the two are **decoupled via the State Store**. The Runner is the worker's "run one cycle" core. It is
  shaped so a future **Scheduler** can call `run_one_cycle` repeatedly (idempotent / re-entrant) and so
  the dashboard can flip `dry_run` / kill-switch through the shared snapshot — but it **ships none** of
  Scheduler / Notifier / dashboard / deployment.
- Four design choices (cycle granularity, the config source-of-truth precedence, the lease
  owner-identity scheme, and the lease hold lifetime) are enumerated in `plan.md` with their option
  analysis; all four are now **RESOLVED** (see the decision sub-entry below and "Resolved Design
  Decisions"). This SPEC pins the assembly surface and semantics; the resolved choices are recorded as
  DECIDED.

### Decisions resolved (2026-06-26)

The four previously open design decisions were **resolved by orchestrator/user — all chose option A**.
`status` remains `draft` (implementation not done yet); only the decisions are now fixed:

1. **Cycle granularity = A — one strategy+account per `run_one_cycle` call.** Decision basis (industry
   norm): Freqtrade runs one strategy per bot instance with per-instance capital allocation (maps 1:1 to
   ballast's `account_seq` isolation); NautilusTrader keeps each strategy an independent-state actor with
   the engine/loop dispatching (= the Runner "how" / Scheduler "when·which" separation). This matches
   ballast's existing per-`ns` cadence + account isolation with zero friction.
2. **Config source-of-truth precedence = A — State Store snapshot authoritative at runtime, seeded once
   from YAML/env on first boot** (so the dashboard can durably flip `dry_run` / kill-switch).
3. **Lease owner-identity = A — configured `WORKER_ID` env with a `host:pid` fallback.**
4. **Lease hold lifetime = A — acquire once at worker startup, hold across cycles (renew via `ttl`).**

---

# SPEC-RUNNER-001 — Runner / typed Config entry point (24/7 step 1)

`@SPEC:SPEC-RUNNER-001`

> The single assembly point for every live action and the worker's core "run one cycle" unit. It
> acquires the **single-writer lease**, loads/persists the typed **operational** config through the
> State Store, and drives **one** cycle: `Strategy.plan_orders` → `OrderManager` → `TossOrderAdapter`.
> The Runner **coordinates only**; it embeds no IO — the clock (`now`/`cycle_key`), the market snapshot
> (`Market`, with `is_open`/`is_holiday`), the `StateStorePort`, and the `BrokerOrderPort` are all
> **injected** and mockable. The kill-switch and `dry_run` gate block submission before any live call
> (reusing the ORDER-001 guards). Money/quantity are `Decimal` (2 dp via `quantize_money`); a bare
> `float` is rejected on the money path. Secrets (`TOSS_CLIENT_ID`/`SECRET`) are read from environment
> variables only (via the existing `from_env`) and never logged or placed in a `repr`.

## Environment

- Language: Python `>=3.11` (single language; financial values use `Decimal`).
- Packaging/deps: `uv` + `pyproject.toml`. `pydantic` v2 (`>=2.6`) is already present and is reused for
  the typed `RunnerConfig`. The Runner adds **no** new third-party runtime dependency — it only wires
  together CORE-001 / STRATEGY-001 / ORDER-001 / STATE-001 / ADAPTER-001/002 contracts that already
  ship.
- Module location: `src/ballast/app/` — a thin **application / orchestration** layer (the worker's
  core), sitting **above** `src/ballast/orders/` and `src/ballast/state/` and the
  `src/ballast/adapters/` boundary, and depending on `src/ballast/core/`. The package does **not exist
  yet**; this SPEC creates it. The pure core (`src/ballast/core/`) never depends on `app/`.
- Purity / injection: the Runner reads **no** wall clock and performs **no** network IO of its own. The
  clock (`now` and the derived `cycle_key`), the `Market` snapshot (carrying `is_open` / `is_holiday`
  market-hours), the `StateStorePort`, and the `BrokerOrderPort` are **injected as arguments** so every
  external IO (Toss adapter, State Store backend) is mockable in tests. The Runner contains coordination
  logic only; it embeds no IO.
- Money/quantity: `Decimal` only (never `float`), normalized to 2 dp via
  `ballast.core.models.quantize_money`. A bare `float` on the money path (`account_capital`, allocation
  ratios) is rejected at construction (reusing the `_reject_float` BeforeValidator pattern from CORE /
  ORDER / STATE models).
- Secrets: the Toss OAuth2 credentials (`TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET`) are read from
  **environment variables only** via the existing `ballast.adapters.toss.factory.from_env`; the Runner
  never embeds, logs, `repr`s, or commits a secret or token. The State Store backend / DB credential
  (if any) is likewise env-only (STATE-001 R5).
- Config: the strategy-domain `ballast.core.config.Config` (`ExecutionConfig.dry_run` /
  `max_position_pct`, the VR/MAB `StrategyConfig` with `account_seq` / `ticker`, instruments) is loaded
  from YAML as today. The typed **operational** `RunnerConfig` (capital / allocation / `dry_run` /
  kill-switch) round-trips through the STATE-001 generic `ConfigSnapshotRecord.data`.
- Tests: `pytest`, `pytest-cov`. The whole Runner is exercised against the STATE-001
  `InMemoryStateStore` and the ORDER-001 `RecordingBrokerOrderPort` (or a mock `BrokerOrderPort`) — **no
  real Toss network, no real DB, no credentials**. Time / market / lease `owner` / `ttl` are injected
  for determinism.
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: **The Runner is the single assembly point and the sole writer.** In the target topology exactly
  one worker process runs the Runner; before it mutates any state or submits any order it holds the
  STATE-001 writer lease, so a stray second process cannot become a second writer (STATE-001 A1 / R3).
- A2: **Strategy-domain config vs operational config are distinct and complementary.** The YAML
  `Config` (`ballast.core.config.Config`) remains the source of truth for **strategy knobs** (VR/MAB
  parameters, instruments, `account_seq`, `ExecutionConfig.max_position_pct`). The typed `RunnerConfig`
  carries only the **operational** knobs the worker and dashboard share (`ticker`, `account_capital`,
  per-strategy `allocation`, `dry_run`, kill-switch). Neither `Config` nor STATE-001's
  `ConfigSnapshotRecord` is forked; `RunnerConfig` is the typed lens over `ConfigSnapshotRecord.data`.
- A3: **The State Store is the runtime authority for the operational config.** The `RunnerConfig` is
  seeded once (from YAML/env) into the durable snapshot on first boot; thereafter the State Store
  snapshot is authoritative at runtime so the dashboard can durably flip `dry_run` / kill-switch and the
  worker reads the change on its next cycle. (DECIDED — Decision 2, option A.)
- A4: **Time and market-hours are injected (purity).** The Runner reads no wall clock. The "now" /
  `cycle_key` used for idempotency stamping and the `Market` snapshot (with `is_open` / `is_holiday`)
  used for market-hours awareness are passed in as arguments. The future Scheduler decides _when_ to
  call; the Runner only knows _how_ to run one cycle.
- A5: **One cycle = one strategy+account.** A single `run_one_cycle` call drives **one** strategy (one
  `ns` / `account_seq`): `plan_orders` → `OrderManager.place` → the order ledger. The Scheduler (later)
  decides which strategy to run on which cadence (MAB daily / VR cycle / LOC at the close). (DECIDED —
  Decision 1, option A; industry norm: Freqtrade one-strategy-per-bot-instance, NautilusTrader
  independent-state actor + dispatching engine.)
- A6: **The live submission backend already exists.** `TossOrderAdapter` (ADAPTER-002) is the concrete
  `BrokerOrderPort` the Runner passes to `OrderManager` on the live path; it is assembled by the
  existing `from_env`. The Runner introduces **no** new broker write code.
- A7: **Safety is enforced by reused guards, not re-implemented.** The `OrderManager` already routes
  every order through the kill-switch → `max_position_pct` → dry-run gate (ORDER-001 R4). The Runner
  **threads** the operational `dry_run` / kill-switch into `OrderManager.place` and **never** bypasses
  those guards; it adds no second, divergent safety path.
- A8: **Money is `Decimal`, 2 dp.** Every operational money value (`account_capital`, allocation
  ratios) is `Decimal` normalized via `quantize_money`; `float` is rejected on the money path. The
  snapshot persists money as decimal **strings** (STATE-001 R5) and round-trips losslessly.
- A9: **Idempotent / re-entrant by construction.** Because `client_order_id` is deterministic
  (ORDER-001 FD3) and the order ledger is durable (STATE-001 R2), the same cycle re-run within its
  window does **not** double-submit; the lease + `expected_version` (STATE-001 R3) prevent double-write.
  This is what lets a future Scheduler call `run_one_cycle` repeatedly safely.
- A10: **Strategy isolation = account.** Each strategy runs against its own `account_seq` (CORE-001 A2,
  v1 default); the Runner is account-addressable and holds no global mutable state beyond the injected
  store handle and the in-flight cycle.

## Fixed Definitions

These are pinned for RUNNER-001 and MUST NOT drift silently. Changing any requires a HISTORY entry.

- **FD1 — `RunnerConfig` is a typed lens, not a fork.** An immutable (pydantic frozen, `extra="forbid"`)
  model in `src/ballast/app/config.py` carrying the **operational** fields: `ticker: str`,
  `account_capital: Decimal`, `allocation: Mapping[str, Decimal]` (per-strategy ns → ratio, e.g.
  `{"vr": "0.6", "mab": "0.4"}`), `dry_run: bool`, `kill_switch: bool`. Money is `Decimal` (2 dp via
  `quantize_money`); a bare `float` is rejected. It does **not** redefine VR/MAB strategy knobs,
  instruments, or `account_seq` (those stay in `ballast.core.config.Config`), and it does **not** add a
  new persisted-record type (it serializes to / from the STATE-001 `ConfigSnapshotRecord.data`).
- **FD2 — `RunnerConfig` ⇄ `ConfigSnapshotRecord` round-trip.** `RunnerConfig.to_snapshot() ->
Mapping[str, Any]` (money as canonical decimal **strings**, never a binary float) and
  `RunnerConfig.from_snapshot(data: Mapping[str, Any]) -> RunnerConfig` are exact inverses; persisting
  via `StateStorePort.set_config(to_snapshot(), expected_version=...)` and reloading via
  `from_snapshot(get_config().data)` yields an equal `RunnerConfig`. The shape matches the STATE-001
  AC-18 example (`ticker` / `account_seq` / `allocation` / `dry_run` / `kill_switch`).
- **FD3 — Lease before any mutation or submission.** The Runner calls
  `StateStorePort.acquire_writer_lease(owner, *, ttl) -> Lease` and only then mutates strategy state,
  upserts the ledger, or submits live. A second concurrent acquire is rejected
  (`WriterLeaseHeldError`); the lease is released (or expires by `ttl`) on shutdown. **Reads never
  require the lease.** DECIDED — the `owner` is a configured `WORKER_ID` env value with a `host:pid`
  fallback (Decision 3, option A), and the lease is **acquired once at worker startup and held across
  cycles** (renewed via `ttl`), not acquired/released per cycle (Decision 4, option A).
- **FD4 — `run_one_cycle` is the unit of work.** A single method —
  `run_one_cycle(strategy, market, *, cfg, cycle_key, store, port, account_inputs=...) -> CycleResult`
  — drives exactly one cycle for one strategy: (1) load persisted `State` for `strategy.ns` from the
  store; (2) `result = strategy.plan_orders(market, state, cfg)`; (3) feed `result.orders` to
  `OrderManager.place(...)` threading `dry_run` / `kill_switch` / `max_position_pct` and the injected
  account inputs; (4) on a successful (non-dry-run) submission, `upsert_order` each result into the
  ledger; (5) `apply_strategy_delta(strategy.ns, result.state_delta, expected_version=...)`. It reads no
  clock and does no IO of its own.
- **FD5 — `dry_run` / kill-switch gate, reused not re-implemented.** The Runner passes the operational
  `dry_run` and `kill_switch` straight into `OrderManager.place`, which enforces the ORDER-001 R4
  ordered guards (kill-switch precedence → `max_position_pct` clamp/block → dry-run gate). The Runner
  **shall not** add a divergent safety path and **shall not** submit live when `dry_run` is true or the
  kill-switch is engaged.
- **FD6 — `CycleResult` is inspectable and broker-neutral.** An immutable DTO summarizing one cycle:
  the strategy `ns`, the `OrderPlan` (intents + `SubmissionResult`s from `OrderManager`), the applied
  `state_delta`, the resulting strategy-state `version`, and the ledger upserts performed. Money fields,
  if any, are `Decimal`. It carries no Toss-specific field and no secret.
- **FD7 — Idempotent / re-entrant re-run.** Re-invoking `run_one_cycle` for the **same** cycle (same
  `ns`, `cycle_key`, market, config) **shall not** create a second distinct live order (the
  deterministic `client_order_id` deduplicates via the ORDER-001 ledger + STATE-001 persisted ledger)
  and **shall not** double-apply the `state_delta` (optimistic-concurrency `expected_version` guards the
  write). This is the property a future Scheduler relies on to call `run_one_cycle` repeatedly.

## Requirements

The Runner MUST satisfy the following EARS requirements (6 modules). All are tagged to
`@SPEC:SPEC-RUNNER-001`.

### REQ-RUNNER-001-R1 — Typed `RunnerConfig` Operational Model + Relationship to `Config` / `ConfigSnapshotRecord` (Ubiquitous)

`@SPEC:SPEC-RUNNER-001` `REQ-RUNNER-001-R1`

The system **shall always** define, in `src/ballast/app/config.py`, an immutable typed `RunnerConfig`
model (FD1) carrying the **operational** knobs `ticker`, `account_capital`, per-strategy `allocation`,
`dry_run`, and `kill_switch`:

- Money/quantity fields (`account_capital`, the `allocation` ratios) **shall** be `Decimal` normalized
  to 2 dp via `quantize_money`, and a bare `float` **shall** be rejected at construction (reusing the
  `_reject_float` pattern).
- `RunnerConfig` **shall not** fork or duplicate the strategy-domain `ballast.core.config.Config` (VR/MAB
  knobs, instruments, `account_seq`, `ExecutionConfig`) — that remains the YAML source of truth — and
  **shall not** introduce a new persisted record type beyond the STATE-001 `ConfigSnapshotRecord`.
- The system **shall** provide `RunnerConfig.to_snapshot()` and `RunnerConfig.from_snapshot(data)`
  (FD2) as exact inverses, serializing money as canonical decimal **strings** (never a binary float) so
  the snapshot round-trips losslessly to `Decimal` and matches the STATE-001 AC-18 generic shape.
- The system **shall not** place a `float` on the money path and **shall not** require any broker
  credential or network to construct a `RunnerConfig`.

### REQ-RUNNER-001-R2 — Config Load + Persist via the State Store (Event-driven + State-driven)

`@SPEC:SPEC-RUNNER-001` `REQ-RUNNER-001-R2`

The system **shall** read and write the operational config through the STATE-001 `StateStorePort`, in
`src/ballast/app/runner.py`:

- **When** the worker boots and the durable config snapshot is empty (`get_config().version == 0`), the
  system **shall** seed it once from the typed `RunnerConfig` (built from YAML/env) via
  `set_config(cfg.to_snapshot(), expected_version=0)`.
- **When** the worker starts a cycle, the system **shall** load the operational config by reading
  `get_config()` and reconstructing `RunnerConfig.from_snapshot(record.data)`, so a dashboard-written
  change to `dry_run` / kill-switch is observed on the next cycle (the State Store snapshot is the
  runtime authority — A3).
- **While** persisting a config change, the system **shall** call `set_config(snapshot, *,
expected_version)` carrying the current `version` and **shall** surface a clear concurrency error on a
  version mismatch (STATE-001 optimistic concurrency); money values **shall** be decimal strings.
- The system **shall not** treat the YAML `Config` as the runtime authority for the operational fields
  after the first-boot seed (the snapshot wins — A3), and **shall not** write a secret into the
  snapshot.

### REQ-RUNNER-001-R3 — Acquire the Single-Writer Lease Before Any Mutation or Submission (State-driven + Unwanted)

`@SPEC:SPEC-RUNNER-001` `REQ-RUNNER-001-R3`

The system **shall** guarantee a single writer by acquiring the STATE-001 writer lease before any state
mutation or live submission (FD3):

- **Before** the worker mutates strategy state (`apply_strategy_delta`), upserts the order ledger
  (`upsert_order`), or submits any live order, the system **shall** hold a lease obtained via
  `acquire_writer_lease(owner, *, ttl) -> Lease`.
- **While** a lease is held by one owner, a second concurrent `acquire_writer_lease` **shall** fail
  (`WriterLeaseHeldError`) so a stray second process cannot become a second writer; on shutdown the
  system **shall** release the lease (`release_writer_lease(lease)`) or let it expire by `ttl`.
- Reads (`get_config`, `load_strategy_state`, `load_order`, `list_open_orders`) **shall not** require
  the lease (the dashboard reads freely).
- The system **shall not** mutate persisted state or submit a live order without first holding the
  lease, and **shall not** silently proceed if the lease cannot be acquired (it **shall** raise/return a
  clear "another writer holds the lease" outcome).

### REQ-RUNNER-001-R4 — Run One Cycle: Assemble Strategy Core → Order Manager → Toss Adapter (Event-driven)

`@SPEC:SPEC-RUNNER-001` `REQ-RUNNER-001-R4`

The system **shall** provide `run_one_cycle(...)` in `src/ballast/app/runner.py` that drives exactly one
cycle for one strategy by assembling the existing parts, with the clock and market-hours **injected**
(FD4):

- **When** `run_one_cycle(strategy, market, *, cfg, cycle_key, store, port, account_inputs=...)` is
  called, the system **shall**: (1) load the persisted `State` for `strategy.ns` from `store`; (2) call
  `result = strategy.plan_orders(market, state, cfg)` (CORE/STRATEGY-001); (3) call
  `OrderManager.place(result.orders, ns=strategy.ns, cycle_key=cycle_key, dry_run=..., kill_switch=...,
max_position_pct=..., base_values=..., positions=..., prices=..., port=port)` (ORDER-001); (4) durably
  `upsert_order` each non-dry-run `SubmissionResult` into the STATE-001 ledger; (5)
  `apply_strategy_delta(strategy.ns, result.state_delta, expected_version=...)` to advance VR `V_n`
  (MAB's empty delta is a no-op).
- The `market` snapshot (`ballast.core.models.Market`, carrying `is_open` / `is_holiday`) and the
  `cycle_key` (derived from an injected "now") **shall** be the only sources of time / market-hours; the
  Runner **shall** read **no** wall clock.
- The `port` (a `BrokerOrderPort`, e.g. `TossOrderAdapter` from `from_env`, or a recording/mock port in
  tests) and the `store` (`StateStorePort`) **shall** be injected so every external IO is mockable.
- The system **shall** return an inspectable `CycleResult` (FD6: `ns`, the `OrderPlan`, the applied
  delta, the new strategy-state `version`, the ledger upserts) and **shall not** embed any IO or any
  Toss-specific field in the Runner itself.

### REQ-RUNNER-001-R5 — Safety Gating: Kill-Switch + `dry_run` Gate Block Submission (Reused Guards) (State-driven + Unwanted)

`@SPEC:SPEC-RUNNER-001` `REQ-RUNNER-001-R5`

The system **shall** honor the kill-switch and the `dry_run` gate before any submission, **reusing** the
ORDER-001 guards rather than re-implementing them (FD5):

- **While** the kill-switch is engaged (from the operational `RunnerConfig`), the system **shall** pass
  `kill_switch=True` into `OrderManager.place`, which blocks every order
  (`SubmissionResult.status=BLOCKED`, kill-switch reason) — no recording, no submission — and the Runner
  **shall not** submit any live order.
- **While** `dry_run` is true (the shipped default), the system **shall** pass `dry_run=True` so the
  Order Manager takes the record-only path (no `place_order` / `cancel_order` call, no network), and the
  Runner **shall not** call the Toss adapter.
- The system **shall** thread the `max_position_pct` clamp inputs (the injected position / buying-power
  / reference price from the ADAPTER-001 read port) into `OrderManager.place` so the existing clamp /
  block applies unchanged.
- The system **shall not** add a second, divergent safety path, **shall not** submit live when `dry_run`
  is true or the kill-switch is engaged, and **shall not** mutate the ledger / persisted state for an
  order the guards blocked.

### REQ-RUNNER-001-R6 — Idempotent / Re-Entrant One-Cycle for a Future Scheduler (Optional + Unwanted)

`@SPEC:SPEC-RUNNER-001` `REQ-RUNNER-001-R6`

- The system **shall** be designed so a future Scheduler can call `run_one_cycle` **repeatedly** for the
  same cycle window without double-submitting or double-applying state (FD7), leaning on the
  deterministic ORDER-001 `client_order_id` and the durable STATE-001 ledger.
- **When** `run_one_cycle` is re-invoked for the **same** `(ns, cycle_key, market, cfg)`, the system
  **shall not** create a second distinct live order — the deterministic `client_order_id` resolves to an
  already-recorded ledger entry (`DUPLICATE`) — and **shall not** apply the `state_delta` a second time
  (the `expected_version` optimistic-concurrency guard rejects a stale re-apply).
- **Where** a cancel must be resolved after a restart or from another process, the system **shall** use
  the persisted ledger's `resolve_order_id(client_order_id) -> broker_order_id | None` (STATE-001 R2)
  rather than relying on the in-process `TossOrderAdapter` map alone.
- The system **shall not** depend on any randomness, UUID, or wall-clock read for the idempotency key,
  and **shall not** assume a fresh, empty ledger on each cycle (it reads the durable ledger).

## Specifications

Reused input → Runner assembly step → implementing module.

| Concern (REQ)                                                        | Input (reused)                                                                                                           | Module                      |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | --------------------------- |
| Typed `RunnerConfig` + `Config`/`ConfigSnapshotRecord` relation `R1` | CORE `Config`/`ExecutionConfig`, `quantize_money`; STATE `ConfigSnapshotRecord`                                          | `src/ballast/app/config.py` |
| Config load/seed/persist via the store `R2`                          | STATE `StateStorePort.get_config`/`set_config`; `RunnerConfig.to_snapshot`/`from_snapshot`                               | `src/ballast/app/runner.py` |
| Single-writer lease before any mutation/submission `R3`              | STATE `acquire_writer_lease`/`release_writer_lease`/`Lease`/`WriterLeaseHeldError`                                       | `src/ballast/app/runner.py` |
| Run one cycle: strategy → Order Manager → Toss adapter `R4`          | CORE/STRATEGY `Strategy.plan_orders`/`PlanResult`/`Market`/`State`; ORDER `OrderManager`; ADAPTER-002 `TossOrderAdapter` | `src/ballast/app/runner.py` |
| Kill-switch + `dry_run` gate (reused guards) `R5`                    | ORDER `OrderManager.place` + `guards` (kill-switch / `max_position_pct` / dry-run)                                       | `src/ballast/app/runner.py` |
| Idempotent / re-entrant re-run + cross-process cancel resolve `R6`   | ORDER deterministic `client_order_id`; STATE order ledger + `resolve_order_id`                                           | `src/ballast/app/runner.py` |

### DTOs / types introduced (REQ-RUNNER-001-R1/R4)

| Type           | Kind       | Key fields                                                                                                                    |
| -------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `RunnerConfig` | frozen DTO | `ticker: str`, `account_capital: Decimal`, `allocation: Mapping[str, Decimal]`, `dry_run: bool`, `kill_switch: bool`          |
| `CycleResult`  | frozen DTO | `ns: str`, `plan: OrderPlan` (reused), `state_delta: Mapping[str, Decimal]`, `new_version: int`, `ledger_upserts: tuple[...]` |

> No new ports are introduced. The Runner is a consumer of the existing `StateStorePort` and
> `BrokerOrderPort`; it adds only the typed `RunnerConfig` lens and the `CycleResult` summary.

## Dependencies

- **Depends on SPEC-CORE-001**: reuses `ballast.core.config.{Config, ExecutionConfig}`,
  `ballast.core.models.{Market, State, Order, Side, quantize_money}`, and
  `ballast.core.strategy.{Strategy, PlanResult}`. Money normalization always calls `quantize_money`
  (never re-implemented). `Market` is the injected source of "now"/price/market-hours for the pure
  strategy call.
- **Depends on SPEC-STRATEGY-001**: consumes `PlanResult(orders, state_delta)` from
  `Strategy.plan_orders` and advances persisted VR `V_n` via the `state_delta` apply-delta channel
  (MAB's empty delta is a no-op).
- **Depends on SPEC-ORDER-001**: drives `OrderManager.place(...)` and reuses its ordered guards
  (kill-switch / `max_position_pct` / dry-run). The Runner threads `dry_run` / `kill_switch` /
  `max_position_pct` and the injected position / buying-power / price inputs; it adds no second safety
  path.
- **Depends on SPEC-STATE-001**: reuses `StateStorePort` (`get_config` / `set_config` /
  `acquire_writer_lease` / `release_writer_lease` / `apply_strategy_delta` / `load_strategy_state` /
  `upsert_order` / `load_order` / `resolve_order_id` / `list_open_orders`) and the DTOs
  `ConfigSnapshotRecord` / `Lease` / `OrderState`. Tests run against `InMemoryStateStore`. The typed
  `RunnerConfig` is the lens STATE-001 FD5 deferred to "the Runner SPEC".
- **Depends on SPEC-ADAPTER-001 (read ports)**: the `max_position_pct` clamp inputs (positions /
  buying-power / reference price) are obtained by the caller via the read-only `BrokerAccountPort`
  (`get_holdings` / `get_buying_power`) and **injected** into `run_one_cycle`; fills for any
  reconciliation are likewise read via that port (reconciliation itself is STATE-001, not this SPEC).
- **Depends on SPEC-ADAPTER-002 (write adapter)**: `TossOrderAdapter` (the existing `BrokerOrderPort`
  live submission backend), assembled by `ballast.adapters.toss.factory.from_env`, is the `port` the
  Runner passes to `OrderManager` on the live path. The Runner introduces no new broker write code.
- **Downstream consumers (OUT of scope, later SPECs)** — the Runner is **shaped to support** these but
  ships none of them:
  - **Scheduler** — calls `run_one_cycle` on a cadence (MAB daily / VR cycle / LOC at the close,
    DST-aware), deciding _when_; the Runner knows only _how_ to run one cycle.
  - **Notifier** — reads the `CycleResult` / ledger to emit fill / error / kill-switch alerts.
  - **Streamlit dashboard** — reads strategy state / ledger / config snapshot and writes the config
    snapshot (`dry_run` / kill-switch) as the worker's read/write peer.
  - **Deployment** — provisions the always-on worker + the persistent State Store backend.
- New third-party deps: **none** — the Runner only wires together contracts that already ship.

## Scope

### In scope (the worker's "run one cycle" entry point)

- The typed `RunnerConfig` operational model + `to_snapshot` / `from_snapshot` round-trip (R1).
- Config load / first-boot seed / persist through the STATE-001 `StateStorePort` (R2).
- Acquiring (and releasing) the single-writer lease before any mutation or submission (R3).
- `run_one_cycle` assembling `Strategy.plan_orders` → `OrderManager.place` → `TossOrderAdapter`, with
  the clock and market-hours injected, and persisting the `state_delta` + ledger upserts (R4).
- Safety gating that **reuses** the ORDER-001 kill-switch / `dry_run` / `max_position_pct` guards (R5).
- An idempotent / re-entrant `run_one_cycle` (deterministic `client_order_id` + durable ledger) so a
  future Scheduler can call it repeatedly, plus cross-process cancel resolution via `resolve_order_id`
  (R6).
- The new `src/ballast/app/` package (`config.py`, `runner.py`, `__init__.py`).

### Out of scope (deferred to later SPECs)

The system **shall not** implement any of the following in this SPEC:

- The **Scheduler** (cadence / DST / market-hours triggering — the Runner only consumes an injected
  `Market` and `cycle_key`), the **Notifier** (ntfy / Discord), the **Streamlit dashboard**, and
  **deployment** (Railway always-on worker, volume / managed-DB provisioning).
- Any **new broker write** code (that is ADAPTER-002 `TossOrderAdapter`), any **new State Store
  backend** (that is STATE-001), and the **reconciliation** routine itself (STATE-001 `reconcile`) — the
  Runner _calls_ these contracts; it does not redefine them.
- Any **strategy decision logic** (CORE / VR / MAB / STRATEGY-001) — the Runner coordinates the existing
  `Strategy.plan_orders`; it computes no orders itself.
- Note: the four design decisions (cycle granularity, config source-of-truth precedence, lease
  owner-identity, lease hold lifetime) are now **RESOLVED** (all option A — see "Resolved Design
  Decisions" and `plan.md`); they are no longer open. Their full option analysis is retained in
  `plan.md` for traceability.

## Resolved Design Decisions (DECIDED 2026-06-26 — see `plan.md` for the full option analysis)

Four choices were genuinely user-resolvable. They are now **RESOLVED by orchestrator/user — all chose
option A**. This SPEC pins the assembly surface and semantics; the option analysis and rationale are
retained in `plan.md` for traceability:

1. **Cycle granularity = A — one strategy+account per `run_one_cycle` call.** Rationale: matches
   ballast's per-`ns` cadence + `account_seq` isolation, and the industry norm — Freqtrade runs one
   strategy per bot instance with per-instance capital allocation, and NautilusTrader keeps each strategy
   an independent-state actor with the engine/loop dispatching (the Runner "how" / Scheduler "when·which"
   separation).
2. **Config source-of-truth precedence = A — State Store snapshot authoritative at runtime, seeded once
   from YAML/env on first boot.** Rationale: lets the dashboard durably flip `dry_run` / kill-switch (a
   real read/write peer), which a YAML-authoritative-on-every-boot model would revert.
3. **Lease owner-identity = A — configured `WORKER_ID` env with a `host:pid` fallback.** Rationale: a
   stable identity lets a redeploy reclaim / observe its own lease; `ttl` covers a crashed owner.
4. **Lease hold lifetime = A — acquire once at worker startup, hold across cycles (renew via `ttl`).**
   Rationale: the worker IS the sole writer for its whole life; the Scheduler calls `run_one_cycle`
   repeatedly under the already-held lease, avoiding per-cycle churn and an acquire/release race window.

## Reality Constraints

- The live path is validated **only** through the ORDER-001 `RecordingBrokerOrderPort` / a mock
  `BrokerOrderPort` and the STATE-001 `InMemoryStateStore` — **no real Toss network, no real DB, no
  credentials** in CI. A real-broker soak (`dry_run` first, then a tiny live allocation) is a step the
  user performs locally / in the cloud with their own keys; it is not, and cannot be, an automated gate
  in this environment.
- The Runner reads **no** wall clock and performs **no** network IO of its own: time / market-hours
  (`now` / `cycle_key` / `Market`), the `StateStorePort`, and the `BrokerOrderPort` are injected. Money
  is `Decimal` (2 dp) end-to-end; `float` is rejected on the money path. Secrets (`TOSS_CLIENT_ID` /
  `SECRET`) are env-only (via `from_env`), masked, never logged or committed.

## Traceability

- `@SPEC:SPEC-RUNNER-001` — this document.
- `@TEST:SPEC-RUNNER-001` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/app/`.
- `@CODE:SPEC-RUNNER-001` — `src/ballast/app/{config,runner}.py`.
- `@DOC:SPEC-RUNNER-001` — generated during `/moai:3-sync`.

### Requirement Index

| Requirement ID    | EARS Type                   | Summary                                                                                                            |
| ----------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| REQ-RUNNER-001-R1 | Ubiquitous                  | Typed `RunnerConfig` operational model + `to/from_snapshot`; complements `Config`/`ConfigSnapshotRecord` (no fork) |
| REQ-RUNNER-001-R2 | Event-driven + State-driven | Config load / first-boot seed / persist via `StateStorePort.get_config`/`set_config` (snapshot authoritative)      |
| REQ-RUNNER-001-R3 | State-driven + Unwanted     | Acquire the single-writer lease before any mutation or submission; reads need no lease                             |
| REQ-RUNNER-001-R4 | Event-driven                | `run_one_cycle`: assemble `plan_orders` → `OrderManager` → `TossOrderAdapter`; clock/market injected               |
| REQ-RUNNER-001-R5 | State-driven + Unwanted     | Kill-switch + `dry_run` gate block submission, reusing the ORDER-001 guards (no divergent path)                    |
| REQ-RUNNER-001-R6 | Optional + Unwanted         | Idempotent / re-entrant one-cycle for a future Scheduler; cross-process cancel via `resolve_order_id`              |
