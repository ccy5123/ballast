"""Tests for the metrics bundle (REQ-BACKTEST-001-R4).

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ballast.backtest.costs import CostModel
from ballast.backtest.engine import run_backtest
from ballast.backtest.metrics import compute_metrics
from ballast.backtest.types import BacktestResult, EquityPoint
from ballast.core.models import Order, OrderType, Side, State
from ballast.core.strategy import PlanResult

from .conftest import NoopStrategy, OneShotBuyStrategy, bar


def _vr_start_state(pool: str) -> State:
    return State(ns="vr", data={"V_n": Decimal("0"), "pool": Decimal(pool), "qty": Decimal("0")})


def test_empty_curve_metrics_report_start_capital() -> None:
    result = BacktestResult(
        equity_curve=(),
        fills=(),
        trades=(),
        realized_pnl_usd=Decimal("0.00"),
        total_tax_usd=Decimal("0.00"),
        start_capital_usd=Decimal("1000.00"),
        fingerprint="empty",
    )
    metrics = compute_metrics(result, basis="USD")
    assert metrics.cagr == 0.0
    assert metrics.mdd == 0.0
    assert metrics.tax_drag == Decimal("0.00")
    assert metrics.final_assets_usd == Decimal("1000.00")


def test_turnover_zero_when_average_equity_is_zero() -> None:
    # A degenerate curve at zero equity → turnover guarded to 0 (no ZeroDivision).
    pt = EquityPoint(
        date=date(2024, 1, 2),
        equity_usd=Decimal("0.00"),
        equity_krw=Decimal("0.00"),
        holdings=Decimal("0.00"),
        cash=Decimal("0.00"),
    )
    result = BacktestResult(
        equity_curve=(pt, pt),
        fills=(),
        trades=(),
        realized_pnl_usd=Decimal("0.00"),
        total_tax_usd=Decimal("0.00"),
        start_capital_usd=Decimal("0.00"),
        fingerprint="zero",
    )
    metrics = compute_metrics(result, basis="USD")
    assert metrics.turnover == 0.0


def test_tax_drag_zero_when_start_capital_zero() -> None:
    pt = EquityPoint(
        date=date(2024, 1, 2),
        equity_usd=Decimal("0.00"),
        equity_krw=Decimal("0.00"),
        holdings=Decimal("0.00"),
        cash=Decimal("0.00"),
    )
    result = BacktestResult(
        equity_curve=(pt,),
        fills=(),
        trades=(),
        realized_pnl_usd=Decimal("0.00"),
        total_tax_usd=Decimal("5.00"),
        start_capital_usd=Decimal("0.00"),
        fingerprint="zero-start",
    )
    metrics = compute_metrics(result, basis="USD")
    assert metrics.tax_drag == Decimal("0.00")


# --- Scenario 9 — no-trade run: flat metrics ------------------------------- #
def test_flat_curve_metrics_are_zero() -> None:
    series = [
        bar(date(2024, 1, 2), "100", "100", "100", "100"),
        bar(date(2024, 6, 1), "100", "100", "100", "100"),
        bar(date(2024, 12, 31), "100", "100", "100", "100"),
    ]
    result = run_backtest(
        NoopStrategy(cadence="daily", ns="vr"),
        series,
        cfg=None,
        start_state=_vr_start_state("1000.00"),
    )
    metrics = compute_metrics(result, basis="KRW")
    assert metrics.basis == "KRW"
    assert metrics.cagr == 0.0
    assert metrics.mdd == 0.0
    assert metrics.tax_drag == Decimal("0.00")
    assert metrics.final_assets_usd == Decimal("1000.00")
    assert metrics.final_assets_krw == Decimal("1300000.00")
    assert metrics.rebalance_count == 0
    assert metrics.profit_take_count == 0


def test_basis_switch_changes_final_assets_reporting_only() -> None:
    series = [bar(date(2024, 1, 2), "100", "100", "100", "100")]
    result = run_backtest(NoopStrategy(), series, cfg=None, start_state=_vr_start_state("1000.00"))
    krw = compute_metrics(result, basis="KRW")
    usd = compute_metrics(result, basis="USD")
    assert krw.basis == "KRW"
    assert usd.basis == "USD"
    # Money-denominated finals come from the Decimal ledger regardless of basis.
    assert krw.final_assets_usd == usd.final_assets_usd == Decimal("1000.00")


def test_growing_curve_has_positive_cagr_and_zero_mdd() -> None:
    # Monotonic up: holdings appreciate, no drawdown.
    series = [
        bar(date(2024, 1, 2), "100", "100", "100", "100"),
        bar(date(2025, 1, 2), "100", "120", "100", "120"),
    ]
    strat = OneShotBuyStrategy(qty="10.00", limit="100.00")
    result = run_backtest(strat, series, cfg=None, start_state=_vr_start_state("1000.00"))
    metrics = compute_metrics(result, basis="USD")
    # 1000 → 1200 over ~1 year ≈ +20% CAGR.
    assert metrics.cagr > 0.15
    assert metrics.mdd == 0.0
    assert metrics.final_assets_usd == Decimal("1200.00")


def test_drawdown_is_measured() -> None:
    # up then down then up: a clear peak-to-trough.
    series = [
        bar(date(2024, 1, 2), "100", "100", "100", "100"),
        bar(date(2024, 2, 2), "100", "150", "100", "150"),
        bar(date(2024, 3, 2), "100", "150", "75", "75"),
        bar(date(2024, 4, 2), "100", "120", "75", "120"),
    ]
    strat = OneShotBuyStrategy(qty="10.00", limit="100.00")
    result = run_backtest(strat, series, cfg=None, start_state=_vr_start_state("1000.00"))
    metrics = compute_metrics(result, basis="USD")
    # peak equity 1500 (10*150), trough 750 (10*75) → MDD == 0.5
    assert abs(metrics.mdd - 0.5) < 1e-9


# --- Scenario 7 — tax drag > 0 for a profitable, frequent-sell sequence ---- #
class FrequentSellStrategy:
    """Buys once, then sells one share each subsequent bar (frequent realizing)."""

    cadence = "daily"
    ns = "vr"

    def __init__(self, ticker: str = "TQQQ") -> None:
        self._ticker = ticker
        self._calls = 0

    def plan_orders(self, market: object, state: object, cfg: object) -> PlanResult:
        self._calls += 1
        if self._calls == 1:
            return PlanResult(
                orders=(
                    Order(
                        side=Side.BUY,
                        ticker=self._ticker,
                        qty=Decimal("100.00"),
                        limit_price=Decimal("100.00"),
                        order_type=OrderType.LOC,
                        account_seq="0001",
                    ),
                )
            )
        # Sell 1 share at a high LOC limit that the close always clears.
        return PlanResult(
            orders=(
                Order(
                    side=Side.SELL,
                    ticker=self._ticker,
                    qty=Decimal("1.00"),
                    limit_price=Decimal("1.00"),
                    order_type=OrderType.LOC,
                    account_seq="0001",
                ),
            )
        )


def test_tax_drag_positive_for_profitable_frequent_sells() -> None:
    # Strong up-trend; many small profit-take SELLs within one calendar year that
    # aggregate well above the 2.5M KRW (≈1923 USD) deduction → positive tax drag.
    closes = ["100"] + [str(200 + i) for i in range(60)]
    series = [
        bar(date(2024, 1, 1) if i == 0 else _day(i), "100", "9999", "1", c)
        for i, c in enumerate(closes)
    ]
    result = run_backtest(
        FrequentSellStrategy(),
        series,
        cfg=None,
        start_state=_vr_start_state("100000.00"),
        costs=CostModel(),
    )
    assert result.total_tax_usd > Decimal("0.00")
    metrics = compute_metrics(result, basis="KRW")
    assert metrics.tax_drag > Decimal("0.00")
    assert metrics.profit_take_count >= 1


def _day(i: int) -> date:
    # Spread bars across one calendar year (all 2024) deterministically.
    from datetime import timedelta

    return date(2024, 1, 1) + timedelta(days=i)
