# SPEC-RUNNER-001 — Acceptance Criteria

`@SPEC:SPEC-RUNNER-001` `@TEST:SPEC-RUNNER-001`

> All scenarios are validated by **unit tests** against the STATE-001 `InMemoryStateStore` and the
> ORDER-001 `RecordingBrokerOrderPort` (or a mock `BrokerOrderPort`) — **no real Toss network, no real
> DB, no credentials**. The Runner reads **no** wall clock and embeds **no** IO: the clock
> (`now`/`cycle_key`), the `Market` snapshot (with `is_open`/`is_holiday`), the `StateStorePort`, and
> the `BrokerOrderPort` are injected. Money/quantity values are `Decimal` (2 dp via `quantize_money`); a
> bare `float` is rejected on the money path; the config snapshot serializes money as decimal strings.
> Secrets (`TOSS_CLIENT_ID`/`SECRET`) are env-only and never logged. The Runner **reuses** the ORDER-001
> guards and **forks no** existing contract.

## Definition of Done

- All scenarios below pass against the in-memory store + recording port.
- `ruff` clean, `ruff format --check` clean, `mypy --strict src` → 0 errors.
- Coverage ≥ 85% for `src/ballast/app/**`; the Runner + typed config target ~100%.
- No real Toss network, no real DB, no credentials in CI; the live path is exercised only via the
  recording / mock `BrokerOrderPort`.
- No wall-clock read and no network IO inside `src/ballast/app/**` (clock / market-hours / store / port
  injected); only the injected store backend and Toss adapter touch IO.
- The four design decisions (cycle granularity, config source-of-truth precedence, lease owner-identity,
  lease lifetime) are **RESOLVED** (all option A — see `spec.md` "Resolved Design Decisions"): one
  strategy per `run_one_cycle` call / State Store snapshot authoritative (seed-once) / configured
  `WORKER_ID` env owner with `host:pid` fallback / acquire-once-hold-across-cycles lease lifetime.

---

## Typed RunnerConfig + Config / Snapshot Relationship Scenarios (R1)

### AC-1 — RunnerConfig carries the operational fields with Decimal money (R1)

```gherkin
Given RunnerConfig(ticker="QLD", account_capital=Decimal("10000.00"),
                   allocation={"vr": Decimal("0.6"), "mab": Decimal("0.4")},
                   dry_run=True, kill_switch=False)
Then account_capital is a Decimal normalized to 2 dp (10000.00)
And the allocation ratios are Decimal values
And the model is frozen and rejects unknown keys (extra="forbid")
And it does NOT redefine VR/MAB strategy knobs, instruments, or account_seq
    (those remain in ballast.core.config.Config — no fork)
```

### AC-2 — RunnerConfig round-trips losslessly through ConfigSnapshotRecord (R1, R2)

```gherkin
Given a RunnerConfig as in AC-1
When to_snapshot() is called
Then it returns the generic STATE-001 shape with money as canonical decimal STRINGS:
    {"ticker": "QLD", "account_capital": "10000.00",
     "allocation": {"vr": "0.6", "mab": "0.4"}, "dry_run": True, "kill_switch": False}
And no value on the money path is a binary float
When from_snapshot(to_snapshot()) is called
Then it returns a RunnerConfig EQUAL to the original (lossless inverse)
And the Decimal money values are recovered exactly
```

### AC-3 — a bare float on the money path is rejected (R1, Decimal policy)

```gherkin
Given a RunnerConfig constructor input where account_capital is a Python float (e.g. 10000.5)
Then construction is rejected (no silent float coercion onto the money path)
Given an allocation ratio supplied as a Python float (e.g. {"vr": 0.6})
Then construction is rejected
And valid string / Decimal money values are accepted and normalized to 2 dp where applicable
```

---

## Config Load / Seed / Persist via the State Store Scenarios (R2)

### AC-4 — first boot seeds the snapshot once from the typed config (R2)

```gherkin
Given an InMemoryStateStore whose get_config().version == 0 (empty snapshot)
And a RunnerConfig built from YAML/env
When the worker boots
Then it calls set_config(cfg.to_snapshot(), expected_version=0) exactly once
And get_config() now returns the seeded snapshot at version == 1
```

### AC-5 — the snapshot is the runtime authority; a dashboard flip is observed next cycle (R2)

```gherkin
Given a seeded snapshot with dry_run == True, kill_switch == False
When a dashboard-style writer calls set_config(updated, expected_version=current) flipping kill_switch -> True
And the worker starts its next cycle and reads RunnerConfig.from_snapshot(get_config().data)
Then the worker observes kill_switch == True (the State Store snapshot wins at runtime)
And it does NOT fall back to the YAML Config value for the operational fields after the first-boot seed
```

### AC-6 — persisting a config change uses optimistic concurrency (R2, R3)

```gherkin
Given get_config().version == 2
When set_config(snapshot, expected_version=1) is attempted   # stale
Then it raises a clear ConcurrencyError (single-writer-safe)
And the persisted snapshot is unchanged
And money values written to the snapshot are decimal strings (never a binary float)
```

---

## Single-Writer Lease Scenarios (R3)

### AC-7 — the lease is acquired before any mutation or submission (R3)

```gherkin
Given an InMemoryStateStore and a worker about to run a cycle
When the worker mutates strategy state, upserts the order ledger, or submits a live order
Then it has already acquired the writer lease via acquire_writer_lease(owner, ttl=...)
And no apply_strategy_delta / upsert_order / live place_order happens without the lease held
```

### AC-8 — a second concurrent writer is rejected; reads need no lease (R3, Unwanted)

```gherkin
Given acquire_writer_lease(owner="worker-A", ttl=...) has granted the lease to worker-A
When acquire_writer_lease(owner="worker-B", ttl=...) is called while A holds it
Then it fails with WriterLeaseHeldError — worker-B cannot become a second writer
When worker-A releases the lease (release_writer_lease) or it expires by ttl
Then acquire_writer_lease(owner="worker-B", ...) now succeeds
And reads (get_config / load_strategy_state / load_order / list_open_orders) never require the lease
```

---

## Run One Cycle: Strategy → Order Manager → Toss Adapter Scenarios (R4)

### AC-9 — one cycle assembles the existing parts and persists the result (R4)

```gherkin
Given a fake Strategy(ns="vr") whose plan_orders(market, state, cfg) returns
      PlanResult(orders=(one BUY Order,), state_delta={"V_n": Decimal("1042.37")})
And an injected Market (current_price, fx_rate, is_open=True, is_holiday=False)
And an injected cycle_key, an InMemoryStateStore, and a RecordingBrokerOrderPort
And RunnerConfig with dry_run=False, kill_switch=False
When run_one_cycle(strategy, market, cfg=cfg, cycle_key=..., store=store, port=port, account_inputs=...) runs
Then it loads State for ns="vr" from the store
And it calls strategy.plan_orders(...) exactly once (pure; market is the only source of now/price)
And it drives OrderManager.place(...) which calls the RecordingBrokerOrderPort once for the BUY
And it upserts the SUBMITTED SubmissionResult into the STATE-001 order ledger (upsert_order)
And it calls apply_strategy_delta("vr", {"V_n": Decimal("1042.37")}, expected_version=<loaded>) bumping the version
And it returns a CycleResult(ns="vr", plan=<OrderPlan>, state_delta={"V_n": ...}, new_version=<bumped>, ledger_upserts=(...))
```

### AC-10 — the clock and market-hours are injected; the Runner reads no clock and does no IO (R4, Unwanted)

```gherkin
Given run_one_cycle(...) with injected market, cycle_key, store, and port
Then the Runner reads no wall clock (datetime.now is never called inside src/ballast/app/**)
And the Runner performs no network or filesystem IO of its own
And the only "now" / market-hours come from the injected Market (is_open / is_holiday) and cycle_key
And the only IO happens inside the injected store backend and the injected BrokerOrderPort
```

### AC-11 — MAB's empty state_delta is a no-op on apply (R4)

```gherkin
Given a fake Strategy(ns="mab") whose plan_orders returns PlanResult(orders=(...), state_delta={})
When run_one_cycle(strategy, ...) runs
Then apply_strategy_delta("mab", {}, expected_version=<loaded>) is a no-op for the persisted data
    (MAB's state is fill-derived, advanced by STATE-001 reconciliation, not by a strategy delta)
And the orders still flow through OrderManager and the ledger as usual
```

---

## Safety Gating (Reused Guards) Scenarios (R5)

### AC-12 — the kill-switch blocks every order; nothing is submitted or upserted (R5, Unwanted)

```gherkin
Given RunnerConfig.kill_switch == True
When run_one_cycle(strategy, ...) runs with orders to place
Then every SubmissionResult is BLOCKED with a kill-switch reason (the reused OrderManager guard)
And the RecordingBrokerOrderPort.place_order is NEVER called
And no order is upserted into the ledger for a blocked order
And the Runner adds no second, divergent safety path
```

### AC-13 — dry_run takes the record-only path with no Toss network (R5, State-driven)

```gherkin
Given RunnerConfig.dry_run == True (the shipped default)
When run_one_cycle(strategy, ...) runs
Then OrderManager records the OrderPlan (status RECORDED) and calls NO place_order / cancel_order
And the RecordingBrokerOrderPort is never called (no network, even with a port injected)
And the cycle still returns an inspectable CycleResult / OrderPlan preview
```

### AC-14 — max_position_pct is threaded into the reused clamp/block guard (R5)

```gherkin
Given dry_run=False and injected account_inputs (base_values, positions, prices)
And a BUY whose resulting position would exceed cfg.execution.max_position_pct
When run_one_cycle(...) runs
Then the order is clamped (qty reduced, re-quantized) or blocked by the existing position_cap_guard
    threaded through OrderManager.place — unchanged from ORDER-001
And the Runner re-implements no clamp logic of its own
```

---

## Idempotent / Re-Entrant One-Cycle Scenarios (R6)

### AC-15 — re-running the same cycle does not double-submit (R6, Unwanted)

```gherkin
Given run_one_cycle(strategy, market, cfg=cfg, cycle_key="vr-20260626", ...) has run once
      (live, dry_run=False) and submitted one order
When run_one_cycle is invoked AGAIN for the SAME (ns, cycle_key, market, cfg)
Then the same deterministic client_order_id is derived (ORDER-001 FD3)
And the second submission returns DUPLICATE — NO second distinct live order is created on the port
And the durable STATE-001 ledger still holds a single entry for that client_order_id
```

### AC-16 — re-running does not double-apply the state delta (R6, R3)

```gherkin
Given the first run_one_cycle applied {"V_n": V2} at expected_version=0 (now version==1)
When run_one_cycle is re-invoked and re-attempts apply_strategy_delta("vr", {"V_n": V2}, expected_version=0)  # stale
Then it raises ConcurrencyError and the persisted V_n is NOT advanced a second time
And the cycle is a no-op beyond the first application (convergence)
```

### AC-17 — cross-process cancel resolves via the persisted ledger after a restart (R6)

```gherkin
Given an order ("mab-c0007-...", broker_order_id="toss-9100", SUBMITTED) was persisted by the worker
When the worker is "restarted" (a fresh store handle + a fresh TossOrderAdapter whose in-process map is empty)
And a cancel resolution calls store.resolve_order_id("mab-c0007-...")
Then it returns "toss-9100" so a cross-process / post-restart cancel can succeed
    (the in-process TossOrderAdapter map alone would return None ⇒ FAILED)
And an unknown client_order_id resolves to None (no guessing)
```

---

## Decimal / Secrets Scenarios (R1, R5)

### AC-18 — secrets are env-only and never logged or in a repr (Unwanted)

```gherkin
Given the Toss adapter is assembled from the environment via the existing from_env (TOSS_CLIENT_ID/SECRET)
Then no Toss credential or token is embedded in source, a log line, an exception, or any DTO/Runner repr
And the typed RunnerConfig and CycleResult carry no secret
And the Runner requires no broker credential to construct a RunnerConfig or to run a dry-run cycle
```

---

## Downstream / Out-of-Scope Note (NOT an acceptance gate here)

The Scheduler (cadence / DST / market-hours triggering), the Notifier (ntfy / Discord), the Streamlit
dashboard, and deployment are downstream consumers (later SPECs). This SPEC ships only the typed
`RunnerConfig` lens and the `run_one_cycle` assembly over existing contracts (CORE / STRATEGY / ORDER /
STATE / ADAPTER-001/002) — shaped to support those consumers (single-writer worker, injected clock /
market-hours, re-entrant cycle, snapshot-decoupled dashboard) but implementing none of them. It defines
no new broker write code, no new State Store backend, and no reconciliation routine (it calls those
existing contracts).

## Quality Gates

- Coverage ≥ 85% (`pytest --cov=src/ballast`); the Runner + typed config target ~100%.
- `mypy --strict src` → 0 errors; `ruff` (lint + format) clean.
- Unit tests run against the `InMemoryStateStore` + `RecordingBrokerOrderPort` / a mock port only; no
  real Toss network, no real DB, no credentials in CI.
- `Decimal` lossless (string round-trip in the snapshot); `float` rejected on the money path; secrets
  env-only (via `from_env`). The Runner reads no wall clock and embeds no IO.

## Traceability

- `@SPEC:SPEC-RUNNER-001` → `spec.md`
- `@TEST:SPEC-RUNNER-001` → `tests/unit/app/{test_config,test_runner}.py`
- `@CODE:SPEC-RUNNER-001` → `src/ballast/app/{config,runner}.py`
