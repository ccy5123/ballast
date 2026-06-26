"""Broker-agnostic port Protocols (REQ-ADAPTER-001-R1).

@CODE:SPEC-ADAPTER-001

These ``typing.Protocol`` ports prescribe no concrete transport, only the
broker-agnostic shape a Toss or future KIS adapter satisfies structurally. They
are ``runtime_checkable`` so structural conformance can be asserted in tests.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from ballast.adapters.models import (
    Account,
    Commission,
    Currency,
    Holdings,
    MarketCalendar,
    OrderRecord,
    OrdersPage,
    Quote,
)


@runtime_checkable
class MarketDataPort(Protocol):
    """Market-data reads (no account header)."""

    def get_price(self, symbol: str) -> Quote: ...

    def get_exchange_rate(self, base: Currency, quote: Currency) -> Decimal: ...

    def get_market_calendar(self, country: str) -> MarketCalendar: ...


@runtime_checkable
class BrokerAccountPort(Protocol):
    """Account-scoped reads (all but ``list_accounts`` carry the account header)."""

    def list_accounts(self) -> list[Account]: ...

    def get_holdings(self, account_seq: str, symbol: str | None = None) -> Holdings: ...

    def get_buying_power(self, account_seq: str, currency: Currency) -> Decimal: ...

    def get_sellable_quantity(self, account_seq: str, symbol: str) -> Decimal: ...

    def get_commissions(self, account_seq: str) -> list[Commission]: ...

    def list_orders(
        self,
        account_seq: str,
        status: Literal["OPEN", "CLOSED"],
        *,
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> OrdersPage: ...

    def get_order(self, account_seq: str, order_id: str) -> OrderRecord: ...
