# SPEC-STATE-001 — Acceptance Criteria

`@SPEC:SPEC-STATE-001` `@TEST:SPEC-STATE-001`

> All scenarios are validated by **in-memory unit tests** (no real DB, no network, no credentials). The
> persistent backend is exercised only **in-process** (a SQLite temp file) via the shared conformance
> suite — **never against a live cloud DB in CI**. The pure reconciliation core reads no clock and does
> no IO (fills/statuses/snapshot/`ts` injected). Money/quantity values are `Decimal` (2 dp); a bare
> `float` is rejected on the money path; `Decimal` is serialized losslessly as a string. Secrets / DB
> credentials are env-only and never logged.

## Definition of Done

- All scenarios below pass in-memory; the SQLite backend passes the same shared conformance suite over a
  temp file.
- `ruff` clean, `ruff format --check` clean, `mypy --strict src` → 0 errors.
- Coverage ≥ 85% for `src/ballast/state/**`; the port + in-memory impl + pure reconciliation target
  ~100%.
- No live cloud DB in CI; SQLite/Postgres exercised in-process or mocked only.
- No wall-clock read and no network IO inside `src/ballast/state/reconcile.py` (fills/statuses/`ts`
  injected); only the store backends touch IO.
- The persistent backend choice (SQLite vs Postgres) is confirmed with orchestrator/user before
  implementation (RECOMMENDED: SQLite-on-volume).

---

## Strategy-State Persistence + Apply-Delta Scenarios (R1)

### AC-1 — persist + reload strategy state across a simulated restart (R1)

```gherkin
Given an empty store and ns="vr"
When load_strategy_state("vr") is called
Then it returns StrategyStateRecord(ns="vr", data={}, version=0)

Given apply_strategy_delta("vr", {"V_n": Decimal("1050.00")}, expected_version=0) has been applied
When the store is "restarted" (a fresh store instance over the same backing, or a re-read)
And load_strategy_state("vr") is called
Then it returns data["V_n"] == Decimal("1050.00") (durable across restart)
And the value is a Decimal (not a float) normalized to 2 dp
```

### AC-2 — apply PlanResult delta advances V_n durably, exactly like the backtest engine (R1)

```gherkin
Given load_strategy_state("vr") has version == 0 and data {"V_n": Decimal("1000.00")}
And a VR PlanResult.state_delta == {"V_n": Decimal("1042.37")}  # recomputed value line
When apply_strategy_delta("vr", delta, expected_version=0) is called
Then it merges (delta-only, not whole-record overwrite) and returns new_version == 1
And a subsequent load_strategy_state("vr") shows data["V_n"] == Decimal("1042.37") (read-your-writes)
And the result matches the backtest engine _apply_state_delta behavior for {"V_n": V2}
```

### AC-3 — unknown delta keys are ignored (forward-compatible) (R1)

```gherkin
Given ns="vr"
When apply_strategy_delta("vr", {"V_n": Decimal("900.00"), "future_key": Decimal("7")}, expected_version=...) is called
Then "V_n" is applied (2-dp quantized)
And "future_key" is ignored (a future stateful strategy is non-breaking; no error)
```

### AC-4 — apply-delta with a stale expected_version is rejected (R1, R3, Unwanted)

```gherkin
Given load_strategy_state("vr") has version == 2
When apply_strategy_delta("vr", {"V_n": Decimal("1.00")}, expected_version=1) is called   # stale
Then it raises a clear ConcurrencyError (optimistic concurrency)
And the persisted record is unchanged (no clobber by a stale/second writer)
```

### AC-5 — MAB strategy state persists its fill-derived keys (R1)

```gherkin
Given ns="mab"
When apply_strategy_delta("mab", {"avg_price": Decimal("82.50"), "holdings": Decimal("12"),
                                  "seed_remaining": Decimal("300.00"), "round_idx": Decimal("3")},
                          expected_version=0) is called
Then load_strategy_state("mab").data round-trips those exact Decimal values (2 dp where applicable)
And an empty delta {} applied to MAB is a no-op (its state is otherwise advanced by reconciliation)
```

---

## Order / Idempotency Ledger + Cross-Process Cancel Scenarios (R2)

### AC-6 — order ledger persists client_order_id → orderId and resolves it (R2)

```gherkin
Given upsert_order("vr-20260626-abc", account_seq="acc-1", broker_order_id="toss-9001",
                   status=OrderState.SUBMITTED, ordered_qty=Decimal("3"), filled_qty=Decimal("0"), ts=...) has run
When resolve_order_id("vr-20260626-abc") is called
Then it returns "toss-9001"
And load_order("vr-20260626-abc").status == OrderState.SUBMITTED
```

### AC-7 — cross-process cancel: resolve client_order_id → orderId AFTER a restart (R2)

```gherkin
Given an order ("mab-c0007-def" → "toss-9100", SUBMITTED) was persisted by the worker before a crash
When the worker is "restarted" (a fresh store + a fresh TossOrderAdapter whose in-process map is empty)
And cancel resolution calls resolve_order_id("mab-c0007-def")
Then it returns "toss-9100" (the in-process map alone would return None ⇒ FAILED)
So a cross-process / post-restart cancel_order can succeed
   (this directly resolves the SPEC-ADAPTER-002 cross-process follow-up)

Given an unknown client_order_id "nope-000"
When resolve_order_id("nope-000") is called
Then it returns None (no guessing of an orderId)
```

### AC-8 — ledger status advances monotonically and never regresses (R2)

```gherkin
Given load_order("k1").status == OrderState.SUBMITTED
When upsert_order("k1", ..., status=OrderState.FILLED, filled_qty == ordered_qty, ...) is called
Then load_order("k1").status == OrderState.FILLED (terminal)
When a later upsert_order("k1", ..., status=OrderState.SUBMITTED, ...) is attempted
Then the terminal FILLED status is NOT regressed (monotonic lifecycle)
And a same-status re-upsert is a no-op (idempotent)
```

---

## Single-Writer / Restart-Safe Scenarios (R3)

### AC-9 — single-writer lease: a second writer is rejected (R3)

```gherkin
Given acquire_writer_lease(owner="worker-A", ttl=...) has granted the lease to worker-A
When acquire_writer_lease(owner="worker-B", ttl=...) is called while A holds it
Then it fails (WriterLeaseHeldError) — worker-B cannot become a second writer
When worker-A releases the lease (or it expires by ttl)
Then acquire_writer_lease(owner="worker-B", ...) now succeeds
And reads (load_strategy_state / load_order / get_config) never require the lease
```

### AC-10 — writes are atomic and read-your-writes (R3)

```gherkin
Given any successful mutating call (apply_strategy_delta / upsert_order / set_config)
When a read of the same record is performed immediately after
Then the read observes the just-written value (read-your-writes)
And a crash mid-write leaves either the old or the new record, never a torn record (atomic)
And after a restart, reloading strategy state + order ledger + config snapshot returns the last
    durably committed values (restart-safety)
```

---

## Reconciliation Scenarios (R4)

### AC-11 — reconciliation updates a MAB position + ledger from a BUY fill, matching the engine (R4)

```gherkin
Given a persisted MAB state {avg_price: Decimal("80.00"), holdings: Decimal("10"),
                             seed_remaining: Decimal("500.00"), round_idx: Decimal("2")}
And a submitted ledger order ("mab-c1" → toss-1, SUBMITTED, ordered_qty=Decimal("5"))
And a BUY FillRecord(client_order_id="mab-c1", side=BUY, qty=Decimal("5"), price=Decimal("84.00"), ts=...)
When reconcile(snapshot, fills=[that fill], order_statuses={}) is called  (pure; no IO, no clock)
Then the ledger order "mab-c1" becomes OrderState.FILLED (filled_qty == ordered_qty)
And the MAB state updates to:
    avg_price == quantize_money((80.00*10 + 84.00*5)/15)        # volume-weighted, == engine _apply_buy
    holdings  == Decimal("15")
    seed_remaining == quantize_money(max(0, 500.00 - (84.00*5 + commission)))
    round_idx == Decimal("3")
And every value is Decimal (no float)
```

### AC-12 — reconciliation is idempotent: re-running over the same fills converges (R4)

```gherkin
Given the BUY fill from AC-11 has already been reconciled (recorded in applied_fills at its watermark)
When reconcile(snapshot, fills=[the SAME fill], order_statuses={}) is run again
Then the fill is NOT applied a second time (no double-count: holdings stays 15, not 20)
And the ledger status stays FILLED (no regression)
And the result is a no-op beyond the first application (convergence)
```

### AC-13 — reconciliation marks a SELL fill and a CANCELED order (R4)

```gherkin
Given a persisted MAB state {avg_price: Decimal("84.00"), holdings: Decimal("15"), ...}
And a SELL FillRecord(client_order_id="mab-sell", side=SELL, qty=Decimal("15"), price=Decimal("90.00"))
When reconcile(...) is called
Then holdings -> Decimal("0") and avg_price -> Decimal("0") at flat (== engine _apply_sell)
And the "mab-sell" ledger order becomes FILLED

Given a SUBMITTED-but-unfilled order "vr-stale" and order_statuses={"toss-stale": "CANCELED"}
When reconcile(snapshot, fills=[], order_statuses={"toss-stale": "CANCELED"}) is called
Then the "vr-stale" ledger order transitions to OrderState.CANCELED (reconciled against broker truth)
```

### AC-14 — VR fill updates qty/pool but leaves V_n to the apply-delta channel (R4, R1)

```gherkin
Given a persisted VR state {V_n: Decimal("1000.00"), pool: Decimal("500.00"), qty: Decimal("4")}
And a BUY FillRecord for the VR account (qty=Decimal("2"), price=Decimal("100.00"))
When reconcile(...) is called
Then qty and pool are updated to the post-fill position (qty -> 6, pool debited)
And V_n is NOT changed by reconciliation (V_n advances only via apply_strategy_delta, R1)
And the VR ledger order/cycle is marked reconciled
```

### AC-15 — the reconciliation core is pure (no clock, no IO) (R4, Unwanted)

```gherkin
Given reconcile(snapshot, fills, order_statuses)
Then it reads no wall clock (datetime.now is never called)
And it performs no network or filesystem IO
And it returns a ReconResult (new snapshot + inspectable mutations) computed purely from its arguments
And the caller (not the pure core) persists the mutations via the store (atomic, R3)
```

---

## In-Memory Store + Config Snapshot + Decimal / Secrets Scenarios (R5)

### AC-16 — InMemoryStateStore structurally satisfies StateStorePort and needs no DB (R5)

```gherkin
Given an InMemoryStateStore instance
Then isinstance(store, StateStorePort) is True   # runtime_checkable Protocol
And it requires NO database, credentials, base URL, or network access
And it honors the same lease / optimistic-concurrency / apply-delta / atomic / read-your-writes
    semantics as a persistent backend (verified by the shared conformance suite)
```

### AC-17 — in-memory ↔ SQLite backend parity (shared conformance suite) (R5)

```gherkin
Given the shared conformance suite parametrized over {InMemoryStateStore, SqliteStateStore(temp_file)}
When every R1-R3/R5 scenario is run against each backend
Then both backends produce identical observable behavior
And the SQLite backend runs entirely in-process over a temp file (WAL); NO live cloud DB is contacted
```

### AC-18 — generic config snapshot persists worker↔dashboard shared settings (R5)

```gherkin
Given set_config({"ticker": "QLD", "account_seq": "acc-1", "allocation": {"vr": "0.6", "mab": "0.4"},
                  "dry_run": True, "kill_switch": False}, expected_version=0) is called
When get_config() is called
Then it returns the same generic mapping (money values as decimal strings) at version == 1
And this SPEC does NOT define the typed Config model (that is the Runner SPEC's concern)
And a set_config with a stale expected_version raises ConcurrencyError (single-writer-safe)
```

### AC-19 — Decimal is lossless; float is rejected on the money path (R1, R5, Decimal policy)

```gherkin
Given any persisted record carrying money/quantity (StrategyStateRecord / OrderLedgerRecord / FillRecord)
When a value would be a Python float (e.g. qty=84.5 instead of Decimal("84.50"))
Then construction rejects it (no silent float coercion onto the money path)
And valid Decimal values normalize to 2 dp via quantize_money
And persisting then reloading a Decimal returns an equal Decimal (lossless string round-trip, never binary float)
```

### AC-20 — secrets / DB credentials are env-only and never logged (R5, Unwanted)

```gherkin
Given a persistent backend configured from the environment (e.g. DATABASE_URL via from_env, or a
      config/env-driven SQLite file path)
Then no credential or connection string is committed to the repo
And no credential appears in any log line, exception message, or DTO/store repr
And the in-memory store and the pure reconciliation core require no secret at all
```

---

## Downstream / Out-of-Scope Note (NOT an acceptance gate here)

The Runner / typed Config model, Scheduler, Notifier, Streamlit dashboard, and deployment are downstream
consumers (later SPECs). This SPEC ships only the `StateStorePort`, persisted DTOs, the in-memory store,
one persistent backend (flagged for review), and the pure reconciliation core — shaped to support those
consumers (single-writer worker, restart-safe, swappable backend) but implementing none of them.

## Quality Gates

- Coverage ≥ 85% (`pytest --cov=src/ballast`); port + in-memory impl + pure reconciliation target ~100%.
- `mypy --strict src` → 0 errors; `ruff` (lint + format) clean.
- Unit tests run against the in-memory store only; the SQLite backend is integration-tested in-process
  (temp file); no live cloud DB in CI.
- `Decimal` lossless (string round-trip); `float` rejected on the money path; secrets/DB creds env-only.

## Traceability

- `@SPEC:SPEC-STATE-001` → `spec.md`
- `@TEST:SPEC-STATE-001` → `tests/unit/state/{test_models,test_reconcile,test_memory,test_store_conformance,test_sqlite}.py`
- `@CODE:SPEC-STATE-001` → `src/ballast/state/{ports,models,memory,reconcile}.py` + the chosen backend (`sqlite.py` or `pg.py`)
