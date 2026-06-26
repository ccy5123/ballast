"""Toss market-data adapter (REQ-ADAPTER-001-R4).

@CODE:SPEC-ADAPTER-001

Implements :class:`~ballast.adapters.ports.MarketDataPort` against the grounded
endpoints. Every method unwraps ``result`` and returns adapter DTOs with
``Decimal`` money.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ballast.adapters.models import Currency, MarketCalendar, Quote
from ballast.adapters.toss.client import TossClient, to_decimal

_VALID_COUNTRIES = ("US", "KR")


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp (``Z`` accepted); ``null`` -> ``None``."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TossMarketDataAdapter:
    """Read-only market-data methods over a :class:`TossClient`."""

    def __init__(self, client: TossClient) -> None:
        self._client = client

    def get_price(self, symbol: str) -> Quote:
        """``GET /api/v1/prices?symbols={symbol}`` -> first ``PriceResponse``."""
        result: list[dict[str, Any]] = self._client.get(
            "/api/v1/prices", params={"symbols": symbol}
        )
        item = result[0]
        last_price = to_decimal(item["lastPrice"])
        assert last_price is not None  # lastPrice is required by the schema
        return Quote(
            symbol=item["symbol"],
            last_price=last_price,
            currency=Currency(item["currency"]),
            timestamp=_parse_dt(item.get("timestamp")),
        )

    def get_exchange_rate(self, base: Currency, quote: Currency) -> Decimal:
        """``GET /api/v1/exchange-rate`` -> ``rate`` as ``Decimal``."""
        result: dict[str, Any] = self._client.get(
            "/api/v1/exchange-rate",
            params={"baseCurrency": base.value, "quoteCurrency": quote.value},
        )
        rate = to_decimal(result["rate"])
        assert rate is not None  # rate is required by the schema
        return rate

    def get_market_calendar(self, country: str) -> MarketCalendar:
        """``GET /api/v1/market-calendar/{US,KR}`` -> ``MarketCalendar``."""
        if country not in _VALID_COUNTRIES:
            raise ValueError(f"country must be one of {_VALID_COUNTRIES}, got {country!r}")
        result: dict[str, Any] = self._client.get(f"/api/v1/market-calendar/{country}")
        return MarketCalendar(
            country=country,
            today=result["today"]["date"],
            previous_business_day=result["previousBusinessDay"]["date"],
            next_business_day=result["nextBusinessDay"]["date"],
        )
