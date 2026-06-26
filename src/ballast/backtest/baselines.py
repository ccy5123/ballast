"""Leverage baselines for equal-footing comparison (REQ-BACKTEST-001-R5).

* :func:`index_buy_hold` — a synthetic ``k x`` daily-rebalanced curve that compounds
  the index's daily return by ``k`` (modelling leveraged-ETF daily reset from the
  index level, not the ETF price). Money stays :class:`Decimal`.
* :func:`naive_buy_hold` — buy the full capital of the target ETF at the first close,
  hold to the last, mark to market, and run the single terminal realized gain through
  the same :class:`CostModel` (commission on both legs, tax on the terminal gain).

When no index series is provided the caller computes only the naive ETF buy & hold
and omits the 1x/2x/3x curves rather than fabricating them.

@CODE:SPEC-BACKTEST-001
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from ballast.backtest.costs import CostModel
from ballast.backtest.types import (
    BacktestResult,
    EquityCurve,
    EquityPoint,
    Fill,
    OHLCBar,
    Trade,
)
from ballast.core.models import Order, OrderType, Side, quantize_money

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BASELINE_ACCT = "baseline"


def index_buy_hold(index: Sequence[tuple[date, Decimal]], k: int, capital: Decimal) -> EquityCurve:
    """Return the synthetic ``k x`` daily-rebalanced buy-&-hold curve from ``index``.

    The curve starts at ``capital`` and, on each step, multiplies by
    ``(1 + k * daily_index_return)`` — so a ``+10%`` index move becomes ``+30%`` on
    the ``k = 3`` curve. Values are quantized to two places.
    """
    points: list[tuple[date, Decimal]] = []
    value = quantize_money(capital)
    k_dec = Decimal(k)
    for i, (d, level) in enumerate(index):
        if i == 0:
            value = quantize_money(capital)
        else:
            prev_level = index[i - 1][1]
            daily_return = (level - prev_level) / prev_level
            value = quantize_money(value * (_ONE + k_dec * daily_return))
        points.append((d, value))
    return EquityCurve(leverage=k, points=tuple(points))


def naive_buy_hold(bars: Sequence[OHLCBar], capital: Decimal, costs: CostModel) -> BacktestResult:
    """Buy the full ``capital`` at the first close and hold to the last (R5).

    The buy and the single terminal sell both pay ``commission_per_trade``; the
    terminal realized gain runs through the cost model, and the final equity point
    is reduced by the annual tax on that gain.
    """
    ordered = sorted(bars, key=lambda b: b.date)
    if not ordered:
        return BacktestResult(
            equity_curve=(),
            fills=(),
            trades=(),
            realized_pnl_usd=_ZERO,
            total_tax_usd=_ZERO,
            start_capital_usd=quantize_money(capital),
            fingerprint="naive:empty",
        )

    first, last = ordered[0], ordered[-1]
    commission = costs.commission_per_trade
    # Deploy the FULL capital into shares at the first close; the buy commission is
    # an additional debit (the residual cash, possibly slightly negative, carries).
    qty = quantize_money(capital / first.close)
    cash = quantize_money(capital - qty * first.close - commission)

    buy_order = Order(
        side=Side.BUY,
        ticker="BASELINE",
        qty=qty,
        limit_price=first.close,
        order_type=OrderType.MARKET,
        account_seq=_BASELINE_ACCT,
    )
    sell_order = Order(
        side=Side.SELL,
        ticker="BASELINE",
        qty=qty,
        limit_price=last.close,
        order_type=OrderType.MARKET,
        account_seq=_BASELINE_ACCT,
    )
    fills = (
        Fill(
            order=buy_order,
            fill_price=first.close,
            qty=qty,
            date=first.date,
            commission=commission,
        ),
        Fill(
            order=sell_order,
            fill_price=last.close,
            qty=qty,
            date=last.date,
            commission=commission,
        ),
    )

    realized = costs.realized_gain_usd(
        sell_price=last.close,
        avg_cost=first.close,
        qty=qty,
        commission=commission,
    )
    tax = costs.annual_tax(realized, last.fx_usdkrw)

    curve = _naive_curve(ordered, qty, cash, tax)
    trades = (
        Trade(
            side=Side.SELL,
            qty=qty,
            realized_gain_usd=realized,
            tax_year=last.date.year,
        ),
    )
    return BacktestResult(
        equity_curve=curve,
        fills=fills,
        trades=trades,
        realized_pnl_usd=realized,
        total_tax_usd=tax,
        start_capital_usd=quantize_money(capital),
        fingerprint=f"naive:{first.date.isoformat()}:{last.date.isoformat()}:{qty}",
    )


def _naive_curve(
    bars: Sequence[OHLCBar], qty: Decimal, cash: Decimal, terminal_tax: Decimal
) -> tuple[EquityPoint, ...]:
    """Mark the held position to market each bar; subtract tax on the last point."""
    points: list[EquityPoint] = []
    last_index = len(bars) - 1
    for i, bar in enumerate(bars):
        equity_usd = quantize_money(cash + qty * bar.close)
        bar_cash = cash
        if i == last_index and terminal_tax > _ZERO:
            equity_usd = quantize_money(equity_usd - terminal_tax)
            bar_cash = quantize_money(cash - terminal_tax)
        points.append(
            EquityPoint(
                date=bar.date,
                equity_usd=equity_usd,
                equity_krw=quantize_money(equity_usd * bar.fx_usdkrw),
                holdings=qty,
                cash=bar_cash,
            )
        )
    return tuple(points)
