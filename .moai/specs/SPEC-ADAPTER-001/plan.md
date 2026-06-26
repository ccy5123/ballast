# SPEC-ADAPTER-001 — Implementation Plan

`@SPEC:SPEC-ADAPTER-001`

> Source of truth for all endpoints/fields: `docs/reference/toss-openapi.json` (v1.1.5).
> Read-only adapter. No order creation/modify/cancel in this SPEC.

## Module Layout

All code lives under `src/ballast/adapters/` (the IO boundary; the pure core never imports it).

```
src/ballast/adapters/
├── __init__.py
├── ports.py            # MarketDataPort, BrokerAccountPort (typing.Protocol)  [R1]
├── models.py           # Adapter DTOs (Decimal money): Quote, Account, Holding,
│                       #   Holdings, Commission, OrderRecord, OrdersPage,
│                       #   MarketCalendar, Currency enum  [R1]
├── errors.py           # TossAuthError, TossApiError, TossRateLimitError  [R3]
└── toss/
    ├── __init__.py
    ├── auth.py         # OAuth2 client-credentials TokenManager  [R2]
    ├── client.py       # httpx transport: Bearer + X-Tossinvest-Account injection,
    │                   #   {result} unwrap, string→Decimal, error mapping,
    │                   #   401 single re-auth, bounded retry/backoff  [R3]
    ├── marketdata.py   # TossMarketDataAdapter(MarketDataPort)  [R4]
    └── account.py      # TossAccountAdapter(BrokerAccountPort)  [R5]
```

Tests mirror the layout under `tests/unit/adapters/` (`test_auth.py`, `test_client.py`,
`test_marketdata.py`, `test_account.py`) using `httpx.MockTransport` / `respx`.

## Dependencies to Add

| Dependency | Where    | Version  | Reason                                                             |
| ---------- | -------- | -------- | ------------------------------------------------------------------ |
| `httpx`    | runtime  | `>=0.27` | REST client (sync-first), Bearer auth, timeouts, `MockTransport`.  |
| `respx`    | dev/test | `>=0.21` | Mock httpx routes against OpenAPI-shaped payloads (no live calls). |
| `pydantic` | runtime  | `>=2.6`  | Already present — adapter DTOs (frozen, strict Decimal).           |

`pandas`/`numpy` are NOT used here (backtest-only per Constitution). No WebSocket libs.

## Architectural Approach

- **Hexagonal ports.** `ports.py` defines `MarketDataPort` / `BrokerAccountPort` as
  `typing.Protocol`s so Toss and a future KIS adapter are interchangeable. The application layer
  depends on the Protocols, not on `toss/*`.
- **DTOs distinct from CORE.** `models.py` DTOs map to/from CORE `Order`/`Market` at the boundary
  (FD7). The Toss `Order` schema becomes `OrderRecord`; it is never imported as the domain `Order`.
- **Decimal at the boundary.** All `format: decimal` JSON strings are parsed to `Decimal` in the
  client/DTO layer; `quantize_money` from `ballast.core.models` is reused where the value crosses into
  the domain. `float` is rejected — strict pydantic / explicit `Decimal(str)` construction.

## Token-Refresh Policy (R2)

- `TokenManager` holds `(access_token, expires_at)` in memory. `expires_at = now + expires_in -
SAFETY_MARGIN` (margin e.g. 30–60s; `now` injected for testability — keep the clock out of pure
  paths and inject it here at the IO edge).
- `get_token()`:
  1. If no token cached OR within safety margin / expired → call `/oauth2/token`
     (form-urlencoded, no Authorization header) and cache.
  2. Else return cached token.
- Token errors (`OAuth2ErrorResponse`, HTTP 400/401) → `TossAuthError(error_code)`; credential errors
  are NOT retried as transient.
- Secret redaction: `client_secret`/`access_token` never logged; `TokenManager.__repr__` masks them.

## Envelope-Unwrap & Error-Mapping Policy (R3)

- **Unwrap**: every read parses `response.json()["result"]` (FD1). The client exposes a small typed
  helper (e.g. `_get(path, params, account_seq=None) -> Any` returning the unwrapped `result`).
- **Money parsing**: a shared `to_decimal(value: str | None) -> Decimal | None` converts strings; JSON
  `null` → `None`. DTOs construct `Decimal` only from `str`/`Decimal`, never `float`.
- **Error mapping** (grounded in `ErrorResponse.error.{requestId, code, message}`):
  - `401` or `code ∈ {expired-token, invalid-token}` → `TossAuthError`.
  - `429` / `code = rate-limit-exceeded` → `TossRateLimitError(retry_after, limit, remaining, reset)`
    from `Retry-After` + `X-RateLimit-*` headers (FD8).
  - everything else (`account-header-required`, `account-not-found`, `stock-not-found`,
    `exchange-rate-not-found`, `forbidden`, `internal-error`, `maintenance`, `400`, `404`, …) →
    `TossApiError(code, message, request_id, status)`.
- **401 single re-auth**: on a `401` for an authenticated call, force one `TokenManager` refresh and
  retry the call exactly once. A second `401` → raise `TossAuthError` (guard against re-auth loops).
- **Bounded retry/backoff**: transient classes (`429`, `500/internal-error`, `503/maintenance`,
  connect/read timeout) retried up to 3 attempts with exponential backoff; `429` honors `Retry-After`.
  Non-transient (`400/403/404`, credential errors) are not retried.
- **Header injection**: `Authorization: Bearer {token}` on all non-token calls;
  `X-Tossinvest-Account: {accountSeq}` only on account-scoped calls (FD5). `accountSeq` is serialized
  as the integer value Toss expects.

## Decimal-at-Boundary Policy

- Adapter is the ONLY place that turns Toss JSON strings into `Decimal`. Nothing downstream sees raw
  strings.
- Money that flows into the strategy/state layer (`last_price`, `fx_rate`, `average_purchase_price`,
  `cash_buying_power`, `quantity`) is normalized via `quantize_money` so it matches CORE precision.
- Currency is the `KRW|USD` enum; `MarketCountry` is `KR|US`.

## Risk Analysis

| Risk                         | Description                                                                 | Mitigation                                                                                                          |
| ---------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Token expiry race            | Token expires mid-flight; concurrent calls double-refresh.                  | Refresh-before-expiry with safety margin; single-flight refresh (lock/guard); 401 triggers exactly one re-auth.     |
| Money-string parsing         | `float` coercion or locale issues silently corrupt money.                   | Parse only `str`→`Decimal`; reject `float`; treat `null`→`None`; reuse `quantize_money`; property tests on parsing. |
| Pagination correctness       | `CLOSED` orders paginate via `nextCursor`/`hasNext`; `OPEN` ignores cursor. | Implement cursor loop only when `hasNext`; assert `OPEN` returns full set; test multi-page mock sequence.           |
| Rate limits                  | Bursts hit `429`; group limits unknown numerically.                         | Respect `Retry-After` + `X-RateLimit-*`; bounded backoff; surface `TossRateLimitError`; observe limits at runtime.  |
| Secret redaction             | Secret/token leaking into logs, exceptions, or `repr`.                      | Env-var-only secrets; redaction in logging + `__repr__`; tests assert secret never appears in logs/repr.            |
| Envelope drift               | Server changes envelope/field shape vs OpenAPI.                             | Fixed Definitions (FD1–FD8) pinned to OpenAPI; contract tests on representative payloads; HISTORY-gated changes.    |
| Toss `Order` vs CORE `Order` | Two `Order` types conflated at the boundary.                                | Adapter DTO `OrderRecord`; explicit mapping; mypy-strict prevents cross-import confusion.                           |
| No sandbox                   | Cannot exercise real endpoints in CI.                                       | Mocked-HTTP unit tests from OpenAPI schemas; live 1-share verification delegated to the user (manual).              |

## Test Approach (Mocked-HTTP)

- Use `httpx.MockTransport` (or `respx`) to register routes for each in-scope endpoint, returning
  representative `{ "result": ... }` payloads shaped per the OpenAPI schemas (`PriceResponse`,
  `HoldingsOverview`, `BuyingPowerResponse`, `PaginatedOrderResponse`, `OAuth2TokenResponse`,
  `ErrorResponse`, …).
- Cover: token issuance/parse, refresh-before-expiry, 401→single re-auth→retry, string→Decimal +
  currency enum, holdings item mapping (`averagePurchasePrice`/`quantity`), `cashBuyingPower` Decimal,
  `X-Tossinvest-Account` header injection, `nextCursor` pagination, envelope-error→typed-exception,
  and secret-never-in-logs/repr. See `acceptance.md` for the Given/When/Then scenarios.
- Inject `now`/clock and the transport into the client/token manager so tests are deterministic.

## Manual Live-Validation Checklist (User, Local — NOT an automated gate)

Performed by the user with real `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET` against
`https://openapi.tossinvest.com`:

1. Export `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` as env vars (confirm they are NOT in the repo).
2. Issue a token via `/oauth2/token`; confirm `token_type == "Bearer"` and a sane `expires_in`.
3. `GET /api/v1/accounts`; record a real `accountSeq`.
4. `get_price` a known symbol; confirm `lastPrice` parses to `Decimal` and `currency`.
5. `get_exchange_rate(USD, KRW)`; confirm `rate` is a `Decimal`.
6. `get_holdings(accountSeq)`; confirm `averagePurchasePrice`/`quantity` per item (1-share sanity).
7. `get_buying_power(accountSeq, KRW or USD)`; confirm `cashBuyingPower` Decimal.
8. `list_orders(accountSeq, "CLOSED")`; if `hasNext`, follow `nextCursor` once.
9. Grep logs to confirm no secret/token leaked.
10. Leave order create/modify/cancel UNTOUCHED (out of scope for this SPEC).

## Milestones (priority-ordered, no time estimates)

- **Primary Goal (Priority High)**: ports + DTOs (`R1`), token manager (`R2`), transport with
  envelope-unwrap / error-mapping / 401-reauth / retry (`R3`).
- **Secondary Goal (Priority High)**: market-data read methods (`R4`: `get_price`,
  `get_exchange_rate`, `get_market_calendar`).
- **Final Goal (Priority Medium)**: account/asset/order-info/order-history read methods (`R5`),
  including `CLOSED` pagination.
- **Optional Goal (Priority Low)**: optional market-data reads (orderbook, candles, trades,
  price-limits, stocks, warnings).

## Quality Gates (per Constitution)

- `uv run ruff check .` → 0 errors; `uv run ruff format --check .` clean.
- `uv run mypy --strict src` → 0 errors.
- `uv run pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% (adapter aims higher).
- No secrets in repo/logs; all HTTP mocked in tests.
