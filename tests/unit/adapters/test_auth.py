"""Tests for the OAuth2 client-credentials TokenManager (REQ-ADAPTER-001-R2).

@TEST:SPEC-ADAPTER-001

Covers token issuance/parse, refresh-before-expiry, OAuth2-error mapping to
``TossAuthError``, and secret/token redaction in logs and ``repr``.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

import httpx
import pytest

from ballast.adapters.errors import TossAuthError
from ballast.adapters.toss.auth import TokenManager


def _token_transport(
    captured: list[httpx.Request],
    *,
    body: dict[str, object] | None = None,
    status: int = 200,
) -> httpx.MockTransport:
    """Build a MockTransport that records the token request and returns ``body``."""

    payload = (
        body
        if body is not None
        else {
            "access_token": "jwt-abc",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


# AC-1 — Token issuance parses access_token / expires_in.
def test_token_issuance_parses_and_caches(clock: object) -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=_token_transport(captured),
    )
    mgr = TokenManager(
        http=client,
        client_id="c_id",
        client_secret="s_supersecret",
        now=clock,  # type: ignore[arg-type]
    )

    token = mgr.get_token()

    assert token == "jwt-abc"
    assert len(captured) == 1
    req = captured[0]
    # Form-urlencoded body with the client-credentials grant.
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    parsed = parse_qs(req.content.decode())
    assert parsed["grant_type"] == ["client_credentials"]
    assert parsed["client_id"] == ["c_id"]
    assert parsed["client_secret"] == ["s_supersecret"]
    # The token call carries NO Authorization header.
    assert "authorization" not in {k.lower() for k in req.headers}


def test_token_is_cached_and_not_refetched(clock: object) -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=_token_transport(captured),
    )
    mgr = TokenManager(
        http=client,
        client_id="c_id",
        client_secret="s_x",
        now=clock,  # type: ignore[arg-type]
    )

    assert mgr.get_token() == "jwt-abc"
    assert mgr.get_token() == "jwt-abc"
    # Cached: only one network round-trip.
    assert len(captured) == 1


# AC-2 — Expired/near-expiry token triggers refresh before a call.
def test_token_refreshes_before_expiry(clock: object) -> None:
    captured: list[httpx.Request] = []
    bodies = [
        {"access_token": "jwt-old", "token_type": "Bearer", "expires_in": 3600},
        {"access_token": "jwt-new", "token_type": "Bearer", "expires_in": 3600},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=bodies[len(captured) - 1])

    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    mgr = TokenManager(
        http=client,
        client_id="c_id",
        client_secret="s_x",
        now=clock,  # type: ignore[arg-type]
        safety_margin=60,
    )

    assert mgr.get_token() == "jwt-old"
    # Advance to within the safety margin of expiry (3600 - 60 = 3540s window).
    clock.advance(3550)  # type: ignore[attr-defined]
    assert mgr.get_token() == "jwt-new"
    assert len(captured) == 2


def test_force_refresh_always_refetches(clock: object) -> None:
    captured: list[httpx.Request] = []
    bodies = [
        {"access_token": "jwt-1", "token_type": "Bearer", "expires_in": 3600},
        {"access_token": "jwt-2", "token_type": "Bearer", "expires_in": 3600},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=bodies[len(captured) - 1])

    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    mgr = TokenManager(
        http=client,
        client_id="c",
        client_secret="s",
        now=clock,  # type: ignore[arg-type]
    )

    assert mgr.get_token() == "jwt-1"
    assert mgr.refresh() == "jwt-2"
    assert len(captured) == 2


# AC-9 (partial) — OAuth2 error from /oauth2/token raises TossAuthError.
@pytest.mark.parametrize("status", [400, 401])
def test_oauth2_error_raises_toss_auth_error(clock: object, status: int) -> None:
    captured: list[httpx.Request] = []
    body = {"error": "invalid_client", "error_description": "Client authentication failed."}
    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=_token_transport(captured, body=body, status=status),
    )
    mgr = TokenManager(
        http=client,
        client_id="c",
        client_secret="s",
        now=clock,  # type: ignore[arg-type]
    )

    with pytest.raises(TossAuthError) as exc:
        mgr.get_token()
    assert exc.value.code == "invalid_client"


# AC-10 — Secret/token never appears in repr.
def test_repr_redacts_secret_and_token(clock: object) -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=_token_transport(captured),
    )
    mgr = TokenManager(
        http=client,
        client_id="c_id",
        client_secret="s_supersecret",
        now=clock,  # type: ignore[arg-type]
    )
    mgr.get_token()

    text = repr(mgr)
    assert "s_supersecret" not in text
    assert "jwt-abc" not in text


# AC-10 — Secret/token never appears in logs.
def test_logs_do_not_leak_secret_or_token(clock: object, caplog: pytest.LogCaptureFixture) -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=_token_transport(captured),
    )
    mgr = TokenManager(
        http=client,
        client_id="c_id",
        client_secret="s_supersecret",
        now=clock,  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.DEBUG, logger="ballast.adapters"):
        mgr.get_token()

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "s_supersecret" not in blob
    assert "jwt-abc" not in blob


def test_default_clock_is_used_when_not_injected() -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=_token_transport(captured),
    )
    # No ``now`` injected: the real UTC clock default is exercised.
    mgr = TokenManager(http=client, client_id="c", client_secret="s")
    assert mgr.get_token() == "jwt-abc"
    # A cached, non-expired token is returned without a second round-trip.
    assert mgr.get_token() == "jwt-abc"
    assert len(captured) == 1
