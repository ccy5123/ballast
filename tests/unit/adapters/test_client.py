"""Tests for the httpx transport TossClient (REQ-ADAPTER-001-R3).

@TEST:SPEC-ADAPTER-001

Covers envelope unwrap, string->Decimal parsing, typed error mapping, exactly
one 401 re-auth+retry, bounded retry/backoff on transient failures, account
header injection, and secret/token redaction.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest

from ballast.adapters.errors import TossApiError, TossAuthError, TossRateLimitError
from ballast.adapters.toss.auth import TokenManager
from ballast.adapters.toss.client import TossClient


def _make_token_manager(clock: object, token: str = "jwt-abc") -> TokenManager:
    """Build a TokenManager whose token endpoint always returns ``token``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": token, "token_type": "Bearer", "expires_in": 3600},
        )

    http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    return TokenManager(
        http=http,
        client_id="c_id",
        client_secret="s_supersecret",
        now=clock,  # type: ignore[arg-type]
    )


def _build_client(
    handler: object,
    *,
    token_manager: TokenManager,
    backoff_base: float = 0.0,
) -> TossClient:
    """Wire a TossClient over a MockTransport ``handler`` with zero real sleeps."""
    http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return TossClient(
        http=http,
        token_manager=token_manager,
        backoff_base=backoff_base,
        sleep=lambda _seconds: None,
    )


# Envelope unwrap + Bearer injection.
def test_get_unwraps_result_and_injects_bearer(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"value": "42"}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    result = client.get("/api/v1/anything")

    assert result == {"value": "42"}
    assert seen[0].headers["authorization"] == "Bearer jwt-abc"


# AC-7 — account-scoped calls inject X-Tossinvest-Account; others do not.
def test_account_header_injected_only_when_requested(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    client.get("/api/v1/holdings", account_seq="12345")
    client.get("/api/v1/accounts")

    assert seen[0].headers["X-Tossinvest-Account"] == "12345"
    assert "x-tossinvest-account" not in {k.lower() for k in seen[1].headers}


# AC-3 — a 401 triggers exactly one re-auth then retry.
def test_single_401_triggers_one_reauth_then_succeeds(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Only authenticated (non-token) calls get the 401-then-200 treatment.
        if len(seen) == 1:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "requestId": "req-1",
                        "code": "expired-token",
                        "message": "토큰이 만료되었습니다.",
                    }
                },
            )
        return httpx.Response(200, json={"result": {"ok": True}})

    tm = _make_token_manager(clock)
    client = _build_client(handler, token_manager=tm)

    result = client.get("/api/v1/prices")
    assert result == {"ok": True}
    # First attempt (401) + retried attempt (200).
    assert len(seen) == 2


def test_second_consecutive_401_raises_auth_error(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            401,
            json={
                "error": {
                    "requestId": "req-1",
                    "code": "invalid-token",
                    "message": "유효하지 않은 토큰입니다.",
                }
            },
        )

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    with pytest.raises(TossAuthError) as exc:
        client.get("/api/v1/prices")
    assert exc.value.code == "invalid-token"
    assert exc.value.request_id == "req-1"
    # No infinite loop: original attempt + exactly one retry.
    assert len(seen) == 2


# AC-9 — envelope error maps to a typed exception.
def test_domain_error_maps_to_api_error(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "requestId": "req-1",
                    "code": "stock-not-found",
                    "message": "종목을 찾을 수 없습니다.",
                }
            },
        )

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    with pytest.raises(TossApiError) as exc:
        client.get("/api/v1/prices")
    assert exc.value.code == "stock-not-found"
    assert exc.value.request_id == "req-1"
    assert exc.value.status_code == 404
    assert "종목을 찾을 수 없습니다." in exc.value.message


# AC-9 — 429 maps to TossRateLimitError surfacing Retry-After and X-RateLimit-*.
def test_rate_limit_maps_to_rate_limit_error(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "Retry-After": "1",
                "X-RateLimit-Limit": "10",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1",
            },
            json={
                "error": {
                    "requestId": "req-9",
                    "code": "rate-limit-exceeded",
                    "message": "요청 한도를 초과했습니다.",
                }
            },
        )

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    with pytest.raises(TossRateLimitError) as exc:
        client.get("/api/v1/prices")
    assert exc.value.retry_after == 1
    assert exc.value.limit == 10
    assert exc.value.remaining == 0
    assert exc.value.reset == 1


# AC-11 — transient failures retry with bounded backoff and then succeed.
def test_transient_500_retries_then_succeeds(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) < 3:
            return httpx.Response(
                500,
                json={
                    "error": {
                        "requestId": "req-1",
                        "code": "internal-error",
                        "message": "처리 중 문제가 생겼어요.",
                    }
                },
            )
        return httpx.Response(200, json={"result": {"ok": True}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    result = client.get("/api/v1/prices")
    assert result == {"ok": True}
    assert len(seen) == 3


def test_transient_failure_exhausts_and_raises(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            503,
            json={
                "error": {
                    "requestId": "req-1",
                    "code": "maintenance",
                    "message": "점검 중입니다.",
                }
            },
        )

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    with pytest.raises(TossApiError) as exc:
        client.get("/api/v1/prices")
    assert exc.value.code == "maintenance"
    # Bounded at max 3 attempts.
    assert len(seen) == 3


def test_timeout_is_retried_then_raises(clock: object) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        raise httpx.ReadTimeout("slow", request=request)

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    with pytest.raises(TossApiError) as exc:
        client.get("/api/v1/prices")
    assert exc.value.code == "timeout"
    assert len(seen) == 3


# AC-11 — non-transient client errors are NOT retried.
@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "invalid-request"), (403, "forbidden"), (404, "account-not-found")],
)
def test_non_transient_errors_not_retried(clock: object, status: int, code: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status,
            json={"error": {"requestId": "req-1", "code": code, "message": "x"}},
        )

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    with pytest.raises(TossApiError) as exc:
        client.get("/api/v1/orders", account_seq="1")
    assert exc.value.code == code
    # Exactly one attempt, no retry.
    assert len(seen) == 1


# AC-10 — secret/token never appears in logs or exception text.
def test_error_text_and_logs_do_not_leak_token(
    clock: object, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"requestId": "req-1", "code": "stock-not-found", "message": "x"}},
        )

    client = _build_client(handler, token_manager=_make_token_manager(clock))

    with (
        caplog.at_level(logging.DEBUG, logger="ballast.adapters"),
        pytest.raises(TossApiError) as exc,
    ):
        client.get("/api/v1/prices")

    assert "jwt-abc" not in str(exc.value)
    assert "s_supersecret" not in str(exc.value)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "jwt-abc" not in blob
    assert "s_supersecret" not in blob
    # requestId IS present for traceability.
    assert "req-1" in blob


def test_to_decimal_helper_parses_strings_and_null() -> None:
    from ballast.adapters.toss.client import to_decimal

    assert to_decimal("70.12") == Decimal("70.12")
    assert isinstance(to_decimal("70.12"), Decimal)
    assert to_decimal(None) is None


def test_to_decimal_passes_through_decimal() -> None:
    from ballast.adapters.toss.client import to_decimal

    value = Decimal("1.23")
    assert to_decimal(value) is value


# AC-12 — float money is rejected at the boundary.
@pytest.mark.parametrize("bad", [70.12, True])
def test_to_decimal_rejects_float_and_bool(bad: object) -> None:
    from ballast.adapters.toss.client import to_decimal

    with pytest.raises((TypeError, ValueError)):
        to_decimal(bad)  # type: ignore[arg-type]


# AC-10 — TossClient repr does not leak secrets/token.
def test_client_repr_is_redacted(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not called
        return httpx.Response(200, json={"result": {}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    text = repr(client)
    assert "jwt-abc" not in text
    assert "s_supersecret" not in text


# An error envelope without an ``error.code`` falls back to ``http-{status}``.
def test_error_without_code_uses_http_status_fallback(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"requestId": "req-1", "message": "x"}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    with pytest.raises(TossApiError) as exc:
        client.get("/api/v1/prices")
    assert exc.value.code == "http-400"


# A malformed (non-mapping) ``error`` field is handled defensively.
def test_malformed_error_field_falls_back(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "oops-not-an-object"})

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    with pytest.raises(TossApiError) as exc:
        client.get("/api/v1/prices")
    assert exc.value.code == "http-400"


# ADAPTER-002 R1 — post unwraps the result, injects Bearer + the account header,
# and sends the JSON body verbatim (mirrors get's transport guarantees).
def test_post_unwraps_result_injects_headers_and_body(clock: object) -> None:
    import json

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"orderId": "ord-1"}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    result = client.post("/api/v1/orders", json={"symbol": "005930"}, account_seq="12345")

    assert result == {"orderId": "ord-1"}
    assert seen[0].method == "POST"
    assert seen[0].headers["authorization"] == "Bearer jwt-abc"
    assert seen[0].headers["X-Tossinvest-Account"] == "12345"
    assert json.loads(seen[0].content) == {"symbol": "005930"}


# ADAPTER-002 R1 — post with no body / no account header is still authenticated.
def test_post_without_body_or_account(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"orderId": "ord-2"}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    result = client.post("/api/v1/orders/ord-1/cancel")

    assert result == {"orderId": "ord-2"}
    assert seen[0].headers["authorization"] == "Bearer jwt-abc"
    assert "x-tossinvest-account" not in {k.lower() for k in seen[0].headers}


# ADAPTER-002 R4 — post reuses the single-401-reauth guard.
def test_post_single_401_reauth_then_succeeds(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                401,
                json={"error": {"requestId": "r", "code": "expired-token", "message": "x"}},
            )
        return httpx.Response(200, json={"result": {"orderId": "ord-3"}})

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    result = client.post("/api/v1/orders", json={}, account_seq="1")
    assert result == {"orderId": "ord-3"}
    assert len(seen) == 2


# ADAPTER-002 R4 — post reuses the bounded transient retry and maps timeouts.
def test_post_transient_retry_and_timeout(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    client = _build_client(handler, token_manager=_make_token_manager(clock))
    with pytest.raises(TossApiError) as exc:
        client.post("/api/v1/orders", json={})
    assert exc.value.code == "timeout"
    assert len(seen) == 3
