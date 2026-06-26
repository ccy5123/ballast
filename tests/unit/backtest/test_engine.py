"""Tests for the deterministic replay engine (REQ-BACKTEST-001-R1).

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ballast.backtest.costs import CostModel
from ballast.backtest.engine import run_backtest
from ballast.backtest.types import OHLCBar
from ballast.core.models import Side, State

from .conftest import CountingStrategy, NoopStrategy, OneShotBuyStrategy, bar

D1 = date(2024, 1, 2)
D2 = date(2024, 1, 3)
D3 = date(2024, 1, 4)


def _three_bar_series() -> list[OHLCBar]:
    # (date, open, high, low, close)
    return [
        bar(D1, "100", "100", "100", "100"),
        bar(D2, "100", "110", "90", "110"),
        bar(D3, "100", "120", "100", "120"),
    ]


def _vr_start_state(pool: str) -> State:
    return State(ns="vr", data={"V_n": Decimal("0"), "pool": Decimal(pool), "qty": Decimal("0")})


# --- Scenario 1 — 3-bar series + stub → exact equity curve ----------------- #
def test_three_bar_exact_equity_curve_usd_and_krw() -> None:
    strat = OneShotBuyStrategy(qty="10.00", limit="100.00", cadence="daily", ns="vr")
    result = run_backtest(
        strat,
        _three_bar_series(),
        cfg=None,
        start_state=_vr_start_state("1000.00"),
        costs=CostModel(),
    )
    usd = [p.equity_usd for p in result.equity_curve]
    krw = [p.equity_krw for p in result.equity_curve]
    assert usd == [Decimal("1000.00"), Decimal("1100.00"), Decimal("1200.00")]
    assert krw == [Decimal("1300000.00"), Decimal("1430000.00"), Decimal("1560000.00")]

    # D1: BUY fills at close 100 → holdings 10, cash 0
    assert result.equity_curve[0].holdings == Decimal("10.00")
    assert result.equity_curve[0].cash == Decimal("0.00")
    assert len(result.fills) == 1
    assert result.fills[0].fill_price == Decimal("100.00")


def test_three_bar_result_is_deterministic() -> None:
    s1 = OneShotBuyStrategy(qty="10.00", limit="100.00")
    s2 = OneShotBuyStrategy(qty="10.00", limit="100.00")
    r1 = run_backtest(s1, _three_bar_series(), cfg=None, start_state=_vr_start_state("1000.00"))
    r2 = run_backtest(s2, _three_bar_series(), cfg=None, start_state=_vr_start_state("1000.00"))
    assert r1.equity_curve == r2.equity_curve
    assert r1.fills == r2.fills
    assert r1.trades == r2.trades
    assert r1.fingerprint == r2.fingerprint


# --- Scenario 4 — an unfilled order moves nothing -------------------------- #
def test_unfilled_buy_leaves_position_untouched() -> None:
    # limit 90 < every close → the BUY never fills; curve stays flat at start cap.
    strat = OneShotBuyStrategy(qty="10.00", limit="90.00", cadence="daily", ns="vr")
    result = run_backtest(
        strat, _three_bar_series(), cfg=None, start_state=_vr_start_state("1000.00")
    )
    assert all(p.holdings == Decimal("0.00") for p in result.equity_curve)
    assert all(p.cash == Decimal("1000.00") for p in result.equity_curve)
    assert result.fills == ()
    assert result.trades == ()


# --- Scenario 9 — no trades → flat curve, zero tax ------------------------- #
def test_no_trades_flat_curve_zero_tax() -> None:
    strat = NoopStrategy(cadence="daily", ns="vr")
    result = run_backtest(
        strat, _three_bar_series(), cfg=None, start_state=_vr_start_state("1000.00")
    )
    assert [p.equity_usd for p in result.equity_curve] == [Decimal("1000.00")] * 3
    assert [p.equity_krw for p in result.equity_curve] == [Decimal("1300000.00")] * 3
    assert all(p.holdings == Decimal("0.00") for p in result.equity_curve)
    assert result.realized_pnl_usd == Decimal("0.00")
    assert result.total_tax_usd == Decimal("0.00")


def test_empty_series_yields_empty_curve() -> None:
    strat = NoopStrategy()
    result = run_backtest(strat, [], cfg=None, start_state=_vr_start_state("1000.00"))
    assert result.equity_curve == ()
    assert result.start_capital_usd == Decimal("1000.00")


# --- commission reduces cash (Scenario 5 at engine level) ------------------ #
def test_commission_reduces_cash_beyond_share_cost() -> None:
    strat = OneShotBuyStrategy(qty="5.00", limit="100.00", cadence="daily", ns="vr")
    series = [bar(D1, "100", "100", "100", "100")]
    result = run_backtest(
        strat,
        series,
        cfg=None,
        start_state=_vr_start_state("1000.00"),
        costs=CostModel(commission_per_trade=Decimal("1.00")),
    )
    # shares 5*100 = 500, plus 1.00 commission → cash 1000 - 501 = 499.00
    assert result.equity_curve[0].cash == Decimal("499.00")
    assert result.fills[0].commission == Decimal("1.00")


# --- Scenario 11 — cadence: VR per cycle, MAB daily ------------------------ #
def _monthly_series() -> list[OHLCBar]:
    # Three months, a few bars each.
    days = [
        date(2024, 1, 2),
        date(2024, 1, 20),
        date(2024, 2, 1),
        date(2024, 2, 15),
        date(2024, 3, 3),
        date(2024, 3, 28),
    ]
    return [bar(d, "100", "100", "100", "100") for d in days]


def test_mab_triggers_every_bar() -> None:
    strat = CountingStrategy(cadence="daily", ns="mab")
    series = _monthly_series()
    run_backtest(
        strat,
        series,
        cfg=None,
        start_state=State(ns="mab", data={}),
    )
    assert strat.calls == len(series)
    # Each MAB trigger sees the MAB state-key shape.
    expected = frozenset({"avg_price", "holdings", "seed_remaining", "round_idx"})
    assert all(keys == expected for keys in strat.seen_state_keys)


def test_vr_triggers_once_per_month() -> None:
    strat = CountingStrategy(cadence="cycle", ns="vr")
    series = _monthly_series()  # 3 distinct months
    run_backtest(
        strat,
        series,
        cfg=None,
        start_state=_vr_start_state("1000.00"),
        cycle_length="monthly",
    )
    assert strat.calls == 3  # one trigger per calendar month
    expected = frozenset({"V_n", "pool", "qty"})
    assert all(keys == expected for keys in strat.seen_state_keys)


def test_cycle_length_int_triggers_every_n_bars() -> None:
    strat = CountingStrategy(cadence="cycle", ns="vr")
    series = _three_bar_series()  # 3 bars
    run_backtest(
        strat,
        series,
        cfg=None,
        start_state=_vr_start_state("1000.00"),
        cycle_length=2,  # bars 0 and 2 → 2 triggers
    )
    assert strat.calls == 2


# --- engine never mutates the passed-in State ------------------------------ #
def test_engine_does_not_mutate_start_state() -> None:
    start = _vr_start_state("1000.00")
    snapshot = dict(start.data)
    strat = OneShotBuyStrategy(qty="10.00", limit="100.00")
    run_backtest(strat, _three_bar_series(), cfg=None, start_state=start)
    assert start.data == snapshot  # unchanged


# --- Scenario 10 — no look-ahead at the engine level ----------------------- #
@given(
    next_close=st.decimals(min_value="1", max_value="1000", places=2),
    next_low=st.decimals(min_value="1", max_value="1000", places=2),
    next_high=st.decimals(min_value="1", max_value="1000", places=2),
)
def test_engine_bar_t_independent_of_bar_t_plus_one(
    next_close: Decimal, next_high: Decimal, next_low: Decimal
) -> None:
    base = [
        bar(D1, "100", "101", "90", "98"),
        bar(D2, "100", "110", "90", "105"),
    ]
    mutated = [
        base[0],
        bar(D2, "100", str(next_high), str(next_low), str(next_close)),
    ]
    strat_a = OneShotBuyStrategy(qty="3.00", limit="100.00", cadence="daily", ns="vr")
    strat_b = OneShotBuyStrategy(qty="3.00", limit="100.00", cadence="daily", ns="vr")
    r_base = run_backtest(strat_a, base, cfg=None, start_state=_vr_start_state("1000.00"))
    r_mut = run_backtest(strat_b, mutated, cfg=None, start_state=_vr_start_state("1000.00"))
    # The fill and EquityPoint at bar t (D1) are identical regardless of D2.
    assert r_base.equity_curve[0] == r_mut.equity_curve[0]
    assert r_base.fills[:1] == r_mut.fills[:1]


# --- cash conservation invariant ------------------------------------------- #
@given(
    closes=st.lists(
        st.decimals(min_value="50", max_value="150", places=2),
        min_size=1,
        max_size=8,
    )
)
def test_cash_conservation(closes: list[Decimal]) -> None:
    series = [
        bar(date(2024, 1, i + 1), str(c), str(c), str(c), str(c)) for i, c in enumerate(closes)
    ]
    start_cash = Decimal("1000.00")
    strat = OneShotBuyStrategy(qty="5.00", limit="200.00", cadence="daily", ns="vr")
    result = run_backtest(strat, series, cfg=None, start_state=_vr_start_state("1000.00"))

    # Cash moves ONLY by fills (buy debit / sell credit), their commission, and tax;
    # holdings appreciation never touches cash.
    cash_delta = Decimal("0")
    for f in result.fills:
        notional = f.fill_price * f.qty
        if f.order.side is Side.BUY:
            cash_delta -= notional + f.commission
        else:
            cash_delta += notional - f.commission
    expected_cash = start_cash + cash_delta - result.total_tax_usd
    assert abs(result.equity_curve[-1].cash - expected_cash) <= Decimal("0.01")


def test_dataframe_input_is_accepted() -> None:
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        [
            {
                "date": D1,
                "open": Decimal("100"),
                "high": Decimal("100"),
                "low": Decimal("100"),
                "close": Decimal("100"),
                "adj_close": None,
                "fx_usdkrw": Decimal("1300"),
            }
        ]
    )
    strat = NoopStrategy()
    result = run_backtest(strat, df, cfg=None, start_state=_vr_start_state("1000.00"))
    assert result.equity_curve[0].equity_usd == Decimal("1000.00")
