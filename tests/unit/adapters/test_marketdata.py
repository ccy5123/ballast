"""Tests for the Toss market-data adapter (REQ-ADAPTER-001-R4).

@TEST:SPEC-ADAPTER-001

Covers price string->Decimal + currency enum, exchange rate, market calendar,
and structural satisfaction of MarketDataPort.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from ballast.adapters.errors import TossApiError
from ballast.adapters.models import Currency, MarketCalendar, Quote
from ballast.adapters.ports import MarketDataPort
from ballast.adapters.toss.auth import TokenManager
from ballast.adapters.toss.client import TossClient
from ballast.adapters.toss.marketdata import TossMarketDataAdapter


def _adapter(clock: object, handler: object) -> TossMarketDataAdapter:
    """Wire a TossMarketDataAdapter over a mocked transport."""

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "jwt-abc", "token_type": "Bearer", "expires_in": 3600},
        )

    token_http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(token_handler),
    )
    tm = TokenManager(
        http=token_http,
        client_id="c",
        client_secret="s",
        now=clock,  # type: ignore[arg-type]
    )
    http = httpx.Client(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    client = TossClient(http=http, token_manager=tm, backoff_base=0.0, sleep=lambda _s: None)
    return TossMarketDataAdapter(client)


# AC-4 — get_price parses lastPrice string->Decimal and currency enum.
def test_get_price_maps_quote(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "TQQQ",
                        "lastPrice": "70.12",
                        "currency": "USD",
                        "timestamp": "2026-06-26T13:30:00Z",
                    }
                ]
            },
        )

    quote = _adapter(clock, handler).get_price("TQQQ")

    assert isinstance(quote, Quote)
    assert quote.symbol == "TQQQ"
    assert quote.last_price == Decimal("70.12")
    assert isinstance(quote.last_price, Decimal)
    assert quote.currency is Currency.USD
    assert quote.timestamp is not None
    # symbols query param is forwarded.
    assert seen[0].url.params["symbols"] == "TQQQ"


def test_get_price_handles_null_timestamp(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": [
                    {"symbol": "005930", "lastPrice": "72000", "currency": "KRW", "timestamp": None}
                ]
            },
        )

    quote = _adapter(clock, handler).get_price("005930")
    assert quote.last_price == Decimal("72000")
    assert quote.currency is Currency.KRW
    assert quote.timestamp is None


def test_get_price_unknown_symbol_raises(clock: object) -> None:
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

    with pytest.raises(TossApiError) as exc:
        _adapter(clock, handler).get_price("BADSYM")
    assert exc.value.code == "stock-not-found"
    assert exc.value.request_id == "req-1"


# R4 — get_exchange_rate returns the rate as Decimal.
def test_get_exchange_rate_returns_decimal(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "baseCurrency": "USD",
                    "quoteCurrency": "KRW",
                    "rate": "1380.5",
                    "midRate": "1375",
                    "basisPoint": "40",
                    "rateChangeType": "UP",
                    "validFrom": "2026-03-25T09:30:00+09:00",
                    "validUntil": "2026-03-25T09:31:00+09:00",
                }
            },
        )

    rate = _adapter(clock, handler).get_exchange_rate(Currency.USD, Currency.KRW)
    assert rate == Decimal("1380.5")
    assert isinstance(rate, Decimal)
    assert seen[0].url.params["baseCurrency"] == "USD"
    assert seen[0].url.params["quoteCurrency"] == "KRW"


# R4 — get_market_calendar maps the today/prev/next business days.
@pytest.mark.parametrize("country", ["US", "KR"])
def test_get_market_calendar(clock: object, country: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "today": {"date": "2026-06-26"},
                    "previousBusinessDay": {"date": "2026-06-25"},
                    "nextBusinessDay": {"date": "2026-06-29"},
                }
            },
        )

    cal = _adapter(clock, handler).get_market_calendar(country)
    assert isinstance(cal, MarketCalendar)
    assert cal.country == country
    assert cal.today == "2026-06-26"
    assert cal.previous_business_day == "2026-06-25"
    assert cal.next_business_day == "2026-06-29"


def test_get_market_calendar_rejects_unknown_country(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
        return httpx.Response(200, json={"result": {}})

    with pytest.raises(ValueError, match="country"):
        _adapter(clock, handler).get_market_calendar("JP")


# R1 — the adapter structurally satisfies MarketDataPort.
def test_market_data_adapter_satisfies_port(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not called
        return httpx.Response(200, json={"result": {}})

    adapter = _adapter(clock, handler)
    assert isinstance(adapter, MarketDataPort)
