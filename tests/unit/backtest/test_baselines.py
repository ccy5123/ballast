"""Tests for the leverage baselines (REQ-BACKTEST-001-R5).

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ballast.backtest.baselines import index_buy_hold, naive_buy_hold
from ballast.backtest.costs import CostModel
from ballast.backtest.types import OHLCBar

from .conftest import bar

D1 = date(2024, 1, 2)
D2 = date(2024, 1, 3)


def _index() -> list[tuple[date, Decimal]]:
    return [(D1, Decimal("100.00")), (D2, Decimal("110.00"))]  # +10% move


# --- Scenario 8 — 3x index buy & hold -------------------------------------- #
def test_index_buy_hold_3x_compounds_daily_return_by_k() -> None:
    curve = index_buy_hold(_index(), k=3, capital=Decimal("1000.00"))
    # +10% index → +30% on the 3x daily-rebalanced curve → 1300.00
    assert curve.points[0][1] == Decimal("1000.00")
    assert curve.points[-1][1] == Decimal("1300.00")
    assert curve.leverage == 3


def test_index_buy_hold_1x_and_2x() -> None:
    one = index_buy_hold(_index(), k=1, capital=Decimal("1000.00"))
    two = index_buy_hold(_index(), k=2, capital=Decimal("1000.00"))
    assert one.points[-1][1] == Decimal("1100.00")
    assert two.points[-1][1] == Decimal("1200.00")


def test_index_buy_hold_dates_align_with_index() -> None:
    curve = index_buy_hold(_index(), k=2, capital=Decimal("1000.00"))
    assert [p[0] for p in curve.points] == [D1, D2]


def test_index_buy_hold_money_is_two_place_decimal() -> None:
    curve = index_buy_hold(_index(), k=3, capital=Decimal("1000.00"))
    for _, value in curve.points:
        assert type(value) is Decimal
        exponent = value.as_tuple().exponent
        assert isinstance(exponent, int)
        assert -exponent == 2


# --- naive ETF buy & hold through the cost model --------------------------- #
def _bars() -> list[OHLCBar]:
    return [
        bar(D1, "100", "100", "100", "100"),
        bar(D2, "100", "120", "100", "120"),
    ]


def test_naive_buy_hold_holds_full_capital_to_the_end() -> None:
    result = naive_buy_hold(_bars(), capital=Decimal("1000.00"), costs=CostModel())
    # buy 10 shares @ 100, hold to 120 → final equity 1200.00
    assert result.equity_curve[0].equity_usd == Decimal("1000.00")
    assert result.equity_curve[-1].equity_usd == Decimal("1200.00")
    assert result.equity_curve[-1].holdings == Decimal("10.00")


def test_naive_buy_hold_terminal_gain_runs_through_cost_model() -> None:
    # commission on both legs; one terminal realized gain.
    result = naive_buy_hold(
        _bars(),
        capital=Decimal("1000.00"),
        costs=CostModel(commission_per_trade=Decimal("1.00")),
    )
    # gain = (120-100)*10 - 1 (sell commission) = 199.00 realized
    assert result.realized_pnl_usd == Decimal("199.00")
    assert len(result.fills) == 2  # buy + terminal sell


def test_naive_buy_hold_empty_series_is_flat() -> None:
    result = naive_buy_hold([], capital=Decimal("1000.00"), costs=CostModel())
    assert result.equity_curve == ()
    assert result.realized_pnl_usd == Decimal("0.00")
