"""Public surface of the ballast backtest layer.

This layer is the stateful IO/orchestration boundary and MAY use pandas/numpy
(the pure ``ballast.core`` layer may not). It REUSES the CORE-001 domain types and
drives strategies only through the ``Strategy`` protocol.

@CODE:SPEC-BACKTEST-001
"""

from __future__ import annotations

from ballast.backtest.baselines import index_buy_hold, naive_buy_hold
from ballast.backtest.costs import CostModel
from ballast.backtest.engine import run_backtest
from ballast.backtest.fills import simulate_fill
from ballast.backtest.metrics import Basis, Metrics, compute_metrics
from ballast.backtest.types import (
    BacktestResult,
    EquityCurve,
    EquityPoint,
    Fill,
    OHLCBar,
    Trade,
)

__all__ = [
    "BacktestResult",
    "Basis",
    "CostModel",
    "EquityCurve",
    "EquityPoint",
    "Fill",
    "Metrics",
    "OHLCBar",
    "Trade",
    "compute_metrics",
    "index_buy_hold",
    "naive_buy_hold",
    "run_backtest",
    "simulate_fill",
]
