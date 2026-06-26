"""Tests for the immutable backtest-only types (REQ-BACKTEST-001 — types).

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

import pytest

from ballast.backtest.types import (
    BacktestResult,
    EquityPoint,
    Fill,
    OHLCBar,
    Trade,
)
from ballast.core.models import Order, OrderType, Side


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return -exponent


def _sample_order() -> Order:
    return Order(
        side=Side.BUY,
        ticker="TQQQ",
        qty=Decimal("5"),
        limit_price=Decimal("100.00"),
        order_type=OrderType.LOC,
        account_seq="0001",
    )


def test_ohlcbar_quantizes_money_fields_to_two_places() -> None:
    b = OHLCBar(
        date=date(2024, 1, 2),
        open=Decimal("100.005"),
        high=Decimal("110.119"),
        low=Decimal("90.001"),
        close=Decimal("105.555"),
        adj_close=Decimal("105.554"),
        fx_usdkrw=Decimal("1300.009"),
    )
    assert b.open == Decimal("100.01")
    assert b.high == Decimal("110.12")
    assert b.close == Decimal("105.56")
    assert b.adj_close == Decimal("105.55")
    assert b.fx_usdkrw == Decimal("1300.01")
    for v in (b.open, b.high, b.low, b.close, b.fx_usdkrw):
        assert type(v) is Decimal
        assert _decimal_places(v) == 2


def test_ohlcbar_adj_close_may_be_none() -> None:
    b = OHLCBar(
        date=date(2024, 1, 2),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        adj_close=None,
        fx_usdkrw=Decimal("1300"),
    )
    assert b.adj_close is None


def test_ohlcbar_is_frozen() -> None:
    b = OHLCBar(
        date=date(2024, 1, 2),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        adj_close=None,
        fx_usdkrw=Decimal("1300"),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.close = Decimal("101")  # type: ignore[misc]


def test_fill_money_fields_are_decimal_two_places() -> None:
    fill = Fill(
        order=_sample_order(),
        fill_price=Decimal("100.001"),
        qty=Decimal("5.004"),
        date=date(2024, 1, 2),
        commission=Decimal("1.005"),
    )
    assert fill.fill_price == Decimal("100.00")
    assert fill.qty == Decimal("5.00")
    assert fill.commission == Decimal("1.01")
    for v in (fill.fill_price, fill.qty, fill.commission):
        assert type(v) is Decimal
        assert _decimal_places(v) == 2


def test_trade_money_fields_are_decimal() -> None:
    trade = Trade(
        side=Side.SELL,
        qty=Decimal("5.00"),
        realized_gain_usd=Decimal("12.345"),
        tax_year=2024,
    )
    assert trade.realized_gain_usd == Decimal("12.35")
    assert type(trade.realized_gain_usd) is Decimal
    assert trade.tax_year == 2024


def test_equity_point_money_fields_are_decimal_two_places() -> None:
    pt = EquityPoint(
        date=date(2024, 1, 2),
        equity_usd=Decimal("1000.001"),
        equity_krw=Decimal("1300000.009"),
        holdings=Decimal("10.001"),
        cash=Decimal("0.004"),
    )
    assert pt.equity_usd == Decimal("1000.00")
    assert pt.equity_krw == Decimal("1300000.01")
    assert pt.holdings == Decimal("10.00")
    assert pt.cash == Decimal("0.00")
    for v in (pt.equity_usd, pt.equity_krw, pt.holdings, pt.cash):
        assert type(v) is Decimal
        assert _decimal_places(v) == 2


def test_backtest_result_holds_ordered_ledgers() -> None:
    pt = EquityPoint(
        date=date(2024, 1, 2),
        equity_usd=Decimal("1000.00"),
        equity_krw=Decimal("1300000.00"),
        holdings=Decimal("0.00"),
        cash=Decimal("1000.00"),
    )
    result = BacktestResult(
        equity_curve=(pt,),
        fills=(),
        trades=(),
        realized_pnl_usd=Decimal("0.00"),
        total_tax_usd=Decimal("0.00"),
        start_capital_usd=Decimal("1000.00"),
        fingerprint="abc",
    )
    assert result.equity_curve == (pt,)
    assert result.realized_pnl_usd == Decimal("0.00")
    assert result.total_tax_usd == Decimal("0.00")
    assert result.fingerprint == "abc"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.fingerprint = "xyz"  # type: ignore[misc]
