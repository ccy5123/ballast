"""Tests for the Toss account adapter (REQ-ADAPTER-001-R5).

@TEST:SPEC-ADAPTER-001

Covers list_accounts, holdings (averagePurchasePrice/quantity), buying-power,
sellable-quantity, commissions, order pagination via nextCursor, order detail,
X-Tossinvest-Account header injection, and structural BrokerAccountPort
satisfaction.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from ballast.adapters.models import (
    Account,
    Commission,
    Currency,
    Holdings,
    OrderRecord,
    OrdersPage,
)
from ballast.adapters.ports import BrokerAccountPort
from ballast.adapters.toss.account import TossAccountAdapter
from ballast.adapters.toss.auth import TokenManager
from ballast.adapters.toss.client import TossClient


def _adapter(clock: object, handler: object) -> TossAccountAdapter:
    """Wire a TossAccountAdapter over a mocked transport."""

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
    return TossAccountAdapter(client)


def _order_payload(order_id: str, price: str | None = "70000") -> dict[str, object]:
    """A representative Toss Order schema instance."""
    return {
        "orderId": order_id,
        "symbol": "005930",
        "side": "BUY",
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "status": "FILLED",
        "price": price,
        "quantity": "10",
        "orderAmount": None,
        "currency": "KRW",
        "orderedAt": "2026-03-29T09:30:00+09:00",
        "canceledAt": None,
        "execution": {
            "filledQuantity": "10",
            "averageFilledPrice": "70000",
            "filledAmount": "700000",
            "commission": "1400",
            "tax": "0",
            "filledAt": "2026-03-28T09:31:15+09:00",
            "settlementDate": "2026-03-30",
        },
    }


# R5 — list_accounts maps accountSeq (int) to a string DTO field; no header.
def test_list_accounts(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "result": [
                    {"accountNo": "12345678901", "accountSeq": 1, "accountType": "BROKERAGE"}
                ]
            },
        )

    accounts = _adapter(clock, handler).list_accounts()
    assert len(accounts) == 1
    acc = accounts[0]
    assert isinstance(acc, Account)
    assert acc.account_no == "12345678901"
    assert acc.account_seq == "1"
    assert acc.account_type == "BROKERAGE"
    # AC-7 — list_accounts carries NO account header.
    assert "x-tossinvest-account" not in {k.lower() for k in seen[0].headers}


# AC-5 — get_holdings maps averagePurchasePrice / quantity per item.
def test_get_holdings_maps_items(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "totalPurchaseAmount": {"krw": "0", "usd": "240.00"},
                    "marketValue": {
                        "amount": {"krw": "0", "usd": "255.00"},
                        "amountAfterCost": {"krw": "0", "usd": "254.00"},
                    },
                    "profitLoss": {
                        "amount": {"krw": "0", "usd": "15.00"},
                        "amountAfterCost": {"krw": "0", "usd": "14.00"},
                        "rate": "0.0625",
                        "rateAfterCost": "0.0583",
                    },
                    "dailyProfitLoss": {"amount": {"krw": "0", "usd": "1.00"}, "rate": "0.004"},
                    "items": [
                        {
                            "symbol": "SOXL",
                            "name": "Direxion Daily Semiconductor Bull 3X",
                            "marketCountry": "US",
                            "currency": "USD",
                            "quantity": "10",
                            "lastPrice": "25.50",
                            "averagePurchasePrice": "24.00",
                            "marketValue": {
                                "purchaseAmount": "240.00",
                                "amount": "255.00",
                                "amountAfterCost": "254.00",
                            },
                            "profitLoss": {
                                "amount": "15.00",
                                "amountAfterCost": "14.00",
                                "rate": "0.0625",
                                "rateAfterCost": "0.0583",
                            },
                            "dailyProfitLoss": {"amount": "1.00", "rate": "0.004"},
                            "cost": {"commission": "0.50", "tax": "0.50"},
                        }
                    ],
                }
            },
        )

    holdings = _adapter(clock, handler).get_holdings("12345")
    assert isinstance(holdings, Holdings)
    item = holdings.items[0]
    assert item.symbol == "SOXL"
    assert item.quantity == Decimal("10")
    assert item.average_purchase_price == Decimal("24.00")
    assert item.last_price == Decimal("25.50")
    assert item.market_value == Decimal("255.00")
    assert item.currency is Currency.USD
    # AC-7 — header injected as the integer value.
    assert seen[0].headers["X-Tossinvest-Account"] == "12345"


def test_get_holdings_with_symbol_filter(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "totalPurchaseAmount": {"krw": "0"},
                    "marketValue": {
                        "amount": {"krw": "0"},
                        "amountAfterCost": {"krw": "0"},
                    },
                    "profitLoss": {
                        "amount": {"krw": "0"},
                        "amountAfterCost": {"krw": "0"},
                        "rate": "0",
                        "rateAfterCost": "0",
                    },
                    "dailyProfitLoss": {"amount": {"krw": "0"}, "rate": "0"},
                    "items": [],
                }
            },
        )

    holdings = _adapter(clock, handler).get_holdings("12345", symbol="SOXL")
    assert holdings.items == ()
    assert seen[0].url.params["symbol"] == "SOXL"


# AC-6 — get_buying_power returns cashBuyingPower Decimal.
def test_get_buying_power(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"result": {"currency": "USD", "cashBuyingPower": "1000.00"}}
        )

    power = _adapter(clock, handler).get_buying_power("12345", Currency.USD)
    assert power == Decimal("1000.00")
    assert isinstance(power, Decimal)
    assert seen[0].url.params["currency"] == "USD"
    assert seen[0].headers["X-Tossinvest-Account"] == "12345"


# R5 — get_sellable_quantity returns a Decimal.
def test_get_sellable_quantity(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": {"sellableQuantity": "100"}})

    qty = _adapter(clock, handler).get_sellable_quantity("12345", "005930")
    assert qty == Decimal("100")
    assert seen[0].url.params["symbol"] == "005930"
    assert seen[0].headers["X-Tossinvest-Account"] == "12345"


# R5 — get_commissions maps each commission row.
def test_get_commissions(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "marketCountry": "KR",
                        "commissionRate": "0.015",
                        "startDate": "2026-01-01",
                        "endDate": "2026-12-31",
                    },
                    {"marketCountry": "US", "commissionRate": "0.25"},
                ]
            },
        )

    commissions = _adapter(clock, handler).get_commissions("12345")
    assert len(commissions) == 2
    first = commissions[0]
    assert isinstance(first, Commission)
    assert first.market_country == "KR"
    assert first.commission_rate == Decimal("0.015")
    assert first.start_date == "2026-01-01"
    assert commissions[1].start_date is None
    assert seen[0].headers["X-Tossinvest-Account"] == "12345"


# AC-8 — list_orders follows nextCursor pagination.
def test_list_orders_single_page(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "orders": [_order_payload("ord-A")],
                    "nextCursor": "cur-2",
                    "hasNext": True,
                }
            },
        )

    page = _adapter(clock, handler).list_orders("12345", "CLOSED")
    assert isinstance(page, OrdersPage)
    assert page.next_cursor == "cur-2"
    assert page.has_next is True
    order = page.orders[0]
    assert isinstance(order, OrderRecord)
    assert order.order_id == "ord-A"
    assert order.price == Decimal("70000")
    assert order.quantity == Decimal("10")
    assert order.status == "FILLED"
    assert order.currency is Currency.KRW
    assert order.execution.filled_quantity == Decimal("10")
    assert seen[0].url.params["status"] == "CLOSED"
    assert seen[0].headers["X-Tossinvest-Account"] == "12345"


def test_list_orders_follows_cursor(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = request.url.params.get("cursor")
        if cursor is None:
            body = {
                "orders": [_order_payload("ord-A")],
                "nextCursor": "cur-2",
                "hasNext": True,
            }
        else:
            assert cursor == "cur-2"
            body = {
                "orders": [_order_payload("ord-B", price=None)],
                "nextCursor": None,
                "hasNext": False,
            }
        return httpx.Response(200, json={"result": body})

    adapter = _adapter(clock, handler)
    page1 = adapter.list_orders("12345", "CLOSED")
    assert page1.orders[0].order_id == "ord-A"
    assert page1.next_cursor == "cur-2"
    assert page1.has_next is True

    page2 = adapter.list_orders("12345", "CLOSED", cursor=page1.next_cursor)
    assert page2.orders[0].order_id == "ord-B"
    assert page2.orders[0].price is None  # MARKET order: null price -> None
    assert page2.next_cursor is None
    assert page2.has_next is False
    assert len(seen) == 2


# R5 — get_order maps a single Order into OrderRecord (with execution).
def test_get_order(clock: object) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": _order_payload("ord-X")})

    order = _adapter(clock, handler).get_order("12345", "ord-X")
    assert order.order_id == "ord-X"
    assert order.side == "BUY"
    assert order.order_type == "LIMIT"
    assert order.time_in_force == "DAY"
    assert order.execution.average_filled_price == Decimal("70000")
    assert order.execution.commission == Decimal("1400")
    assert seen[0].url.path == "/api/v1/orders/ord-X"
    assert seen[0].headers["X-Tossinvest-Account"] == "12345"


# R1 — the adapter structurally satisfies BrokerAccountPort.
def test_account_adapter_satisfies_port(clock: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not called
        return httpx.Response(200, json={"result": {}})

    adapter = _adapter(clock, handler)
    assert isinstance(adapter, BrokerAccountPort)
