---
id: SPEC-ADAPTER-001
version: 0.1.0
status: draft
created: 2026-06-26
updated: 2026-06-26
author: ccy5123
priority: high
lifecycle_level: spec-first
---

## HISTORY

### v0.1.0 (2026-06-26)

- Initial draft. Defines the **read-only** Toss Securities broker/market-data adapter (P1) for
  ballast: broker-agnostic port Protocols (`MarketDataPort`, `BrokerAccountPort`), an OAuth2
  client-credentials token manager, an httpx-based transport that unwraps the `{ "result": <data> }`
  envelope and parses money/quantity JSON strings to `Decimal`, typed exceptions
  (`TossAuthError`, `TossApiError`, `TossRateLimitError`), and adapter DTOs distinct from the
  CORE-001 domain types.
- Every endpoint path, parameter, header, and response field in this SPEC is grounded in
  `docs/reference/toss-openapi.json` (토스증권 Open API, OpenAPI 3.1.0, version 1.1.5,
  server `https://openapi.tossinvest.com`) — the **single source of truth**. No path or field is
  invented.
- Scope is intentionally **READ-ONLY**. Order creation / modify / cancel (any state-changing call)
  are explicitly OUT of scope and deferred to the P2/P3 order SPEC.

---

# SPEC-ADAPTER-001 — Toss Securities Read-Only Adapter (P1)

`@SPEC:SPEC-ADAPTER-001`

> Source of truth for all endpoints and field shapes: **`docs/reference/toss-openapi.json`**
> (토스증권 Open API, OpenAPI 3.1.0, v1.1.5, `https://openapi.tossinvest.com`).
> Where this document and the OpenAPI file disagree, the OpenAPI file wins.

## Environment

- Language: Python `>=3.11` (single language; financial values use `Decimal`).
- Packaging/deps: `uv` + `pyproject.toml`. New deps: `httpx` (`>=0.27`, REST, sync-first),
  `respx` (dev-only, mocked-HTTP tests). `pydantic` v2 (`>=2.6`) is already present.
- HTTP: `httpx` only (REST). No WebSocket (close/cycle-based strategies don't need streaming).
- Validation/modeling: `pydantic` v2 (adapter DTOs) and/or frozen `dataclasses`.
- Module location: `src/ballast/adapters/` — this is the **IO boundary** (Hexagonal port-adapter).
  It depends on the CORE-001 contracts/types but the core never depends on it.
- Money/quantity: parsed from JSON **strings** (the OpenAPI `format: decimal`) to `Decimal` at the
  boundary; reuse `ballast.core.models.quantize_money` where money crosses into the domain.
- Secrets: `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` from environment variables ONLY.
- Tests: `pytest`, `pytest-cov` with `httpx` `MockTransport` and/or `respx`. No live network.
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: **No sandbox, no credentials in this environment.** Toss provides no sandbox; this SPEC is
  validated entirely by **mocked-HTTP unit tests** against payloads shaped per the OpenAPI schemas.
  **Live "real-account 1-share" verification is a manual step the user performs locally** with their
  own keys — it is the _live acceptance gate_, not an automated check here.
- A2: Auth is OAuth2 **Client Credentials**. `POST /oauth2/token` is the only call made WITHOUT an
  `Authorization` header (its OpenAPI `security` is `[]`). All other calls send
  `Authorization: Bearer {access_token}`.
- A3: The token response carries `access_token`, `token_type` (always `"Bearer"`), and `expires_in`
  (seconds, ~3600). The token is cached in memory and refreshed _before_ `expires_in` elapses using a
  safety margin. Secrets and tokens are never logged or included in `repr`.
- A4: Account/Asset/Order-Info/Order-History endpoints additionally require header
  `X-Tossinvest-Account: {accountSeq}` where `accountSeq` is the **integer** key from
  `GET /api/v1/accounts`. Market-Data / Market-Info / Stock-Info endpoints do NOT need it.
- A5: Every read response is the BFF envelope `{ "result": <data> }` (OpenAPI `ApiResponse` combined
  via `allOf` with an endpoint-specific `result`). The adapter unwraps `result`. Error responses use
  `{ "error": { requestId, code, message, data? } }` (OpenAPI `ErrorResponse` / `ApiError`).
- A6: Money/quantity values arrive as JSON **strings** (`format: decimal`, e.g. `lastPrice: "70000"`).
  Nullable money fields arrive as `["string","null"]`. Currency is the enum `KRW | USD`. The adapter
  parses strings to `Decimal` and rejects `float`.
- A7: Rate limits are advertised via headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`,
  `X-RateLimit-Reset`, `Retry-After`) and a `429` response (`code: "rate-limit-exceeded"`). Endpoints
  are grouped (`MARKET_DATA`, `MARKET_DATA_CHART`); exact per-group limits are observed at runtime.
- A8: Strategy isolation = separate brokerage account (`account_seq`), per CORE-001 A2. The adapter is
  account-addressable but holds no global mutable state beyond the in-memory token cache.

## Fixed Definitions

These are pinned against `docs/reference/toss-openapi.json` and MUST NOT drift silently. Changing any
of them requires a HISTORY entry and re-verification against the OpenAPI file.

- **FD1 — Envelope.** Read success = `{ "result": <data> }`; error = `{ "error": { requestId, code,
message, data? } }`. `result` is the only payload the adapter returns to callers.
- **FD2 — Token shape.** `OAuth2TokenResponse = { access_token: str, token_type: "Bearer",
expires_in: int }`. `OAuth2ErrorResponse.error ∈ { invalid_request, invalid_client, invalid_grant,
unauthorized_client, unsupported_grant_type }`.
- **FD3 — Money is string→Decimal.** Every `format: decimal` field is a JSON string; nullable ones are
  `null`-or-string. No `float` anywhere on the money path.
- **FD4 — Currency enum** = `KRW | USD`. **MarketCountry enum** = `KR | US`.
- **FD5 — Account header.** `X-Tossinvest-Account` is the integer `accountSeq`, required by holdings,
  buying-power, sellable-quantity, commissions, orders, order-detail.
- **FD6 — Order-History pagination.** `PaginatedOrderResponse = { orders: Order[], nextCursor: str|null,
hasNext: bool }`. `status` query param ∈ `OPEN | CLOSED` (lifecycle group). For `OPEN`, `cursor`/
  `limit` are ignored and the full set is returned; for `CLOSED` they paginate.
- **FD7 — Toss `Order` ≠ CORE `Order`.** The Toss `Order` schema
  (`{ orderId, symbol, side, orderType ∈ LIMIT|MARKET, timeInForce ∈ DAY|CLS|OPG, status, price?,
quantity, orderAmount?, currency, orderedAt, canceledAt?, execution }`) is an adapter DTO
  (`OrderRecord`). It is mapped to/from the CORE `Order` at the boundary, never conflated.
- **FD8 — Rate-limit headers** = `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`,
  `Retry-After` (all integers).

## Requirements

The adapter MUST satisfy the following EARS requirements (≤5 modules). All are tagged to
`@SPEC:SPEC-ADAPTER-001`.

### REQ-ADAPTER-001-R1 — Broker-Agnostic Port Interfaces (Ubiquitous)

`@SPEC:SPEC-ADAPTER-001` `REQ-ADAPTER-001-R1`

The system **shall always** define broker-agnostic `typing.Protocol` ports in
`src/ballast/adapters/ports.py` so that Toss and KIS implementations are swappable, and adapter DTOs
in `src/ballast/adapters/models.py` whose money/quantity fields are `Decimal` (never `float`):

- **`MarketDataPort`** (no account header):
  - `get_price(symbol: str) -> Quote`
  - `get_exchange_rate(base: Currency, quote: Currency) -> Decimal`
  - `get_market_calendar(country: str) -> MarketCalendar`
  - (optional) `get_orderbook(symbol)`, `get_candles(symbol, interval, ...)`
- **`BrokerAccountPort`** (account-scoped):
  - `list_accounts() -> list[Account]`
  - `get_holdings(account_seq: str, symbol: str | None = None) -> Holdings`
  - `get_buying_power(account_seq: str, currency: Currency) -> Decimal`
  - `get_sellable_quantity(account_seq: str, symbol: str) -> Decimal`
  - `get_commissions(account_seq: str) -> list[Commission]`
  - `list_orders(account_seq: str, status: Literal["OPEN", "CLOSED"], ...) -> OrdersPage`
  - `get_order(account_seq: str, order_id: str) -> OrderRecord`
- **Adapter DTOs** (immutable; pydantic frozen or frozen dataclass): `Quote`, `Account`, `Holding`,
  `Holdings`, `Commission`, `OrderRecord`, `OrdersPage`, `MarketCalendar`, plus the `Currency`
  (`KRW|USD`) enum. These are distinct from the CORE-001 `Order`/`Market` types and are mapped at the
  boundary (FD7).

The ports **shall** prescribe no concrete transport — only the broker-agnostic shape later adapters
satisfy structurally.

### REQ-ADAPTER-001-R2 — OAuth2 Token Manager (Event-driven + State-driven)

`@SPEC:SPEC-ADAPTER-001` `REQ-ADAPTER-001-R2`

The token manager lives in `src/ballast/adapters/toss/auth.py` and uses
`POST /oauth2/token` with `Content-Type: application/x-www-form-urlencoded`, body
`grant_type=client_credentials`, `client_id`, `client_secret`, and **no** `Authorization` header.

- **When** a request needs a token and none is cached, the system **shall** call `/oauth2/token`,
  parse `access_token` / `token_type` / `expires_in` (FD2), and cache the token in memory with its
  computed expiry (`now + expires_in - safety_margin`).
- **While** the cached token is within the safety margin of `expires_in` (or already expired), the
  system **shall** transparently refresh it _before_ issuing the next authenticated call.
- **Where** the token endpoint returns `OAuth2ErrorResponse` (HTTP 400/401), the system **shall** raise
  `TossAuthError` carrying the `error` code, and **shall not** retry client-credential errors
  (`invalid_client`, `unsupported_grant_type`, …) as if they were transient.
- The system **shall not** log or expose `client_id`, `client_secret`, or `access_token` (redaction in
  logs and in any DTO/exception `repr`).

### REQ-ADAPTER-001-R3 — Transport, Envelope Unwrap & Error Mapping (Event-driven + Unwanted)

`@SPEC:SPEC-ADAPTER-001` `REQ-ADAPTER-001-R3`

The httpx client lives in `src/ballast/adapters/toss/client.py` with base URL configurable (default
`https://openapi.tossinvest.com`).

- **When** the client issues any non-token request, it **shall** inject `Authorization: Bearer
{access_token}` and, **where** the call is account-scoped, the header `X-Tossinvest-Account:
{accountSeq}` (integer; FD5).
- **When** a `2xx` response arrives, the client **shall** unwrap the `{ "result": <data> }` envelope
  (FD1) and parse every `format: decimal` string field to `Decimal` (FD3), treating JSON `null` as
  `None`.
- **When** the server returns an `ErrorResponse` envelope or a non-2xx status, the client **shall** map
  it to a typed exception preserving `error.code`, `error.message`, and `error.requestId`:
  - `401` / `code ∈ {expired-token, invalid-token}` → `TossAuthError`.
  - `429` / `code = rate-limit-exceeded` → `TossRateLimitError`, surfacing `Retry-After` and the
    `X-RateLimit-*` headers (FD8).
  - all other domain/error codes (`account-header-required`, `account-not-found`, `stock-not-found`,
    `exchange-rate-not-found`, `forbidden`, `internal-error`, `maintenance`, …) → `TossApiError`.
- **When** a `401` occurs on an authenticated call, the system **shall** trigger **exactly one**
  re-authentication and retry the call once; a second consecutive `401` **shall** surface as
  `TossAuthError` (no infinite re-auth loop).
- **When** a transient failure occurs (`429`, `500/internal-error`, `503/maintenance`, connection/read
  timeout), the system **shall** perform bounded retry with backoff (max 3 attempts), honoring
  `Retry-After` on `429`; on exhaustion it **shall** raise the mapped typed exception.
- The system **shall not** retry non-transient client errors (`400`, `403`, `404`, auth-credential
  errors) and **shall not** include secrets/tokens in any log line, error message, or `repr`.

### REQ-ADAPTER-001-R4 — Market-Data & Market-Info Read Methods (Event-driven)

`@SPEC:SPEC-ADAPTER-001` `REQ-ADAPTER-001-R4`

Implemented in `src/ballast/adapters/toss/marketdata.py` against the grounded endpoints; every method
unwraps `result` and returns adapter DTOs with `Decimal` money.

- **When** `get_price(symbol)` is called, the system **shall** `GET /api/v1/prices?symbols={symbol}`
  and map `PriceResponse.{symbol, lastPrice(str→Decimal), currency(KRW|USD), timestamp?}` into `Quote`.
- **When** `get_exchange_rate(base, quote)` is called, the system **shall**
  `GET /api/v1/exchange-rate?baseCurrency={base}&quoteCurrency={quote}` and return
  `ExchangeRateResponse.rate` as `Decimal` (optionally also exposing `midRate`/`validUntil`).
- **When** `get_market_calendar(country)` is called with `country ∈ {US, KR}`, the system **shall**
  `GET /api/v1/market-calendar/US` or `/KR` and map
  `today/previousBusinessDay/nextBusinessDay` (+ session info) into `MarketCalendar`.
- **Where** orderbook/candles/trades/price-limits are requested (optional), the system **shall** use
  `GET /api/v1/orderbook?symbol=`, `/api/v1/candles?interval={1m|1d}&count&before&adjusted`,
  `/api/v1/trades?symbol=&count`, `/api/v1/price-limits?symbol=` respectively, parsing string money to
  `Decimal` and `null` price limits (US) to `None`.
- **Where** stock reference info is requested (optional), the system **shall** use
  `GET /api/v1/stocks?symbols=` (→ `StockInfo[]`, including `leverageFactor`/`securityType` usable by
  the CORE leverage guardrail) and `GET /api/v1/stocks/{symbol}/warnings` (→ `StockWarning[]`).

### REQ-ADAPTER-001-R5 — Account / Asset / Order-Info / Order-History Read Methods (Event-driven + State-driven)

`@SPEC:SPEC-ADAPTER-001` `REQ-ADAPTER-001-R5`

Implemented in `src/ballast/adapters/toss/account.py`; all methods inject `X-Tossinvest-Account` (FD5)
except `list_accounts`, unwrap `result`, and return `Decimal` money.

- **When** `list_accounts()` is called, the system **shall** `GET /api/v1/accounts` and map each
  `Account.{accountNo, accountSeq(int), accountType}` into an `Account` DTO (accountSeq is the value
  later passed as the header).
- **When** `get_holdings(account_seq, symbol?)` is called, the system **shall**
  `GET /api/v1/holdings` (header set; optional `?symbol=`) and map
  `HoldingsOverview.{totalPurchaseAmount, marketValue, profitLoss, dailyProfitLoss}` plus each
  `items[].{symbol, name, marketCountry, currency, quantity, lastPrice, averagePurchasePrice(=평단),
marketValue, profitLoss, cost}` into `Holdings`/`Holding`. `averagePurchasePrice` is the strategy's
  avg cost and `quantity` the position size.
- **When** `get_buying_power(account_seq, currency)` is called, the system **shall**
  `GET /api/v1/buying-power?currency={currency}` (header set) and return
  `BuyingPowerResponse.cashBuyingPower` (= 예수금) as `Decimal`.
- **When** `get_sellable_quantity(account_seq, symbol)` is called, the system **shall**
  `GET /api/v1/sellable-quantity?symbol={symbol}` (header set) and return
  `SellableQuantityResponse.sellableQuantity` as `Decimal`.
- **When** `get_commissions(account_seq)` is called, the system **shall** `GET /api/v1/commissions`
  (header set) and map each `Commission.{marketCountry, commissionRate, startDate?, endDate?}`.
- **When** `list_orders(account_seq, status, symbol?, from?, to?, cursor?, limit?)` is called, the
  system **shall** `GET /api/v1/orders?status={OPEN|CLOSED}[&symbol&from&to&cursor&limit]` and map
  `PaginatedOrderResponse.{orders[], nextCursor, hasNext}` into `OrdersPage`. **While** `status=CLOSED`
  and `hasNext` is true, callers may follow `nextCursor`; **while** `status=OPEN`, `cursor`/`limit` are
  ignored by the server (FD6).
- **When** `get_order(account_seq, order_id)` is called, the system **shall**
  `GET /api/v1/orders/{orderId}` (header set) and map the Toss `Order` schema into `OrderRecord`
  (including the nested `execution` block); this DTO is distinct from CORE `Order` (FD7).

## Specifications

Port method → Toss endpoint (cited path from `docs/reference/toss-openapi.json`) → implementing module.

| Port method (REQ)                                   | Toss endpoint (source of truth)                                                                                             | Module                                                            |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| _(token manager)_ `R2`                              | `POST /oauth2/token` (security `[]`, form-urlencoded)                                                                       | `src/ballast/adapters/toss/auth.py`                               |
| `MarketDataPort.get_price` `R4`                     | `GET /api/v1/prices?symbols=` → `PriceResponse[]`                                                                           | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_exchange_rate` `R4`             | `GET /api/v1/exchange-rate?baseCurrency&quoteCurrency[&dateTime]` → `ExchangeRateResponse`                                  | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_market_calendar("US")` `R4`     | `GET /api/v1/market-calendar/US[?date]` → `UsMarketCalendarResponse`                                                        | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_market_calendar("KR")` `R4`     | `GET /api/v1/market-calendar/KR[?date]` → `KrMarketCalendarResponse`                                                        | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_orderbook` (optional) `R4`      | `GET /api/v1/orderbook?symbol=` → `OrderbookResponse`                                                                       | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_candles` (optional) `R4`        | `GET /api/v1/candles?interval={1m,1d}&count&before&adjusted` → `CandlePageResponse`                                         | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_trades` (optional) `R4`         | `GET /api/v1/trades?symbol=&count` → `Trade[]`                                                                              | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_price_limits` (optional) `R4`   | `GET /api/v1/price-limits?symbol=` → `PriceLimitResponse`                                                                   | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_stocks` (optional) `R4`         | `GET /api/v1/stocks?symbols=` → `StockInfo[]`                                                                               | `src/ballast/adapters/toss/marketdata.py`                         |
| `MarketDataPort.get_stock_warnings` (optional) `R4` | `GET /api/v1/stocks/{symbol}/warnings` → `StockWarning[]`                                                                   | `src/ballast/adapters/toss/marketdata.py`                         |
| `BrokerAccountPort.list_accounts` `R5`              | `GET /api/v1/accounts` → `Account[]`                                                                                        | `src/ballast/adapters/toss/account.py`                            |
| `BrokerAccountPort.get_holdings` `R5`               | `GET /api/v1/holdings` (`X-Tossinvest-Account`[, `?symbol`]) → `HoldingsOverview`                                           | `src/ballast/adapters/toss/account.py`                            |
| `BrokerAccountPort.get_buying_power` `R5`           | `GET /api/v1/buying-power?currency=` (`X-Tossinvest-Account`) → `BuyingPowerResponse`                                       | `src/ballast/adapters/toss/account.py`                            |
| `BrokerAccountPort.get_sellable_quantity` `R5`      | `GET /api/v1/sellable-quantity?symbol=` (`X-Tossinvest-Account`) → `SellableQuantityResponse`                               | `src/ballast/adapters/toss/account.py`                            |
| `BrokerAccountPort.get_commissions` `R5`            | `GET /api/v1/commissions` (`X-Tossinvest-Account`) → `Commission[]`                                                         | `src/ballast/adapters/toss/account.py`                            |
| `BrokerAccountPort.list_orders` `R5`                | `GET /api/v1/orders?status={OPEN,CLOSED}[&symbol&from&to&cursor&limit]` (`X-Tossinvest-Account`) → `PaginatedOrderResponse` | `src/ballast/adapters/toss/account.py`                            |
| `BrokerAccountPort.get_order` `R5`                  | `GET /api/v1/orders/{orderId}` (`X-Tossinvest-Account`) → `Order`                                                           | `src/ballast/adapters/toss/account.py`                            |
| _(ports + DTOs)_ `R1`                               | n/a (contracts)                                                                                                             | `src/ballast/adapters/ports.py`, `src/ballast/adapters/models.py` |
| _(transport + envelope + errors)_ `R3`              | applies to all calls above                                                                                                  | `src/ballast/adapters/toss/client.py`                             |

### Adapter DTOs introduced (REQ-ADAPTER-001-R1)

| DTO               | Source schema(s)                                        | Key fields (Decimal money)                                                                                                   |
| ----------------- | ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `Currency` (enum) | `Currency`                                              | `KRW                                                                                                                         | USD`                                                              |
| `Quote`           | `PriceResponse`                                         | `symbol`, `last_price: Decimal`, `currency`, `timestamp: datetime                                                            | None`                                                             |
| `Account`         | `Account`                                               | `account_no: str`, `account_seq: str`, `account_type: str`                                                                   |
| `Holding`         | `HoldingsItem`                                          | `symbol`, `quantity: Decimal`, `average_purchase_price: Decimal`, `last_price: Decimal`, `market_value: Decimal`, `currency` |
| `Holdings`        | `HoldingsOverview`                                      | `total_purchase_amount`, `market_value`, `profit_loss`, `items: list[Holding]`                                               |
| `Commission`      | `Commission`                                            | `market_country`, `commission_rate: Decimal`, `start_date?`, `end_date?`                                                     |
| `OrderRecord`     | Toss `Order` (+ `OrderExecution`)                       | `order_id`, `symbol`, `side`, `order_type`, `time_in_force`, `status`, `price: Decimal                                       | None`, `quantity: Decimal`, `currency`, `ordered_at`, `execution` |
| `OrdersPage`      | `PaginatedOrderResponse`                                | `orders: list[OrderRecord]`, `next_cursor: str                                                                               | None`, `has_next: bool`                                           |
| `MarketCalendar`  | `UsMarketCalendarResponse` / `KrMarketCalendarResponse` | `country`, `today`, `previous_business_day`, `next_business_day` (+ session info)                                            |

## Dependencies

- **Depends on SPEC-CORE-001**: the adapter feeds `current_price` (→ `Market`), `fx_rate`,
  `averagePurchasePrice` (→ avg), `cashBuyingPower` (→ 예수금/pool), and position `quantity` into the
  strategy/state layers. Reuse `ballast.core.models.quantize_money` for money crossing into the domain.
  Adapter DTOs are mapped to/from CORE `Order`/`Market` at the boundary (FD7) — never conflated.
- **Source of truth**: `docs/reference/toss-openapi.json` (v1.1.5).
- New third-party deps: `httpx` (runtime), `respx` (dev/test).

## Scope

### In scope (READ-ONLY, P1)

- Auth (`/oauth2/token`), market data, market info, stock info, account, asset, order-info
  (pre-validation), and order-history reads, exactly as enumerated in R4/R5 and the Specifications
  table.

### Out of scope (state-changing writes — P2/P3)

The system **shall not** implement any state-changing call in this SPEC:

- `POST /api/v1/orders` (create order)
- `POST /api/v1/orders/{orderId}/modify`
- `POST /api/v1/orders/{orderId}/cancel`

These belong to the P2/P3 order SPEC and are explicitly excluded here.

## Secrets Policy

- `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` are read from environment variables ONLY — never committed,
  never written to the repo, never logged. The `access_token` is held in memory only.
- Log redaction: any log line, exception message, or DTO `repr` that could surface a secret/token MUST
  redact it. Structured logging carries `requestId` for traceability, never credentials.
- Base URL is configurable (default `https://openapi.tossinvest.com`).

## TBD / Carry-forward

- **T7 (예약지정가 fields)** and **T8 (LOC exposure)** concern **order creation** and are OUT of scope
  here. They are resolved by reading this same `docs/reference/toss-openapi.json` in the P2/P3 order
  SPEC. Grounded fact already observed for that SPEC: Toss exposes LOC via `POST /api/v1/orders`
  request field `timeInForce = CLS` ("At the Close") combined with `orderType = LIMIT`
  (`LIMIT + CLS = LOC`), currently **US-stock only**; the idempotency key is `clientOrderId`
  (10-minute validity). This note is carry-forward context, not a requirement of ADAPTER-001.
- **Rate-limit specifics per group** (`MARKET_DATA`, `MARKET_DATA_CHART`): respect the
  `X-RateLimit-*` / `Retry-After` headers; exact numeric limits are carried as observed-at-runtime
  (the OpenAPI file documents the headers and groups but not fixed numbers).

## Reality Constraints

- No sandbox + no credentials in this environment → ADAPTER-001 is validated by **mocked-HTTP unit
  tests** (`httpx.MockTransport` / `respx`) against representative payloads taken from the OpenAPI
  schemas. **Live "real-account 1-share" verification is performed by the user locally** with their
  keys; that is the live acceptance step, not an automated gate in this SPEC (see `acceptance.md`).
- `httpx` (+ `respx` for tests) to be added as deps. Money is `Decimal` at the domain boundary.

## Traceability

- `@SPEC:SPEC-ADAPTER-001` — this document.
- `@TEST:SPEC-ADAPTER-001` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/adapters/`.
- `@CODE:SPEC-ADAPTER-001` — `src/ballast/adapters/ports.py`, `models.py`,
  `toss/{auth,client,marketdata,account}.py`.
- `@DOC:SPEC-ADAPTER-001` — generated during `/moai:3-sync`.

### Requirement Index

| Requirement ID     | EARS Type                   | Summary                                                                                       |
| ------------------ | --------------------------- | --------------------------------------------------------------------------------------------- |
| REQ-ADAPTER-001-R1 | Ubiquitous                  | Broker-agnostic ports (`MarketDataPort`, `BrokerAccountPort`) + `Decimal` adapter DTOs        |
| REQ-ADAPTER-001-R2 | Event-driven + State-driven | OAuth2 client-credentials token manager (cache, refresh-before-expiry, no secret logging)     |
| REQ-ADAPTER-001-R3 | Event-driven + Unwanted     | httpx transport: Bearer + account header, `{result}` unwrap, typed errors, 401 re-auth, retry |
| REQ-ADAPTER-001-R4 | Event-driven                | Market-data / market-info / stock-info read methods (price, fx, calendar, …)                  |
| REQ-ADAPTER-001-R5 | Event-driven + State-driven | Account / asset / order-info / order-history read methods (holdings 평단, 예수금, pagination) |
