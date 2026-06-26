# SPEC-RUNNER-001 — Implementation Plan

`@SPEC:SPEC-RUNNER-001`

> The **single assembly point for every live action** and the worker's core "run one cycle" unit. It
> acquires the single-writer lease, loads/persists the typed operational config through the State Store,
> and drives one cycle: `Strategy.plan_orders` → `OrderManager` → `TossOrderAdapter`. The Runner
> coordinates only; the clock (`now`/`cycle_key`), the `Market` snapshot (with `is_open`/`is_holiday`),
> the `StateStorePort`, and the `BrokerOrderPort` are **injected** and mockable. Money is `Decimal`
> (2 dp via `quantize_money`); secrets are env-only via the existing `from_env`. **Four design
> decisions (cycle granularity, config source-of-truth, lease owner-identity, lease lifetime) are now
> RESOLVED below — all option A.**

## Module Layout

All code lives under a **new** `src/ballast/app/` package — the worker's application / orchestration
layer, sitting above `src/ballast/orders/` and `src/ballast/state/` and the `src/ballast/adapters/`
boundary, depending on `src/ballast/core/`. The pure core never imports `app/`. The Runner embeds **no**
IO; the store, port, clock, and market snapshot are injected.

```
src/ballast/app/
├── __init__.py
├── config.py     # RunnerConfig (typed operational lens) + to_snapshot / from_snapshot          [R1]
└── runner.py     # Runner.run_one_cycle: lease -> load cfg -> plan_orders -> OrderManager
                  #   -> TossOrderAdapter -> upsert ledger -> apply_strategy_delta; CycleResult   [R2-R6]
```

Tests mirror the layout under `tests/unit/app/`:
`test_config.py` (RunnerConfig round-trip, float rejection, allocation), and
`test_runner.py` (lease, load/seed/persist, one cycle dry-run + live via RecordingBrokerOrderPort,
kill-switch/dry_run gating, idempotent re-run). A `conftest.py` provides fixtures: an
`InMemoryStateStore`, a `RecordingBrokerOrderPort` (or a mock `BrokerOrderPort`), a fake `Strategy`
(returning a fixed `PlanResult`), a fixed `Market`, an injected `cycle_key`, and a `RunnerConfig`.

## Dependencies to Add

| Dependency | Where   | Version | Reason                                                                                                                                                          |
| ---------- | ------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| _(none)_   | `app/*` | —       | No new third-party deps. `pydantic` v2 reused for `RunnerConfig`. Everything else wires existing contracts (CORE / STRATEGY / ORDER / STATE / ADAPTER-001/002). |

`httpx`/`respx` are needed only by the already-shipped Toss adapter, not by the Runner (the `port` is
injected; tests use the recording/mock port). `pandas`/`numpy` are not used (Constitution: backtest-only).

## Types (REQ-RUNNER-001-R1/R4)

`RunnerConfig` — the typed **operational** lens over the STATE-001 `ConfigSnapshotRecord.data` (frozen,
`extra="forbid"`, `Decimal`-money, float-rejecting via the `_reject_float` BeforeValidator pattern):

```text
RunnerConfig:
    ticker: str
    account_capital: Decimal            # money, 2-dp via quantize_money; bare float rejected
    allocation: Mapping[str, Decimal]   # per-strategy ns -> ratio, e.g. {"vr": 0.6, "mab": 0.4}
    dry_run: bool                       # operational gate (shipped default: True)
    kill_switch: bool                   # operational stop (shipped default: False)

    def to_snapshot(self) -> Mapping[str, Any]:    # money as canonical decimal STRINGS, never float
    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "RunnerConfig":   # exact inverse of to_snapshot
```

`CycleResult` — an inspectable, broker-neutral summary of one cycle (frozen DTO; reuses ORDER-001
`OrderPlan`):

```text
CycleResult:
    ns: str
    plan: OrderPlan                      # ORDER-001: intents + SubmissionResults (reused, not forked)
    state_delta: Mapping[str, Decimal]   # the STRATEGY-001 delta applied this cycle (VR {"V_n":..}; MAB {})
    new_version: int                     # the strategy-state version after apply_strategy_delta
    ledger_upserts: tuple[str, ...]      # the client_order_ids upserted into the STATE-001 ledger
```

No new ports. The Runner consumes the existing `StateStorePort` and `BrokerOrderPort`.

## RunnerConfig ⇄ ConfigSnapshotRecord Round-Trip (REQ-RUNNER-001-R1/R2, FD1/FD2)

The typed `RunnerConfig` is the lens STATE-001 FD5 deferred ("the typed Config model is the Runner
SPEC's concern"). The persisted shape is the STATE-001 AC-18 generic mapping:

```text
to_snapshot() ->
    {"ticker": "QLD",
     "account_capital": "10000.00",                 # money as a canonical decimal STRING
     "allocation": {"vr": "0.6", "mab": "0.4"},     # ratios as decimal STRINGS
     "dry_run": True,
     "kill_switch": False}

from_snapshot(data) -> RunnerConfig(...)            # Decimal(...) on the money/ratio strings; exact inverse
```

Relationship to the strategy-domain `Config` (`ballast.core.config.Config`): **complementary, not a
fork**. `Config` (YAML) stays the source of truth for VR/MAB knobs, instruments, `account_seq`, and
`ExecutionConfig.max_position_pct`. `RunnerConfig` owns only the **operational** worker↔dashboard fields.
The two `dry_run` values are reconciled by precedence (Decision 2, DECIDED option A): on first boot the
snapshot is seeded from `RunnerConfig` (itself buildable from `ExecutionConfig.dry_run` + env), and
thereafter the snapshot's `dry_run` / `kill_switch` (which the dashboard can flip) is the runtime
authority.

## One-Cycle Flow (REQ-RUNNER-001-R3/R4/R5/R6, FD3/FD4/FD5/FD7)

`Runner.run_one_cycle(strategy, market, *, cfg, cycle_key, store, port, account_inputs=...) ->
CycleResult` (the clock and market-hours are injected; the Runner reads no wall clock, does no IO):

```text
# (worker startup, once) DECIDED owner = env WORKER_ID or f"{host}:{pid}" fallback (Decision 3, option A)
# DECIDED: acquire ONCE at startup and HOLD across cycles, renew via ttl (Decision 4, option A)
lease = store.acquire_writer_lease(owner, ttl=...)          # WriterLeaseHeldError if a 2nd writer (R3)

# (worker startup, once) seed the config snapshot from YAML/env IFF empty (first boot); else leave it
if store.get_config().version == 0:                               # seed-once (Decision 2, option A; R2)
    store.set_config(RunnerConfig.from_yaml_env(...).to_snapshot(), expected_version=0)

# (each cycle) the State Store snapshot is the runtime authority (Decision 2, option A)
runner_cfg = RunnerConfig.from_snapshot(store.get_config().data)   # snapshot is runtime authority (R2)

state_rec = store.load_strategy_state(strategy.ns)                 # durable State (reads need no lease)
state = State(ns=strategy.ns, data=dict(state_rec.data))
result = strategy.plan_orders(market, state, cfg)                 # PURE (CORE/STRATEGY-001); injected market

plan = OrderManager().place(                                       # ORDER-001 guards inside (R5)
    result.orders, ns=strategy.ns, cycle_key=cycle_key,
    dry_run=runner_cfg.dry_run, kill_switch=runner_cfg.kill_switch,
    max_position_pct=cfg.execution.max_position_pct,
    base_values=account_inputs.base_values,                        # from ADAPTER-001 read port (injected)
    positions=account_inputs.positions, prices=account_inputs.prices,
    port=port)                                                     # TossOrderAdapter (ADAPTER-002) live path

for intent, res in zip(plan.intents, plan.results):               # durable ledger (R4); skip dry-run RECORDED
    if res.status in {SUBMITTED, DUPLICATE, FAILED, ...}:
        store.upsert_order(res.client_order_id, account_seq=intent.account_seq,
                           broker_order_id=res.broker_order_id, status=OrderState(res.status),
                           ordered_qty=intent.qty, filled_qty=Decimal("0"), ts=cycle_key)

new_version = store.apply_strategy_delta(strategy.ns, result.state_delta,
                                         expected_version=state_rec.version)   # VR V_n; MAB {} no-op (R4)

return CycleResult(ns=strategy.ns, plan=plan, state_delta=result.state_delta,
                   new_version=new_version, ledger_upserts=tuple(upserted_ids))

# (worker shutdown) store.release_writer_lease(lease)
```

Re-entrancy / idempotency (R6): re-running the same `(ns, cycle_key, market, cfg)` derives the **same**
`client_order_id`s; the ORDER-001 ledger + the STATE-001 persisted ledger dedup them (`DUPLICATE`, no
second live order), and `apply_strategy_delta` with the now-stale `expected_version` is rejected
(no double-apply). A post-restart cancel uses `store.resolve_order_id(client_order_id)` (STATE-001 R2)
rather than the in-process `TossOrderAdapter._submitted` map alone.

## Safety Gating — Reused, Not Re-Implemented (REQ-RUNNER-001-R5, FD5)

The Runner adds **no** new guard. It threads the operational `dry_run` / `kill_switch` and the
`max_position_pct` clamp inputs into `OrderManager.place`, which already enforces the ORDER-001 R4
ordered guards (`src/ballast/orders/guards.py`): kill-switch precedence (block-all) →
`max_position_pct` clamp/block → dry-run gate (record-only). Consequences asserted by the ACs:

- kill-switch engaged ⇒ every order `BLOCKED`, no ledger upsert, no live call;
- `dry_run` true ⇒ record-only, no `place_order`/`cancel_order`, no Toss network;
- over-cap BUY ⇒ clamped (or blocked) by the existing `position_cap_guard`, unchanged.

## Lease + Single-Writer (REQ-RUNNER-001-R3, FD3)

- DECIDED (Decision 3, option A): the lease `owner` is a configured `WORKER_ID` environment value, with
  a `f"{hostname}:{pid}"` fallback when `WORKER_ID` is unset — a stable identity so a redeploy can
  reclaim / observe its own lease.
- DECIDED (Decision 4, option A): the lease is **acquired once at worker startup and held across
  cycles** (renewed via `ttl`), not acquired/released per cycle — the worker is the sole writer for its
  whole life and the Scheduler calls `run_one_cycle` repeatedly under the already-held lease.
- `acquire_writer_lease(owner, *, ttl) -> Lease` is called before any mutation/submission; a second
  concurrent acquire raises `WriterLeaseHeldError` (a stray second process cannot become a second
  writer). `release_writer_lease(lease)` on shutdown; the `ttl` lets a crashed owner's lease expire so a
  restarted worker can re-take it.
- Every mutating store call (`apply_strategy_delta`, `upsert_order`, `set_config`) additionally carries
  `expected_version` (STATE-001 optimistic concurrency) — belt-and-suspenders with the lease.
- Reads (`get_config`, `load_strategy_state`, `load_order`, `list_open_orders`) require **no** lease, so
  a dashboard reads freely.

## Resolved Design Decisions (DECIDED 2026-06-26 — all option A)

Four choices were genuinely user-resolvable. They are now **RESOLVED by orchestrator/user — all chose
option A**. The full option analysis is retained below for traceability; the chosen option is marked
**SELECTED**. The decisions target the HANDOFF §3 topology (one always-on worker = sole writer,
Streamlit = decoupled dashboard, State Store between them).

### Decision 1 — Cycle granularity: one strategy per call vs all strategies per call — DECIDED: A

- **Option A (SELECTED) — one strategy + account per `run_one_cycle` call.** `run_one_cycle` takes a
  single `strategy` (one `ns` / `account_seq`). The future Scheduler decides which strategy to run on
  which cadence (MAB daily / VR cycle / LOC at the close). Matches `Strategy.cadence` and the per-account
  isolation (`account_seq`); keeps the unit small and re-entrant. **Decision basis (industry norm):**
  Freqtrade runs one strategy per bot instance with per-instance capital allocation (maps 1:1 to
  ballast's `account_seq` isolation); NautilusTrader keeps each strategy an independent-state actor with
  the engine/loop dispatching (= the Runner "how" / Scheduler "when·which" separation).
- **Option B (rejected) — all configured strategies in one call.** `run_cycle()` iterates `vr` + `mab`
  internally. Simpler for a naive loop, but the cadences differ (MAB daily vs VR cycle), so the Scheduler
  must pass a filter anyway — coupling that Option A avoids.

### Decision 2 — Config source-of-truth precedence: State Store snapshot vs YAML/env — DECIDED: A

- **Option A (SELECTED) — snapshot authoritative at runtime, seeded once from YAML/env.** Secrets via
  env (unchanged, `from_env`); strategy knobs via YAML `Config`; the operational `RunnerConfig` is seeded
  into the durable snapshot on first boot (`get_config().version == 0`), and thereafter the State Store
  snapshot wins so the dashboard can durably flip `dry_run` / kill-switch and the worker observes it next
  cycle.
- **Option B (rejected) — YAML/env authoritative on every boot.** The snapshot is a mirror only. Simpler,
  but the dashboard cannot durably flip the kill-switch (a redeploy would revert it) — defeating the
  decoupling.

### Decision 3 — Lease owner-identity scheme — DECIDED: A

- **Option A (SELECTED) — configured `WORKER_ID` (env) with a `host:pid` fallback.** A stable identity
  lets a redeploy reclaim / observe its own lease; `ttl` handles a crashed owner.
- **Option B (rejected) — `host:pid` only.** Zero config, but a redeploy changes identity (still fine
  with `ttl`, but less observable). Retained as the _fallback_ when `WORKER_ID` is unset.
- **Option C (rejected) — a per-process UUID.** Always unique, but opaque and non-reusable across a
  restart.

### Decision 4 — Lease hold lifetime: hold for the worker lifetime vs per cycle — DECIDED: A

- **Option A (SELECTED) — acquire once at worker startup, hold across cycles (renew via `ttl`).** The
  worker IS the sole writer for its whole life; the Scheduler calls `run_one_cycle` repeatedly under the
  already-held lease. No per-cycle churn, no acquire/release race window.
- **Option B (rejected) — acquire/release per cycle.** More defensive in theory, but adds churn and a
  window where no one holds the lease between cycles.

## Decimal / Purity / Secrets Policy

- All operational money (`account_capital`, `allocation` ratios) is `Decimal` (2 dp) via
  `quantize_money`; `float` is rejected on the money path (reuse the `_reject_float` BeforeValidator like
  CORE / ORDER / STATE models). The snapshot serializes money as canonical decimal **strings** and
  round-trips losslessly back to `Decimal` (STATE-001 R5) — never a binary float.
- `run_one_cycle` reads **no** wall clock and does **no** network IO of its own: `now` / `cycle_key`,
  the `Market` snapshot (`is_open` / `is_holiday`), the `StateStorePort`, and the `BrokerOrderPort` are
  injected. The only IO happens inside the injected store backend and the injected Toss adapter.
- Toss credentials (`TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET`) are read from **environment variables
  only** via the existing `from_env`; the Runner never embeds, logs, `repr`s, or commits a secret or
  token. The State Store DB credential (if any) is likewise env-only.

## Risk Analysis

| Risk                               | Description                                                                  | Mitigation                                                                                                                             |
| ---------------------------------- | ---------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Forking the config contract        | A new typed config drifts from `Config` / `ConfigSnapshotRecord`.            | `RunnerConfig` is a **lens** over the STATE-001 snapshot (FD1/FD2); strategy knobs stay in `Config`; round-trip test asserts equality. |
| Double submission on re-run        | A Scheduler re-calls `run_one_cycle` for the same window and double-submits. | Deterministic `client_order_id` (ORDER-001) + durable ledger (STATE-001); re-run returns `DUPLICATE`; idempotency AC.                  |
| Double-applied state delta         | A re-run double-applies VR `V_n`.                                            | `apply_strategy_delta` carries `expected_version`; a stale re-apply raises `ConcurrencyError`; AC asserts the no-op.                   |
| Two writers                        | A stray second worker mutates state concurrently.                            | `acquire_writer_lease` before any mutation; second acquire raises `WriterLeaseHeldError`; AC asserts rejection.                        |
| Bypassed safety guards             | The Runner submits live despite kill-switch / `dry_run`.                     | Reuse `OrderManager.place` guards (R5); the Runner adds no second path; ACs assert BLOCKED / record-only / no Toss call.               |
| Embedded IO / wall-clock read      | The Runner reads `datetime.now` or calls the network directly.               | Inject `now` / `cycle_key` / `Market` / `store` / `port`; AC asserts no clock read and no IO inside the Runner.                        |
| Float on money path                | A `float` `account_capital` / ratio silently corrupts precision.             | `_reject_float` + `quantize_money`; snapshot money as decimal strings; AC asserts float construction is rejected.                      |
| Stale config / dashboard race      | The worker keeps a stale `dry_run` after the dashboard flips it.             | Re-read `get_config()` each cycle (snapshot authoritative, Decision 2 = A); `set_config` `expected_version` guards the write.          |
| Cross-process cancel after restart | `cancel_order` fails post-restart (in-process map empty).                    | Resolve via the persisted ledger `resolve_order_id` (STATE-001 R2), not the in-process map alone (R6).                                 |
| Secret leak                        | A Toss token / DB credential is logged or committed.                         | Env-only via `from_env`; redacted `repr`; the Runner never embeds a secret; AC asserts no secret in `repr` / logs.                     |
| Scope creep                        | Tempting to build the Scheduler / Notifier / dashboard / deploy here.        | Scope / Dependencies fence them as downstream; this SPEC ships only `RunnerConfig` + `run_one_cycle` over existing contracts.          |

## Test Approach (per EARS module, in-memory + recording port first)

- **R1 (`test_config.py`)**: `RunnerConfig` constructs from `Decimal` / decimal strings; a bare `float`
  `account_capital` / ratio is rejected; `to_snapshot()` emits money as decimal strings and the AC-18
  shape; `from_snapshot(to_snapshot())` round-trips to an equal `RunnerConfig` (lossless).
- **R2 (`test_runner.py`)**: first boot (`get_config().version == 0`) seeds the snapshot via `set_config`;
  a later cycle reads `get_config()` and reconstructs `RunnerConfig`; a dashboard-written `kill_switch`
  flip is observed on the next cycle; `set_config` with a stale `expected_version` raises.
- **R3 (`test_runner.py`)**: `acquire_writer_lease` is called before any mutation/submission; a second
  concurrent acquire raises `WriterLeaseHeldError`; reads need no lease; `release_writer_lease` releases.
- **R4 (`test_runner.py`)**: `run_one_cycle` loads `State`, calls a fake `Strategy.plan_orders`
  (returning a fixed `PlanResult`), drives `OrderManager.place` with the injected `RecordingBrokerOrderPort`,
  upserts each non-dry-run result into the `InMemoryStateStore` ledger, applies the `state_delta`
  (VR `{"V_n":..}` bumps the version; MAB `{}` no-op), and returns an inspectable `CycleResult`; the
  Runner reads no clock and does no IO (only the injected store/port do).
- **R5 (`test_runner.py`)**: `kill_switch=True` ⇒ every result `BLOCKED`, no ledger upsert, no port call;
  `dry_run=True` ⇒ record-only, no `place_order`, no Toss network; an over-cap BUY is clamped/blocked by
  the existing guard threaded through `OrderManager`.
- **R6 (`test_runner.py`)**: re-running `run_one_cycle` for the same `(ns, cycle_key, market, cfg)` yields
  the **same** `client_order_id`s, returns `DUPLICATE` (no second live order on the recording port),
  rejects the stale `apply_strategy_delta` (no double-apply), and `resolve_order_id` returns the persisted
  `broker_order_id` after a simulated restart.

Inject the `Market`, `cycle_key`, lease `owner` / `ttl`, the `InMemoryStateStore`, and the
`RecordingBrokerOrderPort` (or a mock `BrokerOrderPort`) into every test for determinism — no real Toss
network, no real DB, no credentials.

## Milestones (priority-ordered, no time estimates)

- **Primary Goal (Priority High)**: the typed `RunnerConfig` + `to_snapshot` / `from_snapshot`
  round-trip (`R1`), and config load / first-boot seed / persist through the `StateStorePort` (`R2`).
- **Secondary Goal (Priority High)**: `run_one_cycle` assembling `Strategy.plan_orders` →
  `OrderManager.place` → the injected `BrokerOrderPort`, persisting the `state_delta` (`apply_strategy_delta`)
  and the ledger (`upsert_order`), returning `CycleResult` (`R4`), with the single-writer lease acquired
  before any mutation/submission (`R3`).
- **Final Goal (Priority Medium)**: the kill-switch / `dry_run` / `max_position_pct` gating threaded
  through the reused ORDER-001 guards (`R5`), and the idempotent / re-entrant re-run + cross-process
  cancel resolution (`R6`).
- **Optional Goal (Priority Low)**: a thin `from_env`-backed assembly helper (build `RunnerConfig` +
  `TossOrderAdapter` + a chosen `StateStorePort` backend into a ready worker, using the DECIDED
  `WORKER_ID`-env lease owner and acquire-once-hold lifetime); richer `CycleResult` inspection for the
  future Notifier/dashboard.

## Quality Gates (per Constitution)

- `ruff check .` → 0 errors; `ruff format --check .` clean.
- `mypy --strict src` → 0 errors.
- `pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% overall; `src/ballast/app/**`
  targets ~100% (it is pure coordination over mockable ports).
- TDD RED→GREEN→REFACTOR against the `InMemoryStateStore` + `RecordingBrokerOrderPort` (no real Toss
  network, no real DB, no credentials). The clock / market-hours are injected; the Runner reads no wall
  clock and embeds no IO. Secrets are env-only (via `from_env`); never logged or committed.
- The four design decisions (cycle granularity, config source-of-truth, lease owner-identity, lease
  lifetime) are **RESOLVED** (all option A — see "Resolved Design Decisions") and the implementation
  follows them: single-strategy-per-call `run_one_cycle`, State-Store-authoritative config (seed-once),
  `WORKER_ID`-env lease owner with `host:pid` fallback, and acquire-once-hold-across-cycles lease
  lifetime.
