"""Toss account / asset / order-history adapter (REQ-ADAPTER-001-R5).

@CODE:SPEC-ADAPTER-001

Implements :class:`~ballast.adapters.ports.BrokerAccountPort`. All methods inject
``X-Tossinvest-Account`` (FD5) except :meth:`list_accounts`, unwrap ``result``,
and return ``Decimal`` money. The Toss ``Order`` schema is mapped to the
``OrderRecord`` DTO, never the CORE ``Order`` (FD7).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from ballast.adapters.models import (
    Account,
    Commission,
    Currency,
    Holding,
    Holdings,
    OrderExecution,
    OrderRecord,
    OrdersPage,
)
from ballast.adapters.toss.client import TossClient, to_decimal


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp (``Z`` accepted); ``null`` -> ``None``."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _required_decimal(value: str | None) -> Decimal:
    """Parse a required ``format: decimal`` string field."""
    parsed = to_decimal(value)
    assert parsed is not None  # required by the schema
    return parsed


def _map_holding(item: dict[str, Any]) -> Holding:
    """Map a ``HoldingsItem`` to a :class:`Holding`."""
    return Holding(
        symbol=item["symbol"],
        name=item["name"],
        market_country=item["marketCountry"],
        currency=Currency(item["currency"]),
        quantity=_required_decimal(item["quantity"]),
        last_price=_required_decimal(item["lastPrice"]),
        average_purchase_price=_required_decimal(item["averagePurchasePrice"]),
        market_value=_required_decimal(item["marketValue"]["amount"]),
    )


def _map_execution(block: dict[str, Any]) -> OrderExecution:
    """Map an ``OrderExecution`` block to :class:`OrderExecution`."""
    return OrderExecution(
        filled_quantity=_required_decimal(block["filledQuantity"]),
        average_filled_price=to_decimal(block.get("averageFilledPrice")),
        filled_amount=to_decimal(block.get("filledAmount")),
        commission=to_decimal(block.get("commission")),
        tax=to_decimal(block.get("tax")),
        filled_at=_parse_dt(block.get("filledAt")),
        settlement_date=block.get("settlementDate"),
    )


def _map_order(order: dict[str, Any]) -> OrderRecord:
    """Map a Toss ``Order`` to an :class:`OrderRecord` (FD7)."""
    ordered_at = _parse_dt(order["orderedAt"])
    assert ordered_at is not None  # orderedAt is required by the schema
    return OrderRecord(
        order_id=order["orderId"],
        symbol=order["symbol"],
        side=order["side"],
        order_type=order["orderType"],
        time_in_force=order["timeInForce"],
        status=order["status"],
        price=to_decimal(order.get("price")),
        quantity=_required_decimal(order["quantity"]),
        order_amount=to_decimal(order.get("orderAmount")),
        currency=Currency(order["currency"]),
        ordered_at=ordered_at,
        canceled_at=_parse_dt(order.get("canceledAt")),
        execution=_map_execution(order["execution"]),
    )


class TossAccountAdapter:
    """Read-only account/asset/order methods over a :class:`TossClient`."""

    def __init__(self, client: TossClient) -> None:
        self._client = client

    def list_accounts(self) -> list[Account]:
        """``GET /api/v1/accounts`` -> ``Account[]`` (no account header)."""
        result: list[dict[str, Any]] = self._client.get("/api/v1/accounts")
        return [
            Account(
                account_no=acc["accountNo"],
                account_seq=str(acc["accountSeq"]),
                account_type=acc["accountType"],
            )
            for acc in result
        ]

    def get_holdings(self, account_seq: str, symbol: str | None = None) -> Holdings:
        """``GET /api/v1/holdings`` (header set; optional ``?symbol``)."""
        result: dict[str, Any] = self._client.get(
            "/api/v1/holdings",
            params={"symbol": symbol},
            account_seq=account_seq,
        )
        items = tuple(_map_holding(item) for item in result["items"])
        return Holdings(items=items)

    def get_buying_power(self, account_seq: str, currency: Currency) -> Decimal:
        """``GET /api/v1/buying-power?currency=`` -> ``cashBuyingPower``."""
        result: dict[str, Any] = self._client.get(
            "/api/v1/buying-power",
            params={"currency": currency.value},
            account_seq=account_seq,
        )
        return _required_decimal(result["cashBuyingPower"])

    def get_sellable_quantity(self, account_seq: str, symbol: str) -> Decimal:
        """``GET /api/v1/sellable-quantity?symbol=`` -> ``sellableQuantity``."""
        result: dict[str, Any] = self._client.get(
            "/api/v1/sellable-quantity",
            params={"symbol": symbol},
            account_seq=account_seq,
        )
        return _required_decimal(result["sellableQuantity"])

    def get_commissions(self, account_seq: str) -> list[Commission]:
        """``GET /api/v1/commissions`` -> ``Commission[]``."""
        result: list[dict[str, Any]] = self._client.get(
            "/api/v1/commissions", account_seq=account_seq
        )
        return [
            Commission(
                market_country=row["marketCountry"],
                commission_rate=_required_decimal(row["commissionRate"]),
                start_date=row.get("startDate"),
                end_date=row.get("endDate"),
            )
            for row in result
        ]

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
    ) -> OrdersPage:
        """``GET /api/v1/orders`` -> one ``PaginatedOrderResponse`` page.

        For ``status=CLOSED`` callers may follow ``next_cursor`` while ``has_next``
        is true; for ``status=OPEN`` the server ignores ``cursor``/``limit`` (FD6).
        """
        result: dict[str, Any] = self._client.get(
            "/api/v1/orders",
            params={
                "status": status,
                "symbol": symbol,
                "from": from_date,
                "to": to_date,
                "cursor": cursor,
                "limit": limit,
            },
            account_seq=account_seq,
        )
        return OrdersPage(
            orders=tuple(_map_order(order) for order in result["orders"]),
            next_cursor=result["nextCursor"],
            has_next=result["hasNext"],
        )

    def get_order(self, account_seq: str, order_id: str) -> OrderRecord:
        """``GET /api/v1/orders/{orderId}`` -> ``OrderRecord``."""
        result: dict[str, Any] = self._client.get(
            f"/api/v1/orders/{order_id}", account_seq=account_seq
        )
        return _map_order(result)
