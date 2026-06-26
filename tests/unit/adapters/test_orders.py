"""Tests for the Toss order-write adapter (REQ-ADAPTER-002 R1-R5).

@TEST:SPEC-ADAPTER-002

All HTTP is mocked (``httpx.MockTransport``); no live network is touched. Covers
the intent->create mapping (LIMIT+DAY / LIMIT+CLS / MARKET), ``clientOrderId``
passthrough, idempotent-replay -> ``DUPLICATE``, cancel success / unknown-key /
cancel errors, response/error -> ``FAILED`` mapping, the CLS-on-KR fail-fast
guard, Decimal-only serialization, and the secrets-never-logged invariant
(AC-1..AC-15), plus the structural ``BrokerOrderPort`` conformance.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import httpx
import pytest

from ballast.adapters.toss.auth import TokenManager
from ballast.adapters.toss.client import TossClient
from ballast.adapters.toss.orders import TossOrderAdapter
from ballast.core.models import Side
from ballast.orders.models import (
    OrderIntent,
    OrderKind,
    SubmissionResult,
    SubmissionStatus,
    Tif,
)
from ballast.orders.ports import BrokerOrderPort

_TOKEN = "jwt-abc"
_SECRET = "s_supersecret"


def _client(clock: object, handler: object, *, token: str = _TOKEN) -> TossClient:
    """Wire a TossClient over a mocked transport with zero real sleeps."""

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": token, "token_type": "Bearer", "expires_in": 3600},
        )

    token_http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(token_handler),
    )
    tm = TokenManager(
        http=token_http,
        client_id="c_id",
        client_secret=_SECRET,
        now=clock,  # type: ignore[arg-type]
    )
    http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return TossClient(http=http, token_manager=tm, backoff_base=0.0, sleep=lambda _s: None)


def _adapter(clock: object, handler: object) -> TossOrderAdapter:
    """Wire a TossOrderAdapter over a mocked transport."""
    return TossOrderAdapter(_client(clock, handler))


def _intent(
    *,
    client_order_id: str = "my-order-001",
    account_seq: str = "12345",
    side: Side = Side.BUY,
    ticker: str = "005930",
    qty: Decimal = Decimal("10"),
    kind: OrderKind = OrderKind.LIMIT,
    tif: Tif = Tif.DAY,
    limit_price: Decimal | None = Decimal("70000"),
) -> OrderIntent:
    """Build a representative OrderIntent (KR limit buy by default)."""
    return OrderIntent(
        client_order_id=client_order_id,
        account_seq=account_seq,
        side=side,
        ticker=ticker,
        qty=qty,
        kind=kind,
        tif=tif,
        limit_price=limit_price,
    )


def _body(request: httpx.Request) -> dict[str, object]:
    """Decode the JSON request body."""
    return json.loads(request.content)  # type: ignore[no-any-return]


# AC-1 — the adapter structurally satisfies the runtime-checkable BrokerOrderPort.
def test_adapter_satisfies_broker_order_port(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not called
        return httpx.Response(200, json={"result": {"orderId": "x"}})

    adapter = _adapter(clock, handler)
    assert isinstance(adapter, BrokerOrderPort)


# AC-2 — place_order maps LIMIT+DAY onto a Toss create request and headers.
def test_place_order_limit_day_mapping(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"result": {"orderId": "ord-abc", "clientOrderId": "my-order-001"}},
        )

    result = _adapter(clock, handler).place_order(_intent())

    request = seen[0]
    assert request.url.path == "/api/v1/orders"
    assert request.headers["X-Tossinvest-Account"] == "12345"
    assert request.headers["authorization"] == "Bearer jwt-abc"
    assert _body(request) == {
        "clientOrderId": "my-order-001",
        "symbol": "005930",
        "side": "BUY",
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "quantity": "10",
        "price": "70000",
    }
    assert result == SubmissionResult(
        client_order_id="my-order-001",
        status=SubmissionStatus.SUBMITTED,
        broker_order_id="ord-abc",
    )


# AC-3 — place_order maps LIMIT+CLS (LOC) for a US stock.
def test_place_order_limit_cls_us_mapping(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"orderId": "ord-loc"}})

    intent = _intent(
        client_order_id="loc-1",
        ticker="AAPL",
        tif=Tif.CLS,
        limit_price=Decimal("185.5"),
    )
    result = _adapter(clock, handler).place_order(intent)

    body = _body(seen[0])
    assert body["orderType"] == "LIMIT"
    assert body["timeInForce"] == "CLS"
    assert body["price"] == "185.5"
    assert body["symbol"] == "AAPL"
    assert result.status is SubmissionStatus.SUBMITTED
    assert result.broker_order_id == "ord-loc"


# AC-4 — place_order maps MARKET with no price field.
def test_place_order_market_omits_price(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"orderId": "ord-mkt"}})

    intent = _intent(
        client_order_id="mkt-1",
        side=Side.SELL,
        ticker="AAPL",
        qty=Decimal("3"),
        kind=OrderKind.MARKET,
        limit_price=None,
    )
    result = _adapter(clock, handler).place_order(intent)

    body = _body(seen[0])
    assert body["orderType"] == "MARKET"
    assert body["quantity"] == "3"
    assert "price" not in body
    assert result.status is SubmissionStatus.SUBMITTED
    assert result.broker_order_id == "ord-mkt"


# AC-5 — clientOrderId passthrough + idempotent replay maps to DUPLICATE.
def test_idempotent_replay_maps_to_duplicate(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"result": {"orderId": "ord-abc", "clientOrderId": "my-order-001"}},
        )

    adapter = _adapter(clock, handler)
    first = adapter.place_order(_intent())
    assert first.status is SubmissionStatus.SUBMITTED
    assert first.broker_order_id == "ord-abc"

    second = adapter.place_order(_intent())
    assert second.status is SubmissionStatus.DUPLICATE
    assert second.broker_order_id == "ord-abc"
    assert second.client_order_id == "my-order-001"


# AC-6 — idempotency-key-conflict maps to FAILED, not DUPLICATE.
def test_idempotency_key_conflict_maps_to_failed(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "requestId": "req-9",
                    "code": "idempotency-key-conflict",
                    "message": "동일한 clientOrderId 로 다른 내용의 주문을 요청할 수 없습니다.",
                }
            },
        )

    result = _adapter(clock, handler).place_order(_intent())
    assert result.status is SubmissionStatus.FAILED
    assert result.reason is not None
    assert "idempotency-key-conflict" in result.reason
    assert result.status is not SubmissionStatus.DUPLICATE


# AC-7 — cancel_order resolves client_order_id and succeeds.
def test_cancel_order_resolves_and_succeeds(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/orders":
            return httpx.Response(200, json={"result": {"orderId": "ord-abc"}})
        return httpx.Response(200, json={"result": {"orderId": "ord-cancel-new"}})

    adapter = _adapter(clock, handler)
    adapter.place_order(_intent())
    result = adapter.cancel_order("12345", "my-order-001")

    cancel_request = seen[-1]
    assert cancel_request.url.path == "/api/v1/orders/ord-abc/cancel"
    assert cancel_request.headers["X-Tossinvest-Account"] == "12345"
    assert result == SubmissionResult(
        client_order_id="my-order-001",
        status=SubmissionStatus.SUBMITTED,
        broker_order_id="ord-cancel-new",
    )


# AC-8 — cancel_order on an unknown client_order_id fails without an HTTP call.
def test_cancel_order_unknown_key_no_http(clock: object) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request)
        return httpx.Response(200, json={"result": {"orderId": "x"}})

    result = _adapter(clock, handler).cancel_order("12345", "never-seen")
    assert result.status is SubmissionStatus.FAILED
    assert result.reason is not None
    assert "unknown client_order_id" in result.reason
    assert calls == []


# AC-9 — cancel failures map to FAILED carrying the Toss code; not retried.
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (409, "already-filled"),
        (404, "order-not-found"),
        (422, "cancel-restricted"),
    ],
)
def test_cancel_failure_maps_to_failed(clock: object, status: int, code: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/orders":
            return httpx.Response(200, json={"result": {"orderId": "ord-x"}})
        return httpx.Response(
            status,
            json={"error": {"requestId": "req-2", "code": code, "message": "x"}},
        )

    adapter = _adapter(clock, handler)
    adapter.place_order(_intent(client_order_id="x"))
    result = adapter.cancel_order("12345", "x")

    assert result.status is SubmissionStatus.FAILED
    assert result.reason is not None
    assert code in result.reason
    # place (1) + exactly one cancel attempt (non-transient, not retried).
    cancel_calls = [r for r in seen if r.url.path.endswith("/cancel")]
    assert len(cancel_calls) == 1


# AC-10 — create business/validation errors map to FAILED carrying the code.
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (422, "insufficient-buying-power"),
        (400, "invalid-request"),
        (400, "confirm-high-value-required"),
        (422, "order-hours-closed"),
    ],
)
def test_create_business_errors_map_to_failed(clock: object, status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"requestId": "req-1", "code": code, "message": "거부됨"}},
        )

    result = _adapter(clock, handler).place_order(_intent())
    assert result.status is SubmissionStatus.FAILED
    assert result.reason is not None
    assert code in result.reason


# AC-11 — transient 500 retries (bounded) then FAILED; no silent infinite retry.
def test_transient_500_exhausts_to_failed(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            500,
            json={"error": {"requestId": "req-1", "code": "internal-error", "message": "오류"}},
        )

    result = _adapter(clock, handler).place_order(_intent())
    assert result.status is SubmissionStatus.FAILED
    assert result.reason is not None
    assert "internal-error" in result.reason
    # Bounded at the TossClient max of 3 attempts.
    assert len(seen) == 3


# AC-11 — 500 twice then 200 succeeds as SUBMITTED within 3 attempts.
def test_transient_500_then_success(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) < 3:
            return httpx.Response(
                500,
                json={"error": {"requestId": "r", "code": "internal-error", "message": "x"}},
            )
        return httpx.Response(200, json={"result": {"orderId": "ord-ok"}})

    result = _adapter(clock, handler).place_order(_intent())
    assert result.status is SubmissionStatus.SUBMITTED
    assert result.broker_order_id == "ord-ok"
    assert len(seen) == 3


# AC-11 — a 429 with Retry-After is honored before the next attempt.
def test_rate_limit_retry_after_honored(clock: object) -> None:
    seen: list[httpx.Request] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={"error": {"requestId": "r", "code": "rate-limit-exceeded", "message": "x"}},
        )

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": _TOKEN, "token_type": "Bearer", "expires_in": 3600}
        )

    token_http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(token_handler),
    )
    tm = TokenManager(http=token_http, client_id="c", client_secret=_SECRET, now=clock)  # type: ignore[arg-type]
    http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    client = TossClient(http=http, token_manager=tm, backoff_base=0.5, sleep=slept.append)

    result = TossOrderAdapter(client).place_order(_intent())
    assert result.status is SubmissionStatus.FAILED
    assert result.reason is not None
    assert "rate-limit-exceeded" in result.reason
    # Retry-After (2s) drove the bounded backoff, not the exponential default.
    assert slept and all(delay == 2.0 for delay in slept)


# AC-12 — CLS on a KR (all-digit) symbol fails fast without an HTTP call.
def test_cls_on_kr_symbol_fails_fast(clock: object) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request)
        return httpx.Response(200, json={"result": {"orderId": "x"}})

    intent = _intent(ticker="005930", tif=Tif.CLS, limit_price=Decimal("70000"))
    result = _adapter(clock, handler).place_order(intent)

    assert result.status is SubmissionStatus.FAILED
    assert result.reason is not None
    assert "US stocks only" in result.reason
    assert calls == []


# AC-12 — CLS on a US (alpha) symbol IS forwarded and returns SUBMITTED.
def test_cls_on_us_symbol_forwarded(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"orderId": "ord-loc"}})

    intent = _intent(ticker="AAPL", tif=Tif.CLS, limit_price=Decimal("185.5"))
    result = _adapter(clock, handler).place_order(intent)
    assert result.status is SubmissionStatus.SUBMITTED
    assert len(seen) == 1


# AC-13 — money is Decimal-only; serialized as exact decimal strings.
def test_money_serialized_as_exact_decimal_strings(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"orderId": "ord-f"}})

    intent = _intent(
        client_order_id="frac-1",
        side=Side.SELL,
        ticker="AAPL",
        qty=Decimal("0.5"),
        limit_price=Decimal("185.5"),
    )
    _adapter(clock, handler).place_order(intent)

    body = _body(seen[0])
    assert body["quantity"] == "0.5"
    assert body["price"] == "185.5"
    assert isinstance(body["quantity"], str)
    assert isinstance(body["price"], str)


# AC-13 — a bare float is rejected upstream at OrderIntent construction.
def test_float_rejected_at_intent_construction() -> None:
    with pytest.raises(ValueError, match="not a float"):
        _intent(qty=70.12)  # type: ignore[arg-type]


# AC-14 — secret/token never appears in logs, reason, or repr (success + failure).
def test_secret_never_leaks(clock: object, caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/orders":
            return httpx.Response(200, json={"result": {"orderId": "ord-abc"}})
        return httpx.Response(
            422,
            json={
                "error": {
                    "requestId": "req-7",
                    "code": "cancel-restricted",
                    "message": "취소할 수 없습니다.",
                }
            },
        )

    adapter = _adapter(clock, handler)
    with caplog.at_level(logging.DEBUG, logger="ballast.adapters"):
        ok = adapter.place_order(_intent())
        bad = adapter.cancel_order("12345", "my-order-001")

    rendered = repr(adapter) + repr(ok) + repr(bad) + str(ok.reason) + str(bad.reason)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert _TOKEN not in rendered
    assert _SECRET not in rendered
    assert _TOKEN not in blob
    assert _SECRET not in blob
    # requestId / Toss code ARE present for traceability.
    assert bad.reason is not None
    assert "cancel-restricted" in bad.reason
    assert "req-7" in blob


# AC-15 — no modify method, no amount-based order path on the public surface.
def test_no_modify_method() -> None:
    assert not hasattr(TossOrderAdapter, "modify")
    assert not hasattr(TossOrderAdapter, "modify_order")


def test_no_amount_based_order_field(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"orderId": "ord-mkt"}})

    intent = _intent(
        client_order_id="mkt-2",
        side=Side.SELL,
        ticker="AAPL",
        qty=Decimal("3"),
        kind=OrderKind.MARKET,
        limit_price=None,
    )
    _adapter(clock, handler).place_order(intent)
    assert "orderAmount" not in _body(seen[0])
