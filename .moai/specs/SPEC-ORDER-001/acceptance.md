# SPEC-ORDER-001 — Acceptance Criteria

`@SPEC:SPEC-ORDER-001` `@TEST:SPEC-ORDER-001`

> All scenarios are validated by **in-memory unit tests** (no network, no credentials). The live
> submission path is exercised only through the in-memory `RecordingBrokerOrderPort`. **dry-run is the
> default**; no real broker write is implemented (deferred to P3 / SPEC-ADAPTER-002). Money/quantity
> values are `Decimal` (2 dp); a bare `float` is rejected on the money path.

## Definition of Done

- All scenarios below pass in-memory.
- `ruff` clean, `ruff format --check` clean, `mypy --strict src` → 0 errors.
- Coverage ≥ 85% for `src/ballast/orders/**` (core-like; target ~100%).
- No real broker write call (`POST /api/v1/orders` create / modify / cancel) is implemented.
- No wall-clock read and no network IO inside `src/ballast/orders/**` (time/prices/positions injected).

---

## Mapping & Idempotency Scenarios (R1)

### AC-1 — `reserved_limit` maps to LIMIT + DAY, carrying limit_price (R1)

```gherkin
Given a CORE Order(side=BUY, ticker="QLD", qty=Decimal("3"), limit_price=Decimal("80.00"),
                   order_type=OrderType.RESERVED_LIMIT, account_seq="acc-1")
And ns="vr" and cycle_key="2026-06-26"
When order_to_intent(order, ns="vr", cycle_key="2026-06-26") is called
Then the OrderIntent has kind == OrderKind.LIMIT and tif == Tif.DAY
And limit_price == Decimal("80.00") (Decimal, not float)
And side == Side.BUY, ticker == "QLD", qty == Decimal("3.00"), account_seq == "acc-1"
And client_order_id matches ^[a-zA-Z0-9\-_]+$ and len(client_order_id) <= 36
```

### AC-2 — `LOC` maps to LIMIT + CLS (limit_price may be None ⇒ MOC-style) (R1)

```gherkin
Given a CORE Order(side=SELL, ticker="SOXL", qty=Decimal("10"), limit_price=None,
                   order_type=OrderType.LOC, account_seq="acc-2")
When order_to_intent(order, ns="mab", cycle_key="2026-06-26") is called
Then the OrderIntent has kind == OrderKind.LIMIT and tif == Tif.CLS
And limit_price == None   # Limit-On-Close with no price ⇒ MOC-style close
And the mapping does NOT raise (None limit_price is allowed for tif=CLS)
```

### AC-3 — `market` maps to MARKET + DAY and drops limit_price (R1)

```gherkin
Given a CORE Order(side=BUY, ticker="SPY", qty=Decimal("1"), limit_price=Decimal("500.00"),
                   order_type=OrderType.MARKET, account_seq="acc-3")
When order_to_intent(order, ns="vr", cycle_key="2026-06-26") is called
Then the OrderIntent has kind == OrderKind.MARKET and tif == Tif.DAY
And limit_price == None   # MARKET carries no price; the incoming value is dropped
```

### AC-4 — LIMIT + DAY without a limit_price is rejected (R1, Unwanted)

```gherkin
Given a CORE Order(order_type=OrderType.RESERVED_LIMIT, limit_price=None, ...)
When order_to_intent(order, ns="vr", cycle_key="2026-06-26") is called
Then it raises a clear ValueError (a reserved_limit / DAY LIMIT requires a limit_price)
And no OrderIntent is produced
```

### AC-5 — client_order_id is deterministic and signature-sensitive (R1)

```gherkin
Given two identical CORE Orders with the same ns="vr" and cycle_key="2026-06-26"
When order_to_intent is called on each
Then both produce the SAME client_order_id (byte-for-byte, across processes/runs)
And no randomness, UUID, or datetime.now is used in deriving it

Given two Orders differing only in qty (Decimal("3") vs Decimal("4"))
When mapped with the same ns and cycle_key
Then they produce DIFFERENT client_order_id values

Given the same Order mapped with a different cycle_key ("2026-06-27")
Then the client_order_id differs from the "2026-06-26" one

And for every produced id: it matches ^[a-zA-Z0-9\-_]+$ and len(id) <= 36
```

---

## Dry-Run Scenarios (R2)

### AC-6 — dry-run is default-ON: records the plan, never calls the port (R2)

```gherkin
Given execution.dry_run is True (the shipped default)
And a RecordingBrokerOrderPort acting as a spy
And a list of CORE Orders to place
When OrderManager.place(orders, dry_run=True, port=spy, ...) is called
Then it returns an OrderPlan whose results all have status == SubmissionStatus.RECORDED
     (or BLOCKED if a guard blocked an order)
And spy.place_order was called ZERO times
And no network IO occurred
And each recorded intent exposes its deterministic client_order_id, mapped kind/tif/limit_price,
    and the applied guard outcome (recorded / clamped / blocked)
```

### AC-7 — dry-run mutates no external state beyond its in-memory plan (R2)

```gherkin
Given execution.dry_run is True
When OrderManager.place(...) is called twice with the same orders and cycle_key
Then neither call submits anything
And the only state change is the appended in-memory OrderPlan / ledger entries
And the produced client_order_ids are identical across the two calls (determinism)
```

---

## Live Submission Scenarios (R3)

### AC-8 — live submit calls the port once and tracks SUBMITTED (R3)

```gherkin
Given execution.dry_run is False
And a RecordingBrokerOrderPort
And one guard-passing CORE Order
When OrderManager.place(orders, dry_run=False, port=recording_port, ...) is called
Then recording_port.place_order is called exactly once with the mapped OrderIntent
And the result has status == SubmissionStatus.SUBMITTED with a non-null broker_order_id
And status_of(client_order_id) returns SUBMITTED
```

### AC-9 — idempotent dedup: re-placing the same client_order_id yields DUPLICATE (R3)

```gherkin
Given execution.dry_run is False and a RecordingBrokerOrderPort
And an Order already submitted in this cycle (its client_order_id is in the ledger as SUBMITTED)
When the same Order (same ns, cycle_key, fields ⇒ same client_order_id) is placed again
Then no second place_order call is issued for that key
And the returned result has status == SubmissionStatus.DUPLICATE
And the recording port likewise reports DUPLICATE for the repeated key (idempotent stand-in)
```

### AC-10 — port failure records FAILED with a reason and stays consistent (R3)

```gherkin
Given execution.dry_run is False and a port whose place_order raises (or returns a failure)
When OrderManager.place(orders, dry_run=False, port=failing_port, ...) is called
Then the result has status == SubmissionStatus.FAILED with a non-empty reason
And the manager does not retry forever (bounded / no infinite loop)
And a FAILED key may be retried explicitly later, while a SUBMITTED/DUPLICATE key is never re-submitted
```

---

## Safety-Guard Scenarios (R4)

### AC-11 — kill-switch blocks everything, with precedence over dry-run (R4)

```gherkin
Given the global kill-switch is engaged
And a list of CORE Orders (with either dry_run True or False)
When OrderManager.place(orders, kill_switch=engaged, ...) is called
Then every result has status == SubmissionStatus.BLOCKED with reason containing "kill-switch"
And nothing is recorded as RECORDED and nothing is submitted (port.place_order called ZERO times)
And this holds even when dry_run is True (kill-switch takes precedence over the dry-run gate)
```

### AC-12 — max_position_pct clamps an over-cap BUY down (never up) (R4)

```gherkin
Given execution.max_position_pct = Decimal("0.20") and base_value (buying power/portfolio) = Decimal("1000.00")
And current_position_value = Decimal("100.00")  # cap = 200.00, headroom = 100.00
And a BUY OrderIntent whose order_value (qty * reference_price) = Decimal("150.00") exceeds the headroom
When the position-cap guard runs
Then the order qty is clamped down so the resulting position value <= cap (<= 200.00)
And the clamped qty <= the original qty (the clamp never increases qty)
And the clamped qty is quantize_money-normalized (2 dp, floored, never rounded up)
And the result reason records the clamp (e.g. "clamped <old>-><new>")
```

### AC-13 — no headroom under max_position_pct blocks the order (R4)

```gherkin
Given execution.max_position_pct = Decimal("0.20") and base_value = Decimal("1000.00")
And current_position_value = Decimal("200.00")  # cap already met, headroom <= 0
When a BUY order is run through the position-cap guard
Then the result has status == SubmissionStatus.BLOCKED with an overflow reason
And the order is not submitted (and not recorded as RECORDED)
And a clamp that would reduce qty to 0 likewise degrades to BLOCKED
```

### AC-14 — dry-run gate forces record-only regardless of other conditions (except kill-switch) (R4)

```gherkin
Given execution.dry_run is True and the kill-switch is NOT engaged
And an order that passes (or is clamped by) the position-cap guard
When OrderManager.place(orders, dry_run=True, port=spy, ...) is called
Then the surviving order is RECORDED (or BLOCKED if the cap blocked it), never SUBMITTED
And spy.place_order is called ZERO times
```

---

## In-Memory / Recording Port Scenarios (R5)

### AC-15 — RecordingBrokerOrderPort structurally satisfies BrokerOrderPort and records intents (R5)

```gherkin
Given a RecordingBrokerOrderPort instance
Then isinstance(port, BrokerOrderPort) is True  # runtime_checkable Protocol
When place_order(intent) is called for a new client_order_id
Then it appends the full OrderIntent to recorded_intents
And returns SubmissionResult(status=SUBMITTED, broker_order_id=<synthetic, e.g. "rec-0">)
And requires NO credentials, base URL, or network access
```

### AC-16 — RecordingBrokerOrderPort is idempotent on repeated client_order_id (R5)

```gherkin
Given a RecordingBrokerOrderPort that already recorded an intent with client_order_id "vr-20260626-abc"
When place_order is called again with the same client_order_id
Then no duplicate intent is appended
And it returns the prior SubmissionResult with status == SubmissionStatus.DUPLICATE
And recorded_intents / results remain inspectable for test assertions
```

### AC-17 — float money is rejected at the boundary (R1, Decimal policy)

```gherkin
Given any OrderIntent / SubmissionResult construction path for money/quantity
When a value would be a Python float (e.g. qty=70.12 instead of Decimal("70.12"))
Then construction rejects it (no silent float coercion onto the money path)
And valid Decimal values normalize to 2 dp via quantize_money
```

---

## P3 Carry-Forward (NOT an acceptance gate here)

The Toss-mapping note (LIMIT+CLS=LOC, clientOrderId 10-minute idempotency) is documentation for the
future P3 write adapter (SPEC-ADAPTER-002). It is **not** exercised or asserted by this SPEC; P2 ships
no real broker write and submits nothing over the network.

## Quality Gates

- Coverage ≥ 85% (`pytest --cov=src/ballast`); core-like module targets ~100%.
- `mypy --strict src` → 0 errors; `ruff` (lint + format) clean.
- Live path exercised only via `RecordingBrokerOrderPort` (zero real network in the suite).
- dry-run default-ON; no `POST` create/modify/cancel implemented (deferred to P3).

## Traceability

- `@SPEC:SPEC-ORDER-001` → `spec.md`
- `@TEST:SPEC-ORDER-001` → `tests/unit/orders/{test_models,test_mapping,test_guards,test_manager_dryrun,test_manager_live,test_recording}.py`
- `@CODE:SPEC-ORDER-001` → `src/ballast/orders/{models,ports,mapping,manager,guards,recording}.py`
