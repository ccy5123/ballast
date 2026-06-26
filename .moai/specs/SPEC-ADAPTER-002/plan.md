# SPEC-ADAPTER-002 — Implementation Plan

`@SPEC:SPEC-ADAPTER-002`

> Source of truth for all endpoints/fields: `docs/reference/toss-openapi.json` (v1.1.5).
> Write (order-submission) adapter. Implements the existing `BrokerOrderPort` (SPEC-ORDER-001) over the
> existing `TossClient` (SPEC-ADAPTER-001). No contract is redefined; the Order Manager is unchanged.

## Module Layout

The single new module lives under `src/ballast/adapters/toss/` (the IO boundary), alongside the
read adapters. The only edits to existing files are an additive `post()` on `TossClient` and the
`from_env` wiring.

```
src/ballast/adapters/
├── errors.py            # (reused) TossAuthError, TossApiError, TossRateLimitError  [R4]
└── toss/
    ├── client.py        # (EDIT) add authenticated post(path, *, json, account_seq) reusing R3
    │                    #   header injection / {result} unwrap / error mapping / 401 re-auth / retry  [R1/R4]
    ├── orders.py        # (NEW) TossOrderAdapter implements BrokerOrderPort:
    │                    #   place_order (POST /api/v1/orders) + cancel_order (.../cancel),
    │                    #   intent→create mapping, clientOrderId passthrough, dedup→DUPLICATE,
    │                    #   error→SubmissionResult, CLS-on-non-US guard, Decimal-only  [R1–R5]
    └── factory.py       # (EDIT) expose TossOrderAdapter(client) on TossAdapters / from_env  [R1]

src/ballast/orders/      # (reused, UNCHANGED) ports.py, models.py — BrokerOrderPort, OrderIntent, …
```

Tests:

```
tests/unit/adapters/
└── test_orders.py       # (NEW) mocked-HTTP (respx / httpx.MockTransport) scenarios for R1–R5  [@TEST]
```

(`test_client.py` gains a small `post()` case; `test_factory.py` gains an order-adapter-present case.)

## Dependencies to Add

| Dependency | Where | Version | Reason                                                                  |
| ---------- | ----- | ------- | ----------------------------------------------------------------------- |
| _(none)_   | —     | —       | Reuses `httpx`/`pydantic` (runtime) and `respx` (dev) from ADAPTER-001. |

No `pandas`/`numpy` (backtest-only per Constitution). No WebSocket libs.

## Architectural Approach

- **One new port implementation.** `TossOrderAdapter` is the concrete `BrokerOrderPort` that P2's Order
  Manager already calls on the live path (`execution.dry_run=False`). It is the production sibling of
  `RecordingBrokerOrderPort` (ORDER-001 R5) and matches its idempotent `SUBMITTED`/`DUPLICATE` semantics.
- **Reuse the transport.** All HTTP concerns (Bearer + `X-Tossinvest-Account` injection, `{ "result" }`
  unwrap, typed-error mapping, exactly-one 401 re-auth, bounded retry honoring `Retry-After`) already
  live in `TossClient` (ADAPTER-001 R3). This SPEC adds an authenticated `post()` that flows through the
  same machinery — it does **not** re-implement retry/error logic in the adapter.
- **Decimal at the boundary.** `OrderIntent.qty` / `limit_price` are already `Decimal` (float rejected at
  construction by ORDER-001 `Money`/`OptionalMoney`). The adapter serializes them to the OpenAPI
  `format: decimal` **string** shape with `str(Decimal)`. Nothing on the money path is ever a `float`.
- **Adapter returns results, never exceptions.** The live path catches every `TossError` and returns a
  `SubmissionResult(FAILED, reason=…)`, so the Order Manager always receives a structured outcome.

## `TossClient.post` Extension (R1/R4)

Add a method mirroring the existing `get`:

- `post(path, *, json: Mapping | None = None, account_seq: str | None = None) -> Any`
  - Build headers via the existing `_build_headers(account_seq)` (Bearer always; account header when
    scoped; FD1).
  - Issue `self._http.post(path, json=json, headers=headers)`.
  - On `200` → `_unwrap(response)` (returns `result`; FD3/FD6).
  - On transient failure (`429`/`5xx`/timeout) → reuse `_is_transient` + `_backoff` (bounded, max 3,
    honors `Retry-After`).
  - On `401` (auth) → reuse the single re-auth + retry guard (no loop).
  - On any other error → `_to_exception(...)` (typed `TossAuthError`/`TossRateLimitError`/`TossApiError`).
  - Secrets never logged (reused redaction; the JSON body carries only order params, never credentials).

This keeps `get` and `post` behaviorally identical except for the verb + body, so the adapter inherits
all ADAPTER-001 R3 guarantees for free.

## `OrderIntent` → `OrderCreateRequest` Mapping (R1/R5)

Build the quantity-based request body (FD4), omitting `price` for MARKET:

| Intent                | Body fields produced                                                                     |
| --------------------- | ---------------------------------------------------------------------------------------- |
| `kind=LIMIT, tif=DAY` | `orderType="LIMIT"`, `timeInForce="DAY"`, `price=str(limit_price)`                       |
| `kind=LIMIT, tif=CLS` | `orderType="LIMIT"`, `timeInForce="CLS"`, `price=str(limit_price)` (US-only; R5)         |
| `kind=MARKET`         | `orderType="MARKET"`, `timeInForce="DAY"`, **no `price`**                                |
| (always)              | `clientOrderId=client_order_id`, `symbol=ticker`, `side=side.value`, `quantity=str(qty)` |

- The account is sent as the header `X-Tossinvest-Account: {account_seq}` (not in the body).
- `confirmHighValueOrder` is **never** set to `true` (omitted → defaults `false`; R5 safety stop).
- **CLS guard (R5):** before mapping, if `tif == CLS` and `ticker` is all-digit (KR 6-digit numeric),
  return `FAILED("CLS (LOC) is supported for US stocks only")` with **no** HTTP call. Ambiguous
  (non-numeric) → forward and let Toss decide (`clsConditionNotMet`).

## Request / Response DTOs

- **Request body**: a small frozen pydantic model (e.g. `_OrderCreateBody`) or a plain `dict[str, str]`
  assembled by the mapping function — money fields are decimal **strings**, `price` omitted for MARKET.
  Built strictly from `OrderIntent`; no `float`.
- **Response (create)**: `OrderResponse = { orderId: str, clientOrderId: str | null }` → take `orderId`
  as `broker_order_id`.
- **Response (cancel)**: `OrderOperationResponse = { orderId: str }` → the **new** id as
  `broker_order_id` (differs from the original; FD6).
- **Errors**: `ErrorResponse.error.{code, message, requestId}` already parsed/typed by `TossClient`; the
  adapter reads `error.code` / `error.message` to build `SubmissionResult.reason`.

## Idempotency / Dedup & Cancel-Resolution Flow (R2/R3)

The adapter holds a single in-process map `_submitted: dict[str, _Record]` keyed by `client_order_id`,
where `_Record` carries the `orderId` and the original `SubmissionResult`:

- `place_order(intent)`:
  1. (R5) CLS-on-KR guard → maybe early `FAILED`.
  2. If `intent.client_order_id` already in `_submitted` → POST again (Toss is idempotent within ~10 min,
     FD8); on `200`, return the prior result relabeled `DUPLICATE` (matches `RecordingBrokerOrderPort`).
     _(Alternatively short-circuit to the cached result without re-POSTing; either is acceptable — the
     network call is the safer, replay-verifying choice and is what the tests assert.)_
  3. Else POST `/api/v1/orders`; on `200` record `client_order_id → orderId` and return `SUBMITTED`.
  4. On any `TossError` → `FAILED` (R4). `409 idempotency-key-conflict` → `FAILED` (NOT duplicate).
- `cancel_order(account_seq, client_order_id)`:
  1. Look up `orderId` in `_submitted`. **Unknown** → `FAILED("unknown client_order_id: …")`, no HTTP.
  2. POST `/api/v1/orders/{orderId}/cancel`; on `200` → `SUBMITTED` with the new `orderId`.
  3. On `TossError` → `FAILED` (R4).

> Cross-restart resolution (a `client_order_id` created in a previous process) is intentionally a
> `FAILED("unknown …")` rather than a guess — it depends on the backlog Reconciliation / State Store
> (HANDOFF §4/§5). This keeps the adapter honest and avoids cancelling the wrong order.

## Error-Mapping Table (R4)

| Source (after `TossClient` handling)                                                          | `SubmissionResult`                                        |
| --------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `200` create (new key)                                                                        | `SUBMITTED`, `broker_order_id=orderId`                    |
| `200` create (dedup hit)                                                                      | `DUPLICATE`, `broker_order_id=orderId`                    |
| `200` cancel                                                                                  | `SUBMITTED`, `broker_order_id=<new orderId>`              |
| `TossApiError(code="invalid-request" / "stock-restricted" / "insufficient-buying-power" / …)` | `FAILED`, `reason="<code>: <message>"`                    |
| `TossApiError(code="idempotency-key-conflict")`                                               | `FAILED` (programming error; same key, different body)    |
| `TossApiError(code="request-in-progress" / "already-*")`                                      | `FAILED` (non-transient; not retried)                     |
| `TossApiError(code="order-not-found" / "account-not-found")`                                  | `FAILED` (cancel)                                         |
| `TossApiError(code="confirm-high-value-required")`                                            | `FAILED` (intentional safety stop; never auto-confirmed)  |
| `TossAuthError` (after single re-auth)                                                        | `FAILED`, `reason="<code>: …"`                            |
| `TossRateLimitError` / `5xx` / timeout (after bounded retry)                                  | `FAILED`, `reason="<code>: …"` (no silent infinite retry) |
| Local CLS-on-KR guard / unknown client_order_id                                               | `FAILED` (no HTTP call)                                   |

## Decimal-at-Boundary & Secrets Policy

- The adapter is the only place that turns `OrderIntent` `Decimal`s into request strings (`str(Decimal)`)
  and Toss `orderId` strings into `broker_order_id`. No `float` is ever introduced.
- Secrets/tokens: reused ADAPTER-001 redaction (Bearer injected by `TossClient`, never logged). The
  adapter logs only `client_order_id` / Toss `code` / `requestId`. `SubmissionResult.reason` carries the
  stable Toss `code` + message, never a token.

## Risk Analysis

| Risk                              | Description                                                                         | Mitigation                                                                                                                     |
| --------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| MAB quarter-sell LOC price (§4)   | `mab_on_seed_exhausted` emits `LIMIT+CLS` with `limit_price=None`; Toss has no MOC. | Adapter is strategy-agnostic: forwards as given, surfaces Toss `400 (price required)` as `FAILED`. Pre-live: define LOC price. |
| CLS on KR symbol                  | A KR symbol with `CLS` would `400 clsConditionNotMet`.                              | Local fail-fast guard (R5) returns `FAILED` without an HTTP call; ambiguous symbols forwarded to Toss.                         |
| Idempotent replay vs first accept | Both return identical `200 { orderId }`; no response flag distinguishes them.       | In-process dedup cache decides `SUBMITTED` vs `DUPLICATE` (FD8); mirrors `RecordingBrokerOrderPort` semantics.                 |
| Cancel-by-clientOrderId           | Toss addresses by server `orderId`; `Order` schema has no `clientOrderId`.          | In-process `client_order_id → orderId` map (FD7). Unknown key → `FAILED("unknown …")`; cross-restart is a follow-up.           |
| Idempotency-key-conflict          | Same key, different body → `409 idempotency-key-conflict`.                          | Mapped to `FAILED` (a loud programming error), NOT silently treated as a duplicate-success.                                    |
| High-value confirmation           | ≥1억 KRW order requires `confirmHighValueOrder=true`.                               | Adapter never auto-confirms → surfaces `FAILED (confirm-high-value-required)` (intentional safety stop).                       |
| Silent infinite retry             | Naive resubmission could spam live orders.                                          | Only the bounded `TossClient` retry (max 3, transient classes); business errors never retried; exhaustion → `FAILED`.          |
| Float on money path               | A `float` qty/price could corrupt money.                                            | `OrderIntent` rejects `float` at construction; adapter serializes `str(Decimal)` only.                                         |
| Secret/token leak                 | Token in a log, `reason`, or `repr`.                                                | Reused ADAPTER-001 redaction; tests assert no secret appears in logs/reason/repr.                                              |
| No sandbox                        | Cannot exercise real submission in CI.                                              | Mocked-HTTP tests from OpenAPI schemas; live 1-share soak delegated to the user (manual, local).                               |

## Test Approach (Mocked-HTTP)

Per-module plan for `tests/unit/adapters/test_orders.py` (using `respx` / `httpx.MockTransport`, with the
injected clock + mocked `/oauth2/token` exactly as `test_account.py` wires the read adapters):

- **Structural conformance**: `assert isinstance(TossOrderAdapter(client), BrokerOrderPort)`
  (runtime-checkable Protocol from ORDER-001).
- **Mapping (R1)** — three respx fixtures asserting the **request body**:
  - LIMIT+DAY → `{ orderType:"LIMIT", timeInForce:"DAY", price:"70000", quantity:"10",
side:"BUY", symbol:"005930", clientOrderId:"…" }` (KR limit buy example).
  - LIMIT+CLS (US) → `{ orderType:"LIMIT", timeInForce:"CLS", price:"185.5", symbol:"AAPL", … }`
    (`usLocBuy` example).
  - MARKET → `{ orderType:"MARKET", quantity:"…" }` with **no `price`** key present.
- **clientOrderId passthrough + idempotent replay → DUPLICATE (R2)**: first `place_order` → `SUBMITTED`
  with `broker_order_id=orderId`; a second `place_order` with the **same** `client_order_id` (Toss mock
  returns the same `orderId`) → `DUPLICATE`.
- **idempotency-key-conflict (R2/R4)**: `409 idempotency-key-conflict` → `FAILED` (not DUPLICATE).
- **cancel success (R3)**: place then cancel by `client_order_id` → resolves `orderId`, POSTs `/cancel`,
  returns `SUBMITTED` with the new `orderId`.
- **cancel unknown key (R3)**: cancel a `client_order_id` never placed in this process → `FAILED("unknown
client_order_id: …")`, **no** HTTP route hit (assert respx call count 0 for `/cancel`).
- **cancel failure (R3/R4)**: `409 already-filled` / `404 order-not-found` / `422 cancel-restricted` →
  `FAILED` with the code.
- **error → FAILED (R4)**: create `422 insufficient-buying-power`, `400 invalid-request`,
  `400 confirm-high-value-required` → `FAILED` carrying `code`; `429`/`5xx` after bounded retry → `FAILED`.
- **CLS-on-non-US guard (R5)**: `tif=CLS`, `ticker="005930"` (KR numeric) → `FAILED` with **no** HTTP
  call; `ticker="AAPL"` + CLS is forwarded (mock 200) → `SUBMITTED`.
- **Decimal / no-float (R5)**: assert serialized `quantity`/`price` are exact decimal strings (e.g.
  `"70000"`, `"185.5"`), never float-formatted; the construction path rejects `float` upstream.
- **secrets never logged (R5)**: with `caplog`, assert no token/secret string appears in any log record,
  exception, or `SubmissionResult.reason`; `requestId` IS present for traceability.
- Inject the clock + transport into `TossClient`/`TokenManager` for determinism (reuse the
  `tests/unit/adapters/conftest.py` `clock` fixture).

## Manual Live-Validation "Soak" Checklist (User, Local — NOT an automated gate)

Performed by the user with real `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET` against
`https://openapi.tossinvest.com`, on a **separate strategy account**, with the smallest size (1 share):

1. Export credentials as env vars (confirm they are NOT in the repo); build `from_env()`.
2. `GET /api/v1/accounts` → record a real `accountSeq` (read-only sanity, ADAPTER-001).
3. Construct a minimal `OrderIntent` (1 share, a price safely away from the market so it rests
   unfilled) and `place_order` → confirm `SUBMITTED` + a real `orderId`.
4. Re-`place_order` with the **same** `client_order_id` within ~10 min → confirm `DUPLICATE` (no second
   live order; verify via `list_orders`).
5. `cancel_order(accountSeq, client_order_id)` → confirm `SUBMITTED` and the order is canceled
   (`list_orders` / `get_order` shows canceled).
6. (US, optional) a 1-share `LIMIT + CLS` (LOC) on a US ticker during the allowed window → confirm accept.
7. Grep logs to confirm no secret/token leaked; confirm `reason` on any `FAILED` carries only Toss `code`.
8. Do **not** exercise modify (out of scope). Do **not** auto-confirm any ≥1억 order.

## Milestones (priority-ordered, no time estimates)

- **Primary Goal (Priority High)**: `TossClient.post` (R1/R4) + `TossOrderAdapter.place_order` with the
  intent→create mapping (R1) and response/error → `SubmissionResult` (R4).
- **Secondary Goal (Priority High)**: `clientOrderId` passthrough + in-process dedup → `DUPLICATE` (R2)
  and the CLS-on-non-US guard + Decimal/secrets constraints (R5).
- **Final Goal (Priority Medium)**: `cancel_order` with `client_order_id → orderId` resolution and
  cancel error mapping (R3); `from_env` wiring of the order adapter.
- **Optional Goal (Priority Low)**: none in scope — modify is explicitly deferred to a future SPEC.

## Quality Gates (per Constitution)

- `uv run ruff check .` → 0 errors; `uv run ruff format --check .` clean.
- `uv run mypy --strict src` → 0 errors.
- `uv run pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% overall; the new
  `orders.py` aims for ~100% via mocked HTTP.
- TDD (RED → GREEN → REFACTOR). All HTTP mocked; zero live network. No secrets in repo/logs.
