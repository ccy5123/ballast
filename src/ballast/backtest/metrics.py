"""Performance metrics from the equity curve and ledger (REQ-BACKTEST-001-R4).

``compute_metrics`` derives CAGR, MDD, annualized volatility, Sharpe, rebalance /
profit-take counts, turnover, tax drag, and final assets, and states whether the
ratio math was done on the KRW or USD curve.

Decimal-vs-float boundary: the curve/ratio math (CAGR/MDD/volatility/Sharpe/
turnover) MAY convert the :class:`Decimal` curve to ``float`` (this module is the
ONLY place that conversion happens, and only for ratios). Money-denominated outputs
(``final_assets_*``, ``tax_drag``) originate from the :class:`Decimal` ledger and
never come back from a ``float``.

@CODE:SPEC-BACKTEST-001
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import numpy as np
import numpy.typing as npt

from ballast.backtest.types import BacktestResult
from ballast.core.models import Side, quantize_money

Basis = Literal["KRW", "USD"]
# A 1-D float64 array: the read-only curve representation for ratio math.
FloatArray = npt.NDArray[np.float64]

_TRADING_DAYS = 252.0
_DAYS_PER_YEAR = 365.25
_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Metrics:
    """A reporting bundle; ``basis`` states the curve ratios were computed on."""

    basis: Basis
    cagr: float
    mdd: float
    volatility: float
    sharpe: float
    rebalance_count: int
    profit_take_count: int
    turnover: float
    tax_drag: Decimal
    final_assets_usd: Decimal
    final_assets_krw: Decimal


def compute_metrics(
    result: BacktestResult, *, basis: Basis = "KRW", risk_free: float = 0.0
) -> Metrics:
    """Compute the metrics bundle for ``result`` on the chosen ``basis``."""
    curve = result.equity_curve
    if not curve:
        return _empty_metrics(basis, result)

    # Decimal -> float, read-only, for ratio math only.
    values: FloatArray = np.array(
        [float(p.equity_krw if basis == "KRW" else p.equity_usd) for p in curve],
        dtype=float,
    )
    days = (curve[-1].date - curve[0].date).days

    cagr = _cagr(values, days)
    mdd = _max_drawdown(values)
    vol = _volatility(values)
    sharpe = _sharpe(values, risk_free)
    turnover = _turnover(result, values)

    rebalance_count = len(result.fills)
    profit_take_count = sum(1 for t in result.trades if t.side is Side.SELL)

    tax_drag = _tax_drag(result)

    return Metrics(
        basis=basis,
        cagr=cagr,
        mdd=mdd,
        volatility=vol,
        sharpe=sharpe,
        rebalance_count=rebalance_count,
        profit_take_count=profit_take_count,
        turnover=turnover,
        tax_drag=tax_drag,
        final_assets_usd=curve[-1].equity_usd,
        final_assets_krw=curve[-1].equity_krw,
    )


def _empty_metrics(basis: Basis, result: BacktestResult) -> Metrics:
    """Metrics for an empty curve: everything zero, finals at start capital."""
    return Metrics(
        basis=basis,
        cagr=0.0,
        mdd=0.0,
        volatility=0.0,
        sharpe=0.0,
        rebalance_count=0,
        profit_take_count=0,
        turnover=0.0,
        tax_drag=quantize_money(_ZERO),
        final_assets_usd=result.start_capital_usd,
        final_assets_krw=quantize_money(_ZERO),
    )


def _cagr(values: FloatArray, days: int) -> float:
    """Compound annual growth rate; ``0`` on flat/degenerate curves."""
    start, end = float(values[0]), float(values[-1])
    if start <= 0.0 or end <= 0.0 or days <= 0:
        return 0.0
    years = days / _DAYS_PER_YEAR
    return float((end / start) ** (1.0 / years) - 1.0)


def _max_drawdown(values: FloatArray) -> float:
    """Largest peak-to-trough decline as a positive fraction; ``0`` if monotone."""
    peak = np.maximum.accumulate(values)
    # Guard against a zero/negative peak (degenerate); treat as no drawdown.
    safe_peak = np.where(peak > 0.0, peak, 1.0)
    drawdowns = (peak - values) / safe_peak
    return float(np.max(drawdowns))


def _periodic_returns(values: FloatArray) -> FloatArray:
    """Simple period-over-period returns; empty when fewer than two points."""
    if values.size < 2:
        return np.array([], dtype=float)
    prev = values[:-1]
    safe_prev = np.where(prev != 0.0, prev, np.nan)
    returns = (values[1:] - prev) / safe_prev
    finite: FloatArray = returns[~np.isnan(returns)]
    return finite


def _volatility(values: FloatArray) -> float:
    """Annualized standard deviation of periodic returns."""
    returns = _periodic_returns(values)
    if returns.size < 2:
        return 0.0
    return float(np.std(returns, ddof=1) * np.sqrt(_TRADING_DAYS))


def _sharpe(values: FloatArray, risk_free: float) -> float:
    """Annualized mean excess return over volatility; ``0`` when vol is ``0``."""
    returns = _periodic_returns(values)
    if returns.size < 2:
        return 0.0
    std = float(np.std(returns, ddof=1))
    if std == 0.0:
        return 0.0
    rf_daily = risk_free / _TRADING_DAYS
    excess = float(np.mean(returns)) - rf_daily
    return float(excess / std * np.sqrt(_TRADING_DAYS))


def _turnover(result: BacktestResult, values: FloatArray) -> float:
    """Total traded notional over average equity (ratio math; float)."""
    traded = sum(float(f.fill_price * f.qty) for f in result.fills)
    avg_equity = float(np.mean(values))
    if avg_equity == 0.0:
        return 0.0
    return traded / avg_equity


def _tax_drag(result: BacktestResult) -> Decimal:
    """Gross-vs-after-tax return gap, sourced from the Decimal ledger (>= 0).

    The curve is after-tax; adding the total tax back to the terminal equity gives
    the pre-tax (gross) terminal value, so the return gap is exactly
    ``total_tax / start_capital`` — non-negative, and ``0`` with no realized tax.
    """
    start = result.start_capital_usd
    if start <= _ZERO:
        return quantize_money(_ZERO)
    drag = result.total_tax_usd / start
    return quantize_money(drag)
