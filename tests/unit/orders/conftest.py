"""Shared fixtures for the Order Manager test suite.

@TEST:SPEC-ORDER-001

Everything is in-memory: no network, no credentials, no wall-clock reads. The
clock (``cycle_key``) and any prices/positions/buying-power are injected by the
tests, mirroring the manager's IO-free contract.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ballast.core.models import Order, OrderType, Side


@pytest.fixture
def buy_reserved_limit() -> Order:
    """A reserved-limit BUY (maps to LIMIT + DAY, carries the limit_price)."""
    return Order(
        side=Side.BUY,
        ticker="QLD",
        qty=Decimal("3"),
        limit_price=Decimal("80.00"),
        order_type=OrderType.RESERVED_LIMIT,
        account_seq="acc-1",
    )


@pytest.fixture
def sell_loc() -> Order:
    """A LOC SELL with no price (maps to LIMIT + CLS, MOC-style close)."""
    return Order(
        side=Side.SELL,
        ticker="SOXL",
        qty=Decimal("10"),
        limit_price=None,
        order_type=OrderType.LOC,
        account_seq="acc-2",
    )


@pytest.fixture
def buy_market() -> Order:
    """A market BUY whose incoming price is dropped (maps to MARKET + DAY)."""
    return Order(
        side=Side.BUY,
        ticker="SPY",
        qty=Decimal("1"),
        limit_price=Decimal("500.00"),
        order_type=OrderType.MARKET,
        account_seq="acc-3",
    )
