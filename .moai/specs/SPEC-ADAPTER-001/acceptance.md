# SPEC-ADAPTER-001 — Acceptance Criteria

`@SPEC:SPEC-ADAPTER-001` `@TEST:SPEC-ADAPTER-001`

> All scenarios are validated by **mocked-HTTP unit tests** (`httpx.MockTransport` / `respx`) using
> payloads shaped per `docs/reference/toss-openapi.json` (v1.1.5). No live network. **Live
> real-account verification is a manual user step** (see `plan.md` checklist), not an automated gate.
> Money/quantity values arrive as JSON strings (`format: decimal`) and MUST become `Decimal`.

## Definition of Done

- All scenarios below pass under mocked HTTP.
- `ruff` clean, `ruff format --check` clean, `mypy --strict src` → 0 errors.
- Coverage ≥ 85% for `src/ballast/adapters/**` (target higher for transport/auth).
- No secret/token appears in any log line, exception message, or DTO `repr`.
- No state-changing endpoint (`POST` create/modify/cancel) is implemented.

---

## Core Scenarios

### AC-1 — Token issuance parses access_token / expires_in (R2)

```gherkin
Given a mocked POST /oauth2/token that returns
  { "access_token": "jwt-abc", "token_type": "Bearer", "expires_in": 3600 }
And TOSS_CLIENT_ID and TOSS_CLIENT_SECRET are provided via environment variables
When the TokenManager requests a token for the first time
Then the request body is application/x-www-form-urlencoded with grant_type=client_credentials,
     client_id and client_secret
And the request carries NO Authorization header
And the manager caches access_token "jwt-abc" with expiry computed from expires_in minus the safety margin
And token_type is "Bearer"
```

### AC-2 — Expired/near-expiry token triggers refresh before a call (R2)

```gherkin
Given a cached token whose expiry is within the safety margin of expires_in (or already expired)
And a mocked POST /oauth2/token that returns a fresh access_token "jwt-new"
When the client is about to issue an authenticated request
Then the TokenManager transparently refreshes the token before the request is sent
And the subsequent authenticated request uses Authorization: Bearer jwt-new
```

### AC-3 — A 401 triggers exactly one re-auth then retry (R3)

```gherkin
Given a valid cached token
And a mocked authenticated endpoint that returns 401 (code "expired-token") on the first attempt
And a mocked POST /oauth2/token returning a fresh token
And the same endpoint returns 200 { "result": ... } on the second attempt
When the client calls that endpoint
Then the client re-authenticates exactly once, retries the call once, and returns the unwrapped result
And if a SECOND consecutive 401 occurs instead, the client raises TossAuthError (no re-auth loop)
```

### AC-4 — get_price parses lastPrice string→Decimal and currency enum (R4)

```gherkin
Given a mocked GET /api/v1/prices?symbols=TQQQ returning
  { "result": [ { "symbol": "TQQQ", "lastPrice": "70.12", "currency": "USD",
                  "timestamp": "2026-06-26T13:30:00Z" } ] }
When get_price("TQQQ") is called
Then it returns a Quote with last_price == Decimal("70.12") (type Decimal, not float)
And currency == Currency.USD
And symbol == "TQQQ"
```

### AC-5 — get_holdings maps averagePurchasePrice / quantity per item (R5)

```gherkin
Given account_seq "12345"
And a mocked GET /api/v1/holdings (header X-Tossinvest-Account: 12345) returning a HoldingsOverview
    whose items[0] = { "symbol": "SOXL", "currency": "USD", "quantity": "10",
                       "lastPrice": "25.50", "averagePurchasePrice": "24.00",
                       "marketValue": { "purchaseAmount": "240.00", "amount": "255.00",
                                        "amountAfterCost": "254.00" }, ... }
When get_holdings("12345") is called
Then the returned Holdings.items[0] has quantity == Decimal("10")
And average_purchase_price == Decimal("24.00")   # 평단
And last_price == Decimal("25.50")
And currency == Currency.USD
```

### AC-6 — get_buying_power returns cashBuyingPower Decimal for the currency (R5)

```gherkin
Given account_seq "12345"
And a mocked GET /api/v1/buying-power?currency=USD (header X-Tossinvest-Account: 12345) returning
  { "result": { "currency": "USD", "cashBuyingPower": "1000.00" } }
When get_buying_power("12345", Currency.USD) is called
Then it returns Decimal("1000.00")   # 예수금
And the value is a Decimal (not float, not str)
```

### AC-7 — Account calls inject X-Tossinvest-Account header (R3/R5)

```gherkin
Given account_seq "12345"
And mocked account-scoped endpoints (holdings, buying-power, sellable-quantity, commissions,
    orders, order-detail)
When any BrokerAccountPort method (other than list_accounts) is called with account_seq "12345"
Then the outgoing request includes header X-Tossinvest-Account: 12345 (integer value)
And list_accounts() (GET /api/v1/accounts) is called WITHOUT that header
```

### AC-8 — list_orders follows nextCursor pagination (R5)

```gherkin
Given account_seq "12345" and status "CLOSED"
And a mocked GET /api/v1/orders?status=CLOSED returning
  { "result": { "orders": [orderA], "nextCursor": "cur-2", "hasNext": true } }
And a mocked GET /api/v1/orders?status=CLOSED&cursor=cur-2 returning
  { "result": { "orders": [orderB], "nextCursor": null, "hasNext": false } }
When list_orders("12345", "CLOSED") is paged until hasNext is false
Then page 1 yields OrdersPage(orders=[orderA], next_cursor="cur-2", has_next=True)
And following next_cursor yields OrdersPage(orders=[orderB], next_cursor=None, has_next=False)
And each OrderRecord exposes order_id, price (Decimal|None), quantity (Decimal), status, currency
```

---

## Error & Security Scenarios

### AC-9 — Envelope error maps to a typed exception (R3)

```gherkin
Given a mocked GET /api/v1/prices?symbols=BADSYM returning HTTP 404
  { "error": { "requestId": "req-1", "code": "stock-not-found",
               "message": "종목을 찾을 수 없습니다." } }
When get_price("BADSYM") is called
Then it raises TossApiError carrying code "stock-not-found", the message, and request_id "req-1"
And given a 429 with code "rate-limit-exceeded" and headers Retry-After / X-RateLimit-*,
    the call raises TossRateLimitError surfacing retry_after and the rate-limit headers
And given a 401 OAuth2ErrorResponse from /oauth2/token, the call raises TossAuthError
```

### AC-10 — Secret/token never appears in logs or repr (R2/R3, secrets policy)

```gherkin
Given a TokenManager and TossClient configured with client_secret "s_supersecret" and a cached
    access_token "jwt-abc"
When the client logs a request/response, raises an error, or its objects are repr()'d
Then "s_supersecret" never appears in any captured log record, exception message, or repr output
And "jwt-abc" never appears either (redacted/masked)
And requestId IS present in structured logs for traceability
```

### AC-11 — Transient failures retry with bounded backoff; non-transient do not (R3)

```gherkin
Given a mocked endpoint returning 500 (code "internal-error") twice then 200 { "result": ... }
When the client calls it
Then the client retries with backoff and succeeds within the max of 3 attempts
And a 429 honors Retry-After before retrying
But a 400 ("invalid-request") or 404 is NOT retried and surfaces immediately as TossApiError
```

### AC-12 — Float money is rejected at the boundary (R3, Decimal-at-boundary)

```gherkin
Given any DTO construction path for money/quantity
When a value would be a Python float (e.g. a malformed numeric instead of a decimal string)
Then construction rejects it (no silent float coercion onto the money path)
And valid decimal strings parse to Decimal, and JSON null parses to None
```

---

## Live Acceptance (Manual — User, Local)

The above are automated (mocked HTTP). The **live acceptance gate is manual**: the user runs the
`plan.md` "Manual Live-Validation Checklist" locally with real `TOSS_CLIENT_ID` /
`TOSS_CLIENT_SECRET` against `https://openapi.tossinvest.com` (token issuance, accounts, price, fx,
holdings 1-share, buying-power, CLOSED-order pagination, log redaction check). This SPEC does not and
cannot automate it in this environment.

## Quality Gates

- Coverage ≥ 85% (`pytest --cov=src/ballast`).
- `mypy --strict` → 0 errors; `ruff` (lint + format) clean.
- All HTTP mocked (`httpx.MockTransport` / `respx`); zero live network in the test suite.
- No secrets in repo/logs; read-only scope (no create/modify/cancel).

## Traceability

- `@SPEC:SPEC-ADAPTER-001` → `spec.md`
- `@TEST:SPEC-ADAPTER-001` → `tests/unit/adapters/{test_auth,test_client,test_marketdata,test_account}.py`
- `@CODE:SPEC-ADAPTER-001` → `src/ballast/adapters/{ports,models,errors}.py`,
  `src/ballast/adapters/toss/{auth,client,marketdata,account}.py`
