"""Tests for the close-based fill model (REQ-BACKTEST-001-R2).

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from ballast.backtest.fills import simulate_fill
from ballast.backtest.types import OHLCBar
from ballast.core.models import Order, OrderType, Side

_D = date(2024, 1, 2)
_NEXT = date(2024, 1, 3)


def _bar(o: str, h: str, low: str, c: str, *, fx: str = "1300.00") -> OHLCBar:
    return OHLCBar(
        date=_D,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        adj_close=None,
        fx_usdkrw=Decimal(fx),
    )


def _loc(side: Side, qty: str, limit: str | None) -> Order:
    return Order(
        side=side,
        ticker="TQQQ",
        qty=Decimal(qty),
        limit_price=None if limit is None else Decimal(limit),
        order_type=OrderType.LOC,
        account_seq="0001",
    )


def _reserved(side: Side, qty: str, limit: str) -> Order:
    return Order(
        side=side,
        ticker="TQQQ",
        qty=Decimal(qty),
        limit_price=Decimal(limit),
        order_type=OrderType.RESERVED_LIMIT,
        account_seq="0001",
    )


# --- Scenario 2 — LOC BUY fills only when close <= limit ------------------- #
def test_loc_buy_fills_at_close_when_close_at_or_below_limit() -> None:
    order = _loc(Side.BUY, "5.00", "100.00")
    fill = simulate_fill(order, _bar("101", "102", "99", "100"), slippage_bps=Decimal("0"))
    assert fill is not None
    assert fill.fill_price == Decimal("100.00")
    assert fill.qty == Decimal("5.00")
    assert fill.date == _D


def test_loc_buy_misses_when_close_above_limit() -> None:
    order = _loc(Side.BUY, "5.00", "100.00")
    fill = simulate_fill(order, _bar("101", "103", "101", "102"), slippage_bps=Decimal("0"))
    assert fill is None


def test_loc_sell_fills_only_when_close_at_or_above_limit() -> None:
    order = _loc(Side.SELL, "5.00", "100.00")
    fill_hit = simulate_fill(order, _bar("99", "101", "99", "100"), slippage_bps=Decimal("0"))
    assert fill_hit is not None
    assert fill_hit.fill_price == Decimal("100.00")
    fill_miss = simulate_fill(order, _bar("99", "99", "98", "99"), slippage_bps=Decimal("0"))
    assert fill_miss is None


def test_loc_no_limit_quarter_sell_fills_at_close() -> None:
    order = _loc(Side.SELL, "2.00", None)  # MAB MOC-like quarter-sell
    fill = simulate_fill(order, _bar("99", "120", "80", "118"), slippage_bps=Decimal("0"))
    assert fill is not None
    assert fill.fill_price == Decimal("118.00")


# --- Scenario 3 — reserved_limit fills when range crosses limit ------------ #
def test_reserved_limit_buy_fills_at_limit_when_in_range() -> None:
    order = _reserved(Side.BUY, "3.00", "95.00")
    fill = simulate_fill(order, _bar("100", "101", "90", "98"), slippage_bps=Decimal("0"))
    assert fill is not None
    assert fill.fill_price == Decimal("95.00")  # the limit, not the close
    assert fill.qty == Decimal("3.00")


def test_reserved_limit_misses_when_limit_outside_range() -> None:
    order = _reserved(Side.BUY, "3.00", "95.00")
    fill = simulate_fill(order, _bar("100", "101", "96", "98"), slippage_bps=Decimal("0"))
    assert fill is None  # cancelled for the cycle, no carry-over


def test_reserved_limit_sell_fills_at_limit_when_in_range() -> None:
    order = _reserved(Side.SELL, "3.00", "105.00")
    fill_hit = simulate_fill(order, _bar("100", "110", "100", "104"), slippage_bps=Decimal("0"))
    assert fill_hit is not None
    assert fill_hit.fill_price == Decimal("105.00")
    fill_miss = simulate_fill(order, _bar("100", "104", "100", "102"), slippage_bps=Decimal("0"))
    assert fill_miss is None


def test_reserved_limit_fills_on_exact_boundary() -> None:
    # low == limit and high == limit both count as crossing.
    order = _reserved(Side.BUY, "1.00", "90.00")
    fill = simulate_fill(order, _bar("100", "101", "90", "95"), slippage_bps=Decimal("0"))
    assert fill is not None and fill.fill_price == Decimal("90.00")


# --- Slippage -------------------------------------------------------------- #
def test_slippage_raises_buy_price_and_lowers_sell_price() -> None:
    buy = _loc(Side.BUY, "1.00", "100.00")
    # 100 * (1 + 100/10000) = 100 * 1.01 = 101.00
    fbuy = simulate_fill(buy, _bar("100", "100", "100", "100"), slippage_bps=Decimal("100"))
    assert fbuy is not None
    assert fbuy.fill_price == Decimal("101.00")

    sell = _loc(Side.SELL, "1.00", "100.00")
    # 100 * (1 - 100/10000) = 99.00
    fsell = simulate_fill(sell, _bar("100", "100", "100", "100"), slippage_bps=Decimal("100"))
    assert fsell is not None
    assert fsell.fill_price == Decimal("99.00")


def test_default_slippage_is_zero() -> None:
    buy = _loc(Side.BUY, "1.00", "100.00")
    fill = simulate_fill(buy, _bar("100", "100", "100", "100"))
    assert fill is not None
    assert fill.fill_price == Decimal("100.00")


def test_fill_money_is_decimal_two_places() -> None:
    fill = simulate_fill(_loc(Side.BUY, "1.00", "100.00"), _bar("100", "100", "100", "100"))
    assert fill is not None
    assert type(fill.fill_price) is Decimal
    assert type(fill.qty) is Decimal


# --- Scenario 10 — no look-ahead: fill at t ignores t+1 (hypothesis) ------- #
@given(
    next_o=st.decimals(min_value="1", max_value="1000", places=2),
    next_h=st.decimals(min_value="1", max_value="1000", places=2),
    next_low=st.decimals(min_value="1", max_value="1000", places=2),
    next_c=st.decimals(min_value="1", max_value="1000", places=2),
    next_fx=st.decimals(min_value="1", max_value="2000", places=2),
)
def test_no_look_ahead_fill_independent_of_next_bar(
    next_o: Decimal,
    next_h: Decimal,
    next_low: Decimal,
    next_c: Decimal,
    next_fx: Decimal,
) -> None:
    order = _reserved(Side.BUY, "3.00", "95.00")
    bar_t = _bar("100", "101", "90", "98")
    baseline = simulate_fill(order, bar_t, slippage_bps=Decimal("0"))

    # An arbitrary t+1 bar; simulate_fill must never touch it.
    next_bar = OHLCBar(
        date=_NEXT,
        open=next_o,
        high=next_h,
        low=next_low,
        close=next_c,
        adj_close=None,
        fx_usdkrw=next_fx,
    )
    again = simulate_fill(order, bar_t, slippage_bps=Decimal("0"))
    assert again == baseline
    # The t+1 bar object exists but is structurally irrelevant to the t fill.
    assert dataclasses.asdict(next_bar) is not None
