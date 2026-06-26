# SPEC-ADAPTER-002 — Acceptance Criteria

`@SPEC:SPEC-ADAPTER-002` `@TEST:SPEC-ADAPTER-002`

> All scenarios are validated by **mocked-HTTP unit tests** (`httpx.MockTransport` / `respx`) using
> payloads shaped per `docs/reference/toss-openapi.json` (v1.1.5). No live network. **Live real-account
> 1-share submit/cancel "soak" is a manual user step** (see `plan.md` checklist), not an automated gate.
> Money/quantity values are serialized to JSON strings (`format: decimal`) from `Decimal`; `float` never
> appears on the money path.

## Definition of Done

- All scenarios below pass under mocked HTTP.
- `ruff` clean, `ruff format --check` clean, `mypy --strict src` → 0 errors.
- Coverage ≥ 85% overall; `src/ballast/adapters/toss/orders.py` targets ~100%.
- `TossOrderAdapter` structurally satisfies `BrokerOrderPort`; `OrderIntent`/`SubmissionResult` are reused
  (not redefined).
- No secret/token appears in any log line, exception message, `SubmissionResult.reason`, or DTO `repr`.
- No `modify` endpoint is implemented (out of scope); no amount-based order creation.

---

## Core Scenarios

### AC-1 — Structural `BrokerOrderPort` conformance (R1)

```gherkin
Given a TossOrderAdapter wired over a TossClient (mocked transport)
When it is checked against the runtime-checkable BrokerOrderPort Protocol from src/ballast/orders/ports.py
Then isinstance(adapter, BrokerOrderPort) is True
And it exposes place_order(intent) -> SubmissionResult and cancel_order(account_seq, client_order_id) -> SubmissionResult
And OrderIntent / SubmissionResult / SubmissionStatus are imported from ballast.orders.models (not redefined)
```

### AC-2 — place_order maps LIMIT+DAY to a Toss create request (R1)

```gherkin
Given an OrderIntent(client_order_id="my-order-001", account_seq="12345", side=BUY,
      ticker="005930", qty=Decimal("10"), kind=LIMIT, tif=DAY, limit_price=Decimal("70000"))
And a mocked POST /api/v1/orders returning 200
      { "result": { "orderId": "ord-abc", "clientOrderId": "my-order-001" } }
When place_order(intent) is called
Then the request carries header X-Tossinvest-Account: 12345 and Authorization: Bearer <token>
And the JSON body == { "clientOrderId":"my-order-001", "symbol":"005930", "side":"BUY",
      "orderType":"LIMIT", "timeInForce":"DAY", "quantity":"10", "price":"70000" }
And it returns SubmissionResult(status=SUBMITTED, broker_order_id="ord-abc", client_order_id="my-order-001")
```

### AC-3 — place_order maps LIMIT+CLS (LOC) for a US stock (R1/R5)

```gherkin
Given an OrderIntent(client_order_id="loc-1", account_seq="12345", side=BUY,
      ticker="AAPL", qty=Decimal("10"), kind=LIMIT, tif=CLS, limit_price=Decimal("185.5"))
And a mocked POST /api/v1/orders returning 200 { "result": { "orderId": "ord-loc" } }
When place_order(intent) is called
Then the JSON body includes "orderType":"LIMIT", "timeInForce":"CLS", "price":"185.5", "symbol":"AAPL"
And it returns SubmissionResult(status=SUBMITTED, broker_order_id="ord-loc")
```

### AC-4 — place_order maps MARKET with no price field (R1)

```gherkin
Given an OrderIntent(client_order_id="mkt-1", account_seq="12345", side=SELL,
      ticker="AAPL", qty=Decimal("3"), kind=MARKET, tif=DAY, limit_price=None)
And a mocked POST /api/v1/orders returning 200 { "result": { "orderId": "ord-mkt" } }
When place_order(intent) is called
Then the JSON body includes "orderType":"MARKET" and "quantity":"3"
And the JSON body has NO "price" key
And it returns SubmissionResult(status=SUBMITTED, broker_order_id="ord-mkt")
```

### AC-5 — clientOrderId passthrough + idempotent replay maps to DUPLICATE (R2)

```gherkin
Given an OrderIntent with client_order_id "my-order-001"
And a mocked POST /api/v1/orders that returns 200 { "result": { "orderId": "ord-abc" } } each time
When place_order(intent) is called the first time
Then it returns SubmissionResult(status=SUBMITTED, broker_order_id="ord-abc")
And the adapter records client_order_id "my-order-001" -> orderId "ord-abc"
When place_order is called again with the SAME client_order_id (within Toss's ~10-min window)
Then it returns SubmissionResult(status=DUPLICATE, broker_order_id="ord-abc")
And no second distinct live order is created (the prior result is relabeled, mirroring RecordingBrokerOrderPort)
```

### AC-6 — idempotency-key-conflict maps to FAILED, not DUPLICATE (R2/R4)

```gherkin
Given a mocked POST /api/v1/orders returning HTTP 409
      { "error": { "requestId":"req-9", "code":"idempotency-key-conflict",
                   "message":"동일한 clientOrderId 로 다른 내용의 주문을 요청할 수 없습니다." } }
When place_order(intent) is called
Then it returns SubmissionResult(status=FAILED, reason includes "idempotency-key-conflict")
And the status is NOT DUPLICATE and NOT SUBMITTED
```

### AC-7 — cancel_order resolves client_order_id and succeeds (R3)

```gherkin
Given place_order(intent) with client_order_id "my-order-001" previously returned orderId "ord-abc"
And a mocked POST /api/v1/orders/ord-abc/cancel returning 200
      { "result": { "orderId": "ord-cancel-new" } }
When cancel_order("12345", "my-order-001") is called
Then the request targets /api/v1/orders/ord-abc/cancel with header X-Tossinvest-Account: 12345
And it returns SubmissionResult(status=SUBMITTED, broker_order_id="ord-cancel-new", client_order_id="my-order-001")
```

### AC-8 — cancel_order on an unknown client_order_id fails without an HTTP call (R3)

```gherkin
Given a fresh TossOrderAdapter that has not placed client_order_id "never-seen" in this process
When cancel_order("12345", "never-seen") is called
Then it returns SubmissionResult(status=FAILED, reason includes "unknown client_order_id")
And no POST to any /cancel route is made (mocked /cancel call count == 0)
```

---

## Error & Security Scenarios

### AC-9 — cancel failure maps to FAILED (R3/R4)

```gherkin
Given place_order previously mapped client_order_id "x" -> orderId "ord-x"
And a mocked POST /api/v1/orders/ord-x/cancel returning HTTP 409
      { "error": { "requestId":"req-2", "code":"already-filled", "message":"이미 체결된 주문입니다." } }
When cancel_order("12345", "x") is called
Then it returns SubmissionResult(status=FAILED, reason includes "already-filled")
And given instead a 404 { code:"order-not-found" } or 422 { code:"cancel-restricted" },
    the result is FAILED carrying that code
And a non-transient cancel error is NOT retried
```

### AC-10 — create business/validation errors map to FAILED (R4)

```gherkin
Given a mocked POST /api/v1/orders returning, across cases:
  - 422 { code:"insufficient-buying-power" }
  - 400 { code:"invalid-request", data:{ field:"price" } }   # limit price required / tick size / CLS condition
  - 400 { code:"confirm-high-value-required" }
  - 422 { code:"order-hours-closed" }
When place_order(intent) is called for each case
Then each returns SubmissionResult(status=FAILED) with reason carrying the Toss code (+message)
And confirm-high-value-required is surfaced as FAILED (the adapter never auto-sets confirmHighValueOrder=true)
And no SubmissionResult ever leaks a TossError exception to the caller
```

### AC-11 — Transient failures retry (bounded) then FAILED; no silent infinite retry (R4)

```gherkin
Given a mocked POST /api/v1/orders returning 500 (code "internal-error") on every attempt
When place_order(intent) is called
Then the client retries with bounded backoff up to the max of 3 attempts (reusing TossClient policy)
And on exhaustion it returns SubmissionResult(status=FAILED, reason includes "internal-error")
And given a 429 with Retry-After, the retry honors Retry-After before the next attempt
And there is NO unbounded resubmission of the live order
And given 500 twice then 200 { result:{ orderId } }, place_order succeeds as SUBMITTED within 3 attempts
```

### AC-12 — CLS on a non-US (KR) symbol fails fast without an HTTP call (R5)

```gherkin
Given an OrderIntent(ticker="005930", kind=LIMIT, tif=CLS, limit_price=Decimal("70000"))  # KR 6-digit numeric
When place_order(intent) is called
Then it returns SubmissionResult(status=FAILED, reason includes "US stocks only")
And no POST to /api/v1/orders is made (mocked create call count == 0)
And for ticker="AAPL" with tif=CLS, the request IS forwarded to Toss (mock 200) and returns SUBMITTED
```

### AC-13 — Money is Decimal-only; serialized as exact decimal strings (R5)

```gherkin
Given an OrderIntent with qty=Decimal("0.5") and limit_price=Decimal("185.5")  # US fractional example shape
When place_order(intent) is called and the request body is captured
Then "quantity" == "0.5" and "price" == "185.5" (exact decimal strings, never float-formatted like 0.5/185.5 floats)
And no value on the money path is a Python float at any point
And a bare float passed to OrderIntent is rejected upstream at construction (ORDER-001 Money invariant)
```

### AC-14 — Secret/token never appears in logs, reason, or repr (R5, secrets policy)

```gherkin
Given a TossOrderAdapter over a TossClient/TokenManager configured with client_secret "s_supersecret"
    and a cached access_token "jwt-abc"
When place_order / cancel_order run (success and FAILED paths), log, raise, or have objects repr()'d
Then "s_supersecret" never appears in any captured log record, exception message, SubmissionResult.reason, or repr
And "jwt-abc" never appears either (redacted/masked)
And requestId / Toss code ARE present in structured logs / reason for traceability
```

---

## Out-of-Scope Guards

### AC-15 — No modify, no amount-based order (Scope)

```gherkin
Given the adapter's public surface
Then there is no modify method on TossOrderAdapter (BrokerOrderPort defines none)
And no code path constructs the amount-based OrderCreateRequest variant (orderAmount); quantity-based only
And the Order Manager (SPEC-ORDER-001) is unchanged by this SPEC
```

---

## Live Acceptance (Manual — User, Local)

The above are automated (mocked HTTP). The **live acceptance gate is manual**: the user runs the
`plan.md` "Manual Live-Validation Soak Checklist" locally with real `TOSS_CLIENT_ID` /
`TOSS_CLIENT_SECRET` against `https://openapi.tossinvest.com`, on a separate strategy account at 1-share
size — `place_order` → `SUBMITTED`, same-key replay → `DUPLICATE`, `cancel_order` → canceled, log
redaction check. This SPEC does not and cannot automate it in this environment (no creds/sandbox in CI).

## Quality Gates

- Coverage ≥ 85% (`pytest --cov=src/ballast`); `orders.py` ~100%.
- `mypy --strict` → 0 errors; `ruff` (lint + format) clean.
- All HTTP mocked (`httpx.MockTransport` / `respx`); zero live network in the test suite.
- No secrets in repo/logs; no `float` on the money path; no `modify` / amount-based create implemented.

## Traceability

- `@SPEC:SPEC-ADAPTER-002` → `spec.md`
- `@TEST:SPEC-ADAPTER-002` → `tests/unit/adapters/test_orders.py` (+ `post()` case in `test_client.py`,
  order-adapter case in `test_factory.py`)
- `@CODE:SPEC-ADAPTER-002` → `src/ballast/adapters/toss/orders.py`,
  `src/ballast/adapters/toss/client.py` (`post`), `src/ballast/adapters/toss/factory.py` (wiring)
