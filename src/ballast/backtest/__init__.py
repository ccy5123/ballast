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
from ballast.backtest.sweep import (
    BacktestScenario,
    GridCell,
    ParamGrid,
    PlateauReport,
    Regime,
    RegimeSpan,
    SweepResult,
    WalkForwardResult,
    WalkForwardWindowResult,
    Window,
    classify_regimes,
    default_score,
    find_plateau,
    override_config,
    regime_metrics,
    run_grid,
    run_scenario,
    run_walk_forward,
    segment_regimes,
    walk_forward_windows,
)
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
    "BacktestScenario",
    "Basis",
    "CostModel",
    "EquityCurve",
    "EquityPoint",
    "Fill",
    "GridCell",
    "Metrics",
    "OHLCBar",
    "ParamGrid",
    "PlateauReport",
    "Regime",
    "RegimeSpan",
    "SweepResult",
    "Trade",
    "WalkForwardResult",
    "WalkForwardWindowResult",
    "Window",
    "classify_regimes",
    "compute_metrics",
    "default_score",
    "find_plateau",
    "index_buy_hold",
    "naive_buy_hold",
    "override_config",
    "regime_metrics",
    "run_backtest",
    "run_grid",
    "run_scenario",
    "run_walk_forward",
    "segment_regimes",
    "simulate_fill",
    "walk_forward_windows",
]
