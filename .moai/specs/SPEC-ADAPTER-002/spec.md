---
id: SPEC-ADAPTER-002
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

- Initial draft. Defines the **Toss Securities write (order-submission) adapter** (P3) for ballast:
  the concrete `BrokerOrderPort` implementation (`TossOrderAdapter`) that the P2 Order Manager
  (SPEC-ORDER-001) already calls. It turns the dry-run path into a real **live-submission** path by
  mapping a broker-neutral `OrderIntent` onto the Toss `OrderCreateRequest` and POSTing
  `POST /api/v1/orders`, and by mapping the Toss response/errors back onto `SubmissionResult`.
- Every endpoint path, parameter, header, request field, enum, and response shape in this SPEC is
  grounded in `docs/reference/toss-openapi.json` (토스증권 Open API, OpenAPI 3.1.0, version 1.1.5,
  server `https://openapi.tossinvest.com`) — the **single source of truth**. No path or field is
  invented.
- **Contracts are reused, not redefined.** `BrokerOrderPort` (`place_order` / `cancel_order`),
  `OrderIntent`, `SubmissionResult`, `SubmissionStatus`, `Tif`, `OrderKind` come verbatim from
  SPEC-ORDER-001 (`src/ballast/orders/{ports,models}.py`). The Order Manager is unchanged; this SPEC
  adds exactly one concrete port implementation plus the minimal transport it needs.
- The order **modify** endpoint (`POST /api/v1/orders/{orderId}/modify`) is **out of scope** —
  `BrokerOrderPort` defines no `modify` method (justified in §Scope). It is documented as grounded /
  deferred so a later SPEC can adopt it cheaply.

---

# SPEC-ADAPTER-002 — Toss Securities Write (Order) Adapter (P3)

`@SPEC:SPEC-ADAPTER-002`

> Source of truth for all endpoints and field shapes: **`docs/reference/toss-openapi.json`**
> (토스증권 Open API, OpenAPI 3.1.0, v1.1.5, `https://openapi.tossinvest.com`).
> Where this document and the OpenAPI file disagree, the OpenAPI file wins.

## Environment

- Language: Python `>=3.11` (single language; financial values use `Decimal`).
- Packaging/deps: `uv` + `pyproject.toml`. **No new runtime deps** — reuses `httpx` and `pydantic` v2
  already added by ADAPTER-001. `respx` (dev-only) already present for mocked-HTTP tests.
- HTTP: `httpx` only (REST), via the existing `TossClient` (ADAPTER-001). The write path adds an
  authenticated **`POST`** capability to that client (header injection, envelope unwrap, error mapping,
  401 single re-auth, bounded retry — all reused from R3 of ADAPTER-001).
- Validation/modeling: `pydantic` v2 frozen DTOs (request body model), mirroring ADAPTER-001 style.
- Module location: `src/ballast/adapters/toss/orders.py` — the **IO boundary** (Hexagonal port-adapter),
  alongside the read adapters. It implements the `BrokerOrderPort` contract from SPEC-ORDER-001.
- Money/quantity: `OrderIntent.qty` / `OrderIntent.limit_price` are already `Decimal` (float rejected at
  construction by ORDER-001 `Money`/`OptionalMoney`). The adapter serializes them to JSON **strings**
  (the OpenAPI `format: decimal` shape) via `str(Decimal)`. **`float` never appears on the money path.**
- Secrets: OAuth2 credentials (`TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET`) from environment variables ONLY,
  reused via the ADAPTER-001 `TokenManager` / `TossClient`. Tokens live in memory only; never logged.
- Tests: `pytest`, `pytest-cov` with `httpx.MockTransport` / `respx`. **No live network.**
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: **No sandbox, no credentials in this environment.** Toss provides no sandbox; this SPEC is
  validated entirely by **mocked-HTTP unit tests** against payloads shaped per the OpenAPI schemas.
  **Live verification — a real-account 1-share submit/cancel "soak" — is a manual step the user performs
  locally** with their own keys. It is the _live acceptance gate_, not an automated check here, and
  there are no credentials in CI (see §Reality Constraints).
- A2: Auth, transport, envelope-unwrap, error-mapping, 401-reauth, and bounded retry are **reused from
  ADAPTER-001 R2/R3** unchanged. `POST /api/v1/orders` carries `Authorization: Bearer {token}` and the
  account header `X-Tossinvest-Account: {accountSeq}` (FD-A5, ADAPTER-001 FD5).
- A3: Order creation is the **quantity-based** `OrderCreateRequest` variant (`quantity`), because
  `OrderIntent` carries a quantity (`qty`), not an amount. The **amount-based** variant (`orderAmount`,
  US-MARKET only) is OUT of scope: `OrderIntent` has no amount field (FD4).
- A4: The idempotency key is the Toss request field `clientOrderId`. P2's deterministic
  `OrderIntent.client_order_id` already satisfies the Toss constraints (`^[a-zA-Z0-9\-_]+$`, ≤36 chars),
  so it is passed through **verbatim**. Toss treats a same-key replay within ~10 minutes as idempotent
  and returns the prior order result (FD2).
- A5: A Toss create **success** is HTTP 200 `{ "result": { orderId, clientOrderId? } }` (FD3). The
  success envelope is identical for a first accept and an idempotent replay — Toss exposes no flag
  distinguishing them — so the adapter uses its own in-process dedup cache to label a replay (FD6).
- A6: The Toss order-write endpoints address an order by the **server-generated `orderId`** (an opaque
  token), NOT by `clientOrderId`; and the Toss `Order` read schema does **not** carry `clientOrderId`
  (verified against the OpenAPI `Order` schema). Therefore the adapter resolves
  `client_order_id → orderId` from an **in-process map populated at `place_order` success** (FD7). A
  cross-process / cross-restart resolution is a pre-live follow-up (see §Dependencies, §Reality
  Constraints), tied to the backlog Reconciliation / State Store.
- A7: `OrderIntent.side` is the CORE `Side` enum whose values are exactly `"BUY"` / `"SELL"` — identical
  to the Toss `side` enum, so it passes through unchanged.
- A8: The adapter is **strategy-agnostic**. It forwards the intent it is given and lets Toss be the
  authority on tick size, price ranges, buying power, market hours, and fractional-quantity rules; those
  surface as typed Toss errors mapped to `SubmissionResult(FAILED, reason=…)`. The one cheap **fail-fast
  guard** the adapter performs locally is the CLS-on-non-US guard (R5), because the symbol shape makes it
  unambiguous for KR symbols and Toss would reject it anyway.

## Fixed Definitions

These are pinned against `docs/reference/toss-openapi.json` and MUST NOT drift silently. Changing any of
them requires a HISTORY entry and re-verification against the OpenAPI file. They extend (and do not
restate) ADAPTER-001's FD1–FD8.

- **FD1 — Create endpoint.** `POST /api/v1/orders` (`operationId: createOrder`), account-scoped (header
  `X-Tossinvest-Account`), request body `OrderCreateRequest`, success `200 { "result": OrderResponse }`.
- **FD2 — `clientOrderId` (idempotency key).** Request field on `OrderCreateRequest`:
  `type: string`, `pattern: ^[a-zA-Z0-9\-_]+$`, `maxLength: 36`. Per the OpenAPI description, a same-key
  re-request within **~10 minutes** returns the prior order result unchanged; after the window a same key
  becomes a new order. Omitting it disables idempotency (the adapter always sends it).
- **FD3 — `OrderResponse`.** `{ orderId: str (required), clientOrderId: str | null }`. `orderId` is the
  server-generated opaque identifier used by modify/cancel.
- **FD4 — `OrderCreateRequest` enums & fields (quantity-based variant).**
  - `symbol: str` (KRX = 6-digit numeric, US = alpha ticker).
  - `side ∈ { BUY, SELL }`.
  - `orderType ∈ { LIMIT, MARKET }`.
  - `timeInForce ∈ { DAY, CLS }`, default `DAY`. `LIMIT + CLS = LOC` (Limit-On-Close), **US-stock only**.
  - `quantity: str` (`format: decimal`, `pattern: ^\d+(\.\d+)?$`, `maxLength: 30`); fractional only for
    US `MARKET + SELL` (else integer; otherwise Toss `400 invalid-request`).
  - `price: str` (`format: decimal`); **required when `orderType=LIMIT`**, **must be absent when
    `orderType=MARKET`** (sending the wrong combination → `400 invalid-request`).
  - `confirmHighValueOrder: bool` (default `false`); the adapter does **not** auto-set `true` (≥1억 KRW
    orders return `400 confirm-high-value-required` — an intentional safety stop, see §Risks).
- **FD5 — Cancel endpoint.** `POST /api/v1/orders/{orderId}/cancel` (`operationId: cancelOrder`),
  account-scoped, empty/no body, success `200 { "result": OrderOperationResponse }`.
- **FD6 — `OrderOperationResponse`.** `{ orderId: str (required) }` — the **new** identifier issued for
  the cancel/modify; differs from the original order's `orderId`.
- **FD7 — Address-by-`orderId`, not `clientOrderId`.** Cancel/modify/get take the server `orderId`. The
  Toss `Order` read schema has no `clientOrderId` field, so the adapter keeps an in-process
  `{ client_order_id → orderId }` map filled on create success to satisfy `cancel_order`'s
  `client_order_id` argument.
- **FD8 — Idempotent-replay is indistinguishable in one response.** A first accept and a within-window
  replay both return `200 { result: { orderId } }`; the adapter's own dedup cache (not a response flag)
  decides `SUBMITTED` vs `DUPLICATE`. The 409 `idempotency-key-conflict` (same key, **different** body)
  is a distinct **error**, not a duplicate-success.
- **FD9 — Create error codes (grounded).** `400`: `invalid-request` (covers
  invalid `orderType`/`timeInForce`/`side`, `quantity,orderAmount` required, `price` required for LIMIT,
  invalid tick size, fractional-quantity-US-SELL-only, fractional-quantity-scale-exceeded,
  `clsConditionNotMet`), `confirm-high-value-required`, `account-header-required`. `409`:
  `request-in-progress`, `idempotency-key-conflict`. `422`: `insufficient-buying-power`,
  `order-hours-closed`, `stock-restricted`, `price-out-of-range`, `opposite-pending-order-exists`,
  `order-type-not-allowed`, `prerequisite-required`, `market-not-supported-for-stock`,
  `investor-exchange-not-integrated`, `amount-order-outside-regular-hours`,
  `fractional-quantity-outside-regular-hours`, `account-restricted`, `max-order-amount-exceeded`.
  `500`: `internal-error`, `maintenance`. `429`: `rate-limit-exceeded`.
- **FD10 — Cancel/modify error codes (grounded).** `404`: `order-not-found`, `account-not-found`. `409`:
  `already-filled`, `already-canceled`, `already-modified`, `already-rejected`, `already-processing`.
  `422` (cancel): `cancel-restricted`, `order-hours-closed`. `422` (modify): `modify-restricted`,
  `order-hours-closed`, plus business-rule codes shared with create.

## Requirements

The adapter MUST satisfy the following EARS requirements (≤5 modules). All are tagged to
`@SPEC:SPEC-ADAPTER-002`. Each maps an existing `BrokerOrderPort` method or transport concern onto the
grounded Toss order endpoints — no new public contract is invented.

### REQ-ADAPTER-002-R1 — Concrete `BrokerOrderPort` write adapter + intent→create mapping (Ubiquitous + Event-driven)

`@SPEC:SPEC-ADAPTER-002` `REQ-ADAPTER-002-R1`

The system **shall always** provide a concrete `TossOrderAdapter` in
`src/ballast/adapters/toss/orders.py` that **structurally satisfies** the `BrokerOrderPort` Protocol
from `src/ballast/orders/ports.py` (`place_order(intent) -> SubmissionResult`,
`cancel_order(account_seq, client_order_id) -> SubmissionResult`) over the authenticated `TossClient`.
It **shall reuse** `OrderIntent` / `SubmissionResult` / `SubmissionStatus` from
`src/ballast/orders/models.py` and **shall not** redefine them.

- **When** `place_order(intent)` is called, the system **shall** map the broker-neutral `OrderIntent`
  onto a quantity-based `OrderCreateRequest` (FD1/FD4) and `POST /api/v1/orders` with
  `X-Tossinvest-Account: {intent.account_seq}`, using the mapping:
  - `kind=LIMIT, tif=DAY` → `orderType=LIMIT`, `timeInForce=DAY`, `price=str(limit_price)`.
  - `kind=LIMIT, tif=CLS` → `orderType=LIMIT`, `timeInForce=CLS` (LOC; **US-stock only**, R5 guard),
    `price=str(limit_price)`.
  - `kind=MARKET` → `orderType=MARKET`, **no `price` field** (and **no** `timeInForce` other than the
    default semantics — DAY).
  - `side` ← `intent.side` (`BUY`/`SELL`, passthrough), `symbol` ← `intent.ticker`,
    `quantity=str(intent.qty)`, `clientOrderId` ← `intent.client_order_id` (R2 passthrough).
- All money/quantity values **shall** be serialized as decimal **strings** from `Decimal`
  (`str(Decimal)`); the system **shall not** introduce any `float` on the money path (FD4, R5).
- The `TossClient` **shall** be extended with an authenticated `post(path, *, json, account_seq)` that
  reuses ADAPTER-001 R3 header injection, `{ "result": … }` unwrap, typed-error mapping, exactly-one
  401 re-auth, and bounded retry/backoff. No new transport semantics are introduced.

### REQ-ADAPTER-002-R2 — `clientOrderId` passthrough & idempotent-replay → DUPLICATE (State-driven)

`@SPEC:SPEC-ADAPTER-002` `REQ-ADAPTER-002-R2`

The deterministic `OrderIntent.client_order_id` (already conforming to `^[a-zA-Z0-9\-_]+$`, ≤36 chars) is
the idempotency key passed **verbatim** as the Toss `clientOrderId` (FD2).

- **When** a `place_order` for a `client_order_id` **not** seen in this process succeeds (200), the
  system **shall** return `SubmissionResult(status=SUBMITTED, broker_order_id=<OrderResponse.orderId>,
client_order_id=…)` and record `client_order_id → orderId` in its in-process dedup/resolution map (FD7).
- **While** the same `client_order_id` has already been submitted in this process and Toss returns 200
  again (an idempotent within-window replay; FD8), the system **shall** return the prior result relabeled
  `SubmissionStatus.DUPLICATE` (mirroring `RecordingBrokerOrderPort` semantics from ORDER-001 R5), and
  **shall not** treat the replay as a second live order.
- The system **shall** rely on Toss's ~10-minute idempotency window so that a retried same-key submit is
  safe (no duplicate live order) and **shall not** perform any silent unbounded resubmission.
- **If** Toss returns `409 idempotency-key-conflict` (same key, **different** body), the system **shall**
  surface it as `SubmissionResult(FAILED, reason="idempotency-key-conflict: …")` (a programming error,
  not a duplicate-success; R4).

### REQ-ADAPTER-002-R3 — Cancel via Toss cancel endpoint (Event-driven + Unwanted)

`@SPEC:SPEC-ADAPTER-002` `REQ-ADAPTER-002-R3`

- **When** `cancel_order(account_seq, client_order_id)` is called, the system **shall** resolve
  `client_order_id → orderId` via its in-process map (FD7), then `POST /api/v1/orders/{orderId}/cancel`
  with `X-Tossinvest-Account: {account_seq}` and map a 200 `OrderOperationResponse` to
  `SubmissionResult(status=SUBMITTED, broker_order_id=<new orderId>, client_order_id=…)` (FD5/FD6).
- **If** the `client_order_id` is unknown to the in-process map (e.g. created in a previous process), the
  system **shall** return `SubmissionResult(FAILED, reason="unknown client_order_id: …")` **without
  guessing** an `orderId` and **without** calling Toss (cross-process resolution is a pre-live follow-up;
  §Dependencies).
- **When** Toss returns a cancel error (`404 order-not-found`/`account-not-found`, `409 already-*`,
  `422 cancel-restricted`/`order-hours-closed`; FD10), the system **shall** map it to
  `SubmissionResult(FAILED, reason="<code>: <message>")` (R4) and **shall not** retry a non-transient
  cancel error.
- The order **modify** endpoint is **out of scope** (`BrokerOrderPort` has no `modify`; §Scope).

### REQ-ADAPTER-002-R4 — Response & error mapping to `SubmissionResult`; no silent infinite retry (Event-driven + Unwanted)

`@SPEC:SPEC-ADAPTER-002` `REQ-ADAPTER-002-R4`

The live path **shall always** return a `SubmissionResult` to the Order Manager — it **shall not** leak a
`TossError` exception onto the caller.

- **When** a create/cancel call succeeds (200), the system **shall** return `SubmissionResult` per R1–R3
  (`SUBMITTED` / `DUPLICATE`), carrying `broker_order_id = <Toss orderId>`.
- **When** the `TossClient` raises a typed error (`TossAuthError`, `TossRateLimitError`, `TossApiError`)
  after its ADAPTER-001 R3 handling (exactly-one 401 re-auth; bounded retry honoring `Retry-After` on
  `429`/`5xx`/timeout; FD9), the system **shall** catch it and return
  `SubmissionResult(status=FAILED, reason="<error.code>: <error.message>")`, preserving the stable Toss
  `code` so the operator/manager can act. Reuse `ballast.adapters.errors` types as the source of `code`.
- The system **shall not** perform any silent unbounded retry: retries are bounded by the existing
  `TossClient` policy (max 3 attempts, transient classes only); on exhaustion the mapped typed error
  becomes `FAILED`. Non-transient business errors (`400`/`404`/`409`/`422`) are **never** retried.
- The system **shall not** log or expose any secret/token; it **shall** log only stable identifiers
  (`client_order_id`, Toss `code`, `requestId`) for traceability (secrets policy, reused from
  ADAPTER-001).

### REQ-ADAPTER-002-R5 — US-stock CLS guard + Decimal-only + secrets constraints (Unwanted + Ubiquitous)

`@SPEC:SPEC-ADAPTER-002` `REQ-ADAPTER-002-R5`

- **If** `intent.tif == Tif.CLS` and `intent.ticker` is a clearly **KR** symbol (all-digit / 6-digit
  numeric, per the OpenAPI symbol convention), the system **shall** fail fast with
  `SubmissionResult(FAILED, reason="CLS (LOC) is supported for US stocks only")` **without** calling Toss
  (which would return `400 clsConditionNotMet` anyway; FD4/FD9). For ambiguous (non-numeric) symbols the
  system **shall** forward to Toss and let it be the authority.
- The system **shall** serialize `qty` and `limit_price` as decimal **strings** from `Decimal` and
  **shall not** accept or emit a `float` anywhere on the money path (the upstream `OrderIntent` already
  rejects bare `float` at construction; the adapter preserves this invariant).
- The system **shall not** auto-set `confirmHighValueOrder=true`; a ≥1억 KRW order therefore surfaces as
  `FAILED (confirm-high-value-required)` rather than being silently confirmed (intentional safety stop).
- The system **shall not** place any secret, token, or full credential into a log line, exception
  message, `SubmissionResult.reason`, or DTO `repr` (reuses ADAPTER-001 redaction; tokens stay in
  memory only).

## Specifications

Port method / concern (REQ) → Toss endpoint (cited path from `docs/reference/toss-openapi.json`) →
implementing module.

| Port method / concern (REQ)                 | Toss endpoint (source of truth)                                                   | Module                                          |
| ------------------------------------------- | --------------------------------------------------------------------------------- | ----------------------------------------------- |
| `BrokerOrderPort.place_order` `R1/R2/R4/R5` | `POST /api/v1/orders` (`createOrder`, `X-Tossinvest-Account`) → `OrderResponse`   | `src/ballast/adapters/toss/orders.py`           |
| `BrokerOrderPort.cancel_order` `R3/R4`      | `POST /api/v1/orders/{orderId}/cancel` (`cancelOrder`) → `OrderOperationResponse` | `src/ballast/adapters/toss/orders.py`           |
| _(authenticated POST transport)_ `R1/R4`    | applies to both calls above (reuses ADAPTER-001 R3 semantics)                     | `src/ballast/adapters/toss/client.py` (+`post`) |
| _(factory wiring `from_env`)_ `R1`          | n/a (bootstrap)                                                                   | `src/ballast/adapters/toss/factory.py`          |
| _(modify — OUT of scope; deferred)_         | `POST /api/v1/orders/{orderId}/modify` (`modifyOrder`) → `OrderOperationResponse` | _(future SPEC; no `BrokerOrderPort.modify`)_    |

### `OrderIntent` → Toss `OrderCreateRequest` field mapping (REQ-ADAPTER-002-R1)

| `OrderIntent` field            | Toss request field            | Mapping rule                                                                             |
| ------------------------------ | ----------------------------- | ---------------------------------------------------------------------------------------- |
| `client_order_id: str`         | `clientOrderId`               | Verbatim passthrough (already `^[a-zA-Z0-9\-_]+$`, ≤36) (R2, FD2).                       |
| `account_seq: str`             | `X-Tossinvest-Account` header | Sent as the integer accountSeq header, not in the body (FD1).                            |
| `side: Side`                   | `side`                        | `BUY`/`SELL` passthrough (identical enum) (A7).                                          |
| `ticker: str`                  | `symbol`                      | Verbatim (KRX 6-digit numeric / US alpha) (FD4).                                         |
| `qty: Decimal`                 | `quantity`                    | `str(Decimal)` decimal string; quantity-based variant only (A3, FD4).                    |
| `kind=LIMIT`                   | `orderType=LIMIT`             | + `price=str(limit_price)` (required for LIMIT; FD4).                                    |
| `kind=MARKET`                  | `orderType=MARKET`            | **no** `price` field (FD4).                                                              |
| `tif=DAY`                      | `timeInForce=DAY`             | Day order (FD4).                                                                         |
| `tif=CLS`                      | `timeInForce=CLS`             | LOC = `LIMIT + CLS`; **US-stock only** (R5 guard) (FD4).                                 |
| `limit_price: Decimal \| None` | `price`                       | `str(Decimal)` when LIMIT; omitted for MARKET. `None` + LIMIT → Toss `400` (see §Risks). |

### Toss response/error → `SubmissionResult` mapping (REQ-ADAPTER-002-R2/R3/R4)

| Toss outcome                                                | `SubmissionResult`                                                            |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `200` create, first time for this `client_order_id`         | `SUBMITTED`, `broker_order_id=orderId`                                        |
| `200` create, in-process dedup hit (idempotent replay)      | `DUPLICATE`, `broker_order_id=orderId` (prior result relabeled)               |
| `200` cancel (`OrderOperationResponse`)                     | `SUBMITTED`, `broker_order_id=<new orderId>`                                  |
| `409 idempotency-key-conflict` (same key, different body)   | `FAILED`, `reason="idempotency-key-conflict: …"`                              |
| `409 request-in-progress` / `409 already-*`                 | `FAILED`, `reason="<code>: …"` (non-transient; not retried)                   |
| `400 invalid-request` (incl. `clsConditionNotMet`, tick…)   | `FAILED`, `reason="invalid-request: …"`                                       |
| `400 confirm-high-value-required`                           | `FAILED`, `reason="confirm-high-value-required: …"` (safety stop)             |
| `404 order-not-found` / `account-not-found` (cancel)        | `FAILED`, `reason="<code>: …"`                                                |
| `422` business-rule (buying-power, hours, restricted, …)    | `FAILED`, `reason="<code>: …"`                                                |
| `401` (after single re-auth), `429`/`5xx`/timeout exhausted | `FAILED`, `reason="<code>: …"` (no silent infinite retry; R4)                 |
| Local CLS-on-KR guard (R5)                                  | `FAILED`, `reason="CLS (LOC) is supported for US stocks only"` (no HTTP call) |
| Unknown `client_order_id` on cancel (R3)                    | `FAILED`, `reason="unknown client_order_id: …"` (no HTTP call)                |

## Dependencies

- **Depends on SPEC-ORDER-001 (P2)**: reuses `BrokerOrderPort`, `OrderIntent`, `SubmissionResult`,
  `SubmissionStatus`, `Tif`, `OrderKind`, `Side` verbatim. The Order Manager (guards, dry-run gate,
  kill-switch, `max_position_pct` clamp) is **unchanged**; this SPEC only provides the missing concrete
  port the manager calls when `execution.dry_run=False`.
- **Depends on SPEC-ADAPTER-001 (P1)**: reuses `TokenManager` (OAuth2), `TossClient` (transport, envelope
  unwrap, error mapping, 401 re-auth, bounded retry), `ballast.adapters.errors`
  (`TossAuthError`/`TossApiError`/`TossRateLimitError`), and `from_env` wiring. Adds an authenticated
  `POST` to `TossClient` and a `TossOrderAdapter` to the `from_env` surface.
- **Source of truth**: `docs/reference/toss-openapi.json` (v1.1.5).
- **Pre-live follow-ups (HANDOFF §4 — NOT in scope here; raised as pre-live risks/dependencies):**
  - 🔸 **MAB quarter-sell LOC price.** `mab_on_seed_exhausted` emits `OrderType.LOC` with
    `limit_price=None` (effectively MOC). Toss has **no** market-on-close; a price-less `LIMIT + CLS` is
    rejected (`price` required for LIMIT). A _correct_ live MAB quarter-sell therefore needs a defined LOC
    price **before** live submission. The adapter itself is strategy-agnostic and merely surfaces Toss's
    `400` as `FAILED` — it does not invent a price.
  - 🔸 **VR `V_n` multi-cycle evolution** is a backtest-fidelity concern (Strategy contract), independent
    of the adapter; noted so it is not mistaken for an adapter requirement.
  - 🔸 **Cross-process cancel resolution.** Resolving `client_order_id → orderId` across restarts requires
    the backlog Reconciliation / State Store; until then `cancel_order` only resolves keys created in the
    same process (R3).

## Scope

### In scope (WRITE / live submission, P3)

- `TossOrderAdapter` implementing `BrokerOrderPort.place_order` (`POST /api/v1/orders`, quantity-based)
  and `cancel_order` (`POST /api/v1/orders/{orderId}/cancel`), with the intent→create mapping (R1),
  `clientOrderId` passthrough + idempotent-replay → `DUPLICATE` (R2), response/error → `SubmissionResult`
  mapping (R4), and the CLS/Decimal/secrets guards (R5).
- The minimal authenticated `POST` capability added to `TossClient`, reusing ADAPTER-001 R3 semantics.
- `from_env` wiring to expose the order adapter alongside the read adapters.

### Out of scope

- **Order modify** (`POST /api/v1/orders/{orderId}/modify`). **Justification:** `BrokerOrderPort` (the
  contract this SPEC implements exactly) defines **no** `modify` method, and the Order Manager never calls
  one; the broker-neutral amend pattern in P2 is **cancel + re-submit a fresh idempotent order**, not
  in-place modify. Adding a `modify` method would (a) break the "implement the contract exactly, do not
  redefine" constraint, and (b) yield dead code with no caller. The endpoint is grounded and documented
  (FD-adjacent, Specifications table) so a **future** SPEC can adopt it cheaply once `BrokerOrderPort`
  grows a `modify` method and a caller exists. → **Open question surfaced for orchestrator review.**
- **Amount-based order creation** (`orderAmount`, US-MARKET only): `OrderIntent` has no amount field (A3).
- Any change to the Order Manager, strategies, or CORE — all unchanged.
- Automated live submission against the real Toss API (manual, user-local; §Reality Constraints).

## Secrets Policy

- OAuth2 credentials are read from environment variables ONLY (via ADAPTER-001 `from_env`), never
  committed, never logged. The `access_token` stays in memory.
- No secret/token appears in any log line, exception message, `SubmissionResult.reason`, or `repr`.
  Structured logging carries `client_order_id` / Toss `code` / `requestId` for traceability — never
  credentials.

## Reality Constraints

- **No sandbox + no credentials in this environment** → ADAPTER-002 is validated by **mocked-HTTP unit
  tests** (`httpx.MockTransport` / `respx`) against payloads shaped per the OpenAPI schemas. A
  **real-account 1-share live submit/cancel "soak" is performed by the user locally** with their keys;
  that is the live acceptance step, **not** an automated gate in CI (see `acceptance.md`).
- **No new runtime deps.** Money is `Decimal`; `float` is never on the money path.
- **Idempotency is bounded by Toss (~10 min)**; the adapter adds an in-process dedup/resolution cache but
  performs no unbounded retry.

## Traceability

- `@SPEC:SPEC-ADAPTER-002` — this document.
- `@TEST:SPEC-ADAPTER-002` — see `acceptance.md` Given/When/Then scenarios + `tests/unit/adapters/test_orders.py`.
- `@CODE:SPEC-ADAPTER-002` — `src/ballast/adapters/toss/orders.py`, `src/ballast/adapters/toss/client.py`
  (`post`), `src/ballast/adapters/toss/factory.py` (wiring).
- `@DOC:SPEC-ADAPTER-002` — generated during `/moai:3-sync`.

### Requirement Index

| Requirement ID     | EARS Type                 | Summary                                                                                                            |
| ------------------ | ------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| REQ-ADAPTER-002-R1 | Ubiquitous + Event-driven | Concrete `TossOrderAdapter` (`BrokerOrderPort`) + `OrderIntent`→`OrderCreateRequest` mapping; authenticated `POST` |
| REQ-ADAPTER-002-R2 | State-driven              | `clientOrderId` passthrough; in-process dedup → idempotent-replay maps to `DUPLICATE`                              |
| REQ-ADAPTER-002-R3 | Event-driven + Unwanted   | `cancel_order` via `/cancel` (resolve `client_order_id`→`orderId`); modify out of scope                            |
| REQ-ADAPTER-002-R4 | Event-driven + Unwanted   | Toss response/error → `SubmissionResult` (`SUBMITTED`/`DUPLICATE`/`FAILED`); no silent infinite retry              |
| REQ-ADAPTER-002-R5 | Unwanted + Ubiquitous     | US-stock CLS fail-fast guard; Decimal-only money; no auto high-value confirm; secrets never logged                 |
