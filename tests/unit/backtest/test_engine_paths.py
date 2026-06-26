"""Additional engine path coverage (REQ-BACKTEST-001-R1).

Covers a full-position sell (avg_price reset), a real Config ticker resolution, a
``cycle`` strategy with a monthly mid-month bar, and a DataFrame carrying adj_close.

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ballast.backtest.engine import run_backtest
from ballast.backtest.types import OHLCBar
from ballast.core.config import Config
from ballast.core.models import Order, OrderType, Side, State
from ballast.core.strategy import PlanResult

from .conftest import CountingStrategy, NoopStrategy, bar

# The canonical example Config (TQQQ vr / SOXL mab) from the core spec.
_CONFIG_YAML = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true
execution:
  broker: toss
  dry_run: true
  max_position_pct: "0.95"
instruments:
  TQQQ:
    leverage: 3
    underlying: NDX
    default_target_pct: "0.50"
    default_band: "0.15"
  SOXL:
    leverage: 3
    underlying: SOX
    default_target_pct: "0.40"
    default_band: "0.20"
strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
  mab:
    account_seq: "0002"
    ticker: SOXL
    target_pct: "0.45"
"""


def _config(tmp_path: object) -> Config:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    p = tmp_path / "config.yaml"
    p.write_text(_CONFIG_YAML, encoding="utf-8")
    return Config.load(p)


class BuyThenSellAllStrategy:
    """Buys on bar 1, sells the WHOLE position on bar 2 (resets avg_price)."""

    cadence = "daily"
    ns = "vr"

    def __init__(self) -> None:
        self._calls = 0

    def plan_orders(self, market: object, state: object, cfg: object) -> PlanResult:
        self._calls += 1
        if self._calls == 1:
            return PlanResult(
                orders=(
                    Order(
                        side=Side.BUY,
                        ticker="TQQQ",
                        qty=Decimal("10.00"),
                        limit_price=Decimal("100.00"),
                        order_type=OrderType.LOC,
                        account_seq="0001",
                    ),
                )
            )
        if self._calls == 2:
            return PlanResult(
                orders=(
                    Order(
                        side=Side.SELL,
                        ticker="TQQQ",
                        qty=Decimal("10.00"),
                        limit_price=Decimal("1.00"),  # low limit → always fills
                        order_type=OrderType.LOC,
                        account_seq="0001",
                    ),
                )
            )
        return PlanResult()


def test_full_position_sell_resets_avg_price_and_realizes_gain() -> None:
    series = [
        bar(date(2024, 1, 2), "100", "100", "100", "100"),
        bar(date(2024, 1, 3), "100", "120", "100", "120"),
        bar(date(2024, 1, 4), "100", "120", "100", "120"),
    ]
    result = run_backtest(
        BuyThenSellAllStrategy(),
        series,
        cfg=None,
        start_state=State(
            ns="vr",
            data={"V_n": Decimal("0"), "pool": Decimal("1000.00"), "qty": Decimal("0")},
        ),
    )
    # Sold all 10 @ 120 from avg 100 → realized (120-100)*10 = 200.00
    assert result.realized_pnl_usd == Decimal("200.00")
    assert result.equity_curve[-1].holdings == Decimal("0.00")
    # cash back to 1000 (start) + 200 gain = 1200; equity flat afterward.
    assert result.equity_curve[-1].cash == Decimal("1200.00")


def test_real_config_resolves_market_ticker_for_mab(tmp_path: object) -> None:
    cfg = _config(tmp_path)
    strat = CountingStrategy(cadence="daily", ns="mab")
    series = [bar(date(2024, 1, 2), "100", "100", "100", "100")]
    run_backtest(
        strat,
        series,
        cfg,
        start_state=State(ns="mab", data={}),
    )
    assert strat.calls == 1


def test_real_config_resolves_market_ticker_for_vr(tmp_path: object) -> None:
    cfg = _config(tmp_path)
    strat = CountingStrategy(cadence="cycle", ns="vr")
    series = [bar(date(2024, 1, 2), "100", "100", "100", "100")]
    run_backtest(
        strat,
        series,
        cfg,
        start_state=State(ns="vr", data={}),
        cycle_length="monthly",
    )
    assert strat.calls == 1


def test_monthly_cycle_skips_same_month_bars() -> None:
    # Two bars in the SAME month → only the first triggers (the 182->185 branch).
    strat = CountingStrategy(cadence="cycle", ns="vr")
    series = [
        bar(date(2024, 1, 2), "100", "100", "100", "100"),
        bar(date(2024, 1, 20), "100", "100", "100", "100"),
    ]
    run_backtest(
        strat,
        series,
        cfg=None,
        start_state=State(ns="vr", data={}),
        cycle_length="monthly",
    )
    assert strat.calls == 1  # second (same-month) bar does not trigger


def test_dataframe_with_adj_close_is_converted() -> None:
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        [
            {
                "date": date(2024, 1, 2),
                "open": Decimal("100"),
                "high": Decimal("100"),
                "low": Decimal("100"),
                "close": Decimal("100"),
                "adj_close": Decimal("99.5"),
                "fx_usdkrw": Decimal("1300"),
            },
            {
                "date": date(2024, 1, 3),
                "open": Decimal("100"),
                "high": Decimal("110"),
                "low": Decimal("100"),
                "close": Decimal("110"),
                "adj_close": Decimal("109.5"),
                "fx_usdkrw": Decimal("1300"),
            },
        ]
    )
    result = run_backtest(
        NoopStrategy(),
        df,
        cfg=None,
        start_state=State(ns="vr", data={"pool": Decimal("1000.00")}),
    )
    assert len(result.equity_curve) == 2
    assert result.equity_curve[0].equity_usd == Decimal("1000.00")


def test_unsorted_bars_are_ordered_ascending() -> None:
    series: list[OHLCBar] = [
        bar(date(2024, 1, 4), "100", "100", "100", "120"),
        bar(date(2024, 1, 2), "100", "100", "100", "100"),
        bar(date(2024, 1, 3), "100", "100", "100", "110"),
    ]
    result = run_backtest(
        NoopStrategy(),
        series,
        cfg=None,
        start_state=State(ns="vr", data={"pool": Decimal("1000.00")}),
    )
    dates = [p.date for p in result.equity_curve]
    assert dates == sorted(dates)
