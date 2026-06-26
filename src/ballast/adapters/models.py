"""Immutable adapter DTOs with Decimal money (REQ-ADAPTER-001-R1).

@CODE:SPEC-ADAPTER-001

These DTOs are the broker-agnostic shapes returned by the ports. Money/quantity
fields are :class:`~decimal.Decimal` (never ``float``); a bare ``float`` is
rejected at construction (FD3, AC-12) so precision never silently leaks onto the
money path. They are distinct from the CORE ``Order``/``Market`` types and are
mapped at the boundary (FD7).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict


def _reject_float(value: Any) -> Any:
    """Reject bare ``float`` money inputs before Decimal coercion.

    String (the OpenAPI ``format: decimal`` shape) and ``Decimal`` pass through;
    a bare ``float`` is rejected. ``bool`` is an ``int`` subclass in Python and is
    not affected.
    """
    if isinstance(value, float):
        raise ValueError("money/quantity must be a string or Decimal, not a float")
    return value


# A money/quantity value that must arrive as a string or Decimal, never a float.
Money = Annotated[Decimal, BeforeValidator(_reject_float)]

_Frozen = ConfigDict(frozen=True, extra="ignore")


class Currency(StrEnum):
    """Currency code (FD4). Source schema: ``Currency``."""

    KRW = "KRW"
    USD = "USD"


# Market country label (FD4). Kept as a Literal to tolerate the KR/US enum while
# still allowing unknown values to surface as plain strings at the call site.
MarketCountry = Literal["KR", "US"]


class Quote(BaseModel):
    """Current-price snapshot. Source schema: ``PriceResponse``."""

    model_config = _Frozen

    symbol: str
    last_price: Money
    currency: Currency
    timestamp: datetime | None = None


class Account(BaseModel):
    """A brokerage account. Source schema: ``Account``.

    ``account_seq`` is stringified from the integer ``accountSeq`` so it can be
    passed straight through to the ``X-Tossinvest-Account`` header (FD5).
    """

    model_config = _Frozen

    account_no: str
    account_seq: str
    account_type: str


class Holding(BaseModel):
    """A single position. Source schema: ``HoldingsItem``."""

    model_config = _Frozen

    symbol: str
    name: str
    market_country: str
    currency: Currency
    quantity: Money
    last_price: Money
    average_purchase_price: Money
    market_value: Money


class Holdings(BaseModel):
    """Holdings overview. Source schema: ``HoldingsOverview``."""

    model_config = _Frozen

    items: tuple[Holding, ...]


class Commission(BaseModel):
    """Per-market commission. Source schema: ``Commission``."""

    model_config = _Frozen

    market_country: str
    commission_rate: Money
    start_date: str | None = None
    end_date: str | None = None


class OrderExecution(BaseModel):
    """Execution block of a Toss order. Source schema: ``OrderExecution``."""

    model_config = _Frozen

    filled_quantity: Money
    average_filled_price: Money | None = None
    filled_amount: Money | None = None
    commission: Money | None = None
    tax: Money | None = None
    filled_at: datetime | None = None
    settlement_date: str | None = None


class OrderRecord(BaseModel):
    """A Toss order (adapter DTO, distinct from CORE ``Order``; FD7).

    Source schema: ``Order`` (+ ``OrderExecution``).
    """

    model_config = _Frozen

    order_id: str
    symbol: str
    side: str
    order_type: str
    time_in_force: str
    status: str
    price: Money | None
    quantity: Money
    order_amount: Money | None
    currency: Currency
    ordered_at: datetime
    canceled_at: datetime | None
    execution: OrderExecution


class OrdersPage(BaseModel):
    """One page of orders. Source schema: ``PaginatedOrderResponse``."""

    model_config = _Frozen

    orders: tuple[OrderRecord, ...]
    next_cursor: str | None
    has_next: bool


class MarketCalendar(BaseModel):
    """Market business-day calendar.

    Source schemas: ``UsMarketCalendarResponse`` / ``KrMarketCalendarResponse``.
    Only the business-day dates are surfaced here; per-session detail is left to
    a later SPEC.
    """

    model_config = _Frozen

    country: str
    today: str
    previous_business_day: str
    next_business_day: str
