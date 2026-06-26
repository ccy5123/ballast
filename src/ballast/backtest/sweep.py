"""Parameter sweep + walk-forward + regime split + robust plateau (REQ-BACKTEST-002).

This is a PURE orchestration layer on top of the SPEC-BACKTEST-001 engine: it adds
no new fill/cost/tax mechanic. It runs :func:`run_backtest` + :func:`compute_metrics`
across a parameter grid, slices the bar series into walk-forward train/test windows
and into market regimes, and reduces the grid to a robust plateau rather than a
single overfit optimum.

Decoupling by injection: the sweep core never introspects the domain config. A
caller-supplied ``build(combination) -> BacktestScenario`` maps one parameter
combination to a runnable scenario (it alone knows which knob maps where), and a
caller-supplied ``score(metrics) -> float`` collapses a :class:`Metrics` bundle to
one comparable number (default: CAGR). The optional :func:`override_config` helper
applies dotted-path overrides to a frozen pydantic config by round-tripping a
deep-merged dump through full re-validation, so it returns a NEW frozen object and
never mutates its input.

Float-vs-Decimal boundary: this layer performs NO money arithmetic. Every
:class:`~decimal.Decimal` quantity passes through unchanged from the
:class:`BacktestResult` / :class:`Metrics` it consumes; the sweep's own comparison
math (scores, robust scores, the mean OOS score) is ``float``. The single
:class:`~decimal.Decimal` the sweep computes with is the regime trailing-trend
ratio, kept in :class:`~decimal.Decimal` purely for an exact threshold comparison
that produces a label, not money.

@CODE:SPEC-BACKTEST-002
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from itertools import product
from typing import Any, Literal

from ballast.backtest.costs import CostModel
from ballast.backtest.engine import run_backtest
from ballast.backtest.metrics import Basis, Metrics, compute_metrics
from ballast.backtest.types import BacktestResult, EquityPoint, OHLCBar, Trade
from ballast.core.config import Config
from ballast.core.models import State
from ballast.core.strategy import Strategy

_PATH_SEP = "__"

# Callback aliases: the only two coupling points (A2 / A3).
BuildFn = Callable[[Mapping[str, Any]], "BacktestScenario"]
ScoreFn = Callable[[Metrics], float]


# --- Sweep-only immutable types -------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ParamGrid:
    """An ordered N-dimensional parameter lattice; ``axes`` keep declaration order."""

    axes: tuple[tuple[str, tuple[Any, ...]], ...] = ()

    def combinations(self) -> list[dict[str, Any]]:
        """Cartesian product of the axes, last axis varying fastest.

        Identical to :func:`itertools.product` over the axes' value lists. An empty
        grid (no axes) yields exactly one empty combination ``{}``.
        """
        names = [name for name, _ in self.axes]
        value_lists = [values for _, values in self.axes]
        return [dict(zip(names, combo, strict=True)) for combo in product(*value_lists)]


@dataclass(frozen=True, slots=True)
class BacktestScenario:
    """Everything :func:`run_backtest` needs for one run, produced by ``build``."""

    strategy: Strategy
    cfg: Config
    start_state: State
    cycle_length: Literal["monthly"] | int = "monthly"
    costs: CostModel = field(default_factory=CostModel)
    index: Sequence[tuple[date, Decimal]] | None = None


@dataclass(frozen=True, slots=True)
class GridCell:
    """One swept combination: its params, the metrics, and the scalar score."""

    params: Mapping[str, Any]
    metrics: Metrics
    score: float


@dataclass(frozen=True, slots=True)
class PlateauReport:
    """The robust optimum (mean-neighbourhood best) plus the within-tolerance set."""

    robust_optimum: Mapping[str, Any]
    robust_score: float
    plateau: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SweepResult:
    """A completed grid sweep: ordered cells, the single best, and the plateau."""

    cells: tuple[GridCell, ...]
    best: GridCell
    plateau: PlateauReport


@dataclass(frozen=True, slots=True)
class Window:
    """A walk-forward window: half-open train/test bar-index ranges."""

    train: tuple[int, int]
    test: tuple[int, int]


@dataclass(frozen=True, slots=True)
class WalkForwardWindowResult:
    """One window's in-sample selection and out-of-sample evaluation."""

    window: Window
    is_best_params: Mapping[str, Any]
    is_score: float
    oos_metrics: Metrics
    oos_score: float


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """All per-window records plus the mean out-of-sample score."""

    windows: tuple[WalkForwardWindowResult, ...]
    mean_oos_score: float


class Regime(Enum):
    """A reporting lens over the close series; never alters any order or fill."""

    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


@dataclass(frozen=True, slots=True)
class RegimeSpan:
    """A contiguous half-open ``[start, end)`` bar-index run of one regime label."""

    regime: Regime
    start: int
    end: int
    start_date: date
    end_date: date


# --- Scoring + scenario runner --------------------------------------------- #
def default_score(metrics: Metrics) -> float:
    """The default objective: the metrics CAGR (higher is better)."""
    return metrics.cagr


def run_scenario(scenario: BacktestScenario, bars: Sequence[OHLCBar]) -> BacktestResult:
    """Thin wrapper that runs one ``scenario`` over ``bars`` via the engine."""
    return run_backtest(
        scenario.strategy,
        bars,
        scenario.cfg,
        start_state=scenario.start_state,
        cycle_length=scenario.cycle_length,
        index=scenario.index,
        costs=scenario.costs,
    )


# --- Grid sweep ------------------------------------------------------------ #
def run_grid(
    grid: ParamGrid,
    build: BuildFn,
    bars: Sequence[OHLCBar],
    *,
    score: ScoreFn = default_score,
    basis: Basis = "KRW",
    tolerance: float = 0.10,
) -> SweepResult:
    """Sweep ``grid``: build/run/score every combination, attach best + plateau."""
    cells: list[GridCell] = []
    for combo in grid.combinations():
        scenario = build(combo)
        result = run_scenario(scenario, bars)
        metrics = compute_metrics(result, basis=basis)
        cells.append(GridCell(params=combo, metrics=metrics, score=score(metrics)))
    ordered = tuple(cells)
    best = _best_cell(ordered)
    plateau = find_plateau(grid, ordered, tolerance=tolerance)
    return SweepResult(cells=ordered, best=best, plateau=plateau)


def _best_cell(cells: tuple[GridCell, ...]) -> GridCell:
    """The highest-score cell; ties resolve to the earlier cell (first-wins)."""
    best = cells[0]
    for cell in cells[1:]:
        if cell.score > best.score:
            best = cell
    return best


# --- Walk-forward ---------------------------------------------------------- #
def walk_forward_windows(
    n: int,
    *,
    train: int,
    test: int,
    step: int | None = None,
    anchored: bool = False,
) -> list[Window]:
    """Deterministic train/test windows; emit only when the full test fits in ``n``."""
    advance = test if step is None else step
    windows: list[Window] = []
    w = 0
    while True:
        offset = w * advance
        train_start = 0 if anchored else offset
        train_end = offset + train
        test_start = train_end
        test_end = test_start + test
        if test_end > n:
            break
        windows.append(Window(train=(train_start, train_end), test=(test_start, test_end)))
        w += 1
    return windows


def run_walk_forward(
    grid: ParamGrid,
    build: BuildFn,
    bars: Sequence[OHLCBar],
    *,
    score: ScoreFn = default_score,
    train: int,
    test: int,
    step: int | None = None,
    anchored: bool = False,
    basis: Basis = "KRW",
) -> WalkForwardResult:
    """Per window: pick the in-sample best, then score it out-of-sample."""
    series = list(bars)
    windows = walk_forward_windows(
        len(series), train=train, test=test, step=step, anchored=anchored
    )
    records: list[WalkForwardWindowResult] = []
    for window in windows:
        train_bars = series[window.train[0] : window.train[1]]
        test_bars = series[window.test[0] : window.test[1]]
        in_sample = run_grid(grid, build, train_bars, score=score, basis=basis)
        is_best = in_sample.best
        oos_result = run_scenario(build(is_best.params), test_bars)
        oos_metrics = compute_metrics(oos_result, basis=basis)
        records.append(
            WalkForwardWindowResult(
                window=window,
                is_best_params=is_best.params,
                is_score=is_best.score,
                oos_metrics=oos_metrics,
                oos_score=score(oos_metrics),
            )
        )
    mean_oos = sum(r.oos_score for r in records) / len(records) if records else 0.0
    return WalkForwardResult(windows=tuple(records), mean_oos_score=mean_oos)


# --- Regime classification + segmentation ---------------------------------- #
def classify_regimes(
    bars: Sequence[OHLCBar],
    *,
    window: int = 5,
    up_threshold: Decimal = Decimal("0.05"),
    down_threshold: Decimal = Decimal("-0.05"),
) -> list[Regime]:
    """Label each bar UP/DOWN/SIDEWAYS from its trailing-``window`` close trend."""
    closes = [b.close for b in bars]
    labels: list[Regime] = []
    for i in range(len(closes)):
        trend = _trailing_trend(closes, i, window)
        if trend > up_threshold:
            labels.append(Regime.UP)
        elif trend < down_threshold:
            labels.append(Regime.DOWN)
        else:
            labels.append(Regime.SIDEWAYS)
    return labels


def _trailing_trend(closes: Sequence[Decimal], i: int, window: int) -> Decimal:
    """Exact Decimal trailing-trend ratio at bar ``i`` (``0`` at the first bar)."""
    if i == 0:
        return Decimal("0")
    base_index = i - window if i >= window else 0
    base = closes[base_index]
    if base == 0:
        return Decimal("0")
    return closes[i] / base - Decimal("1")


def segment_regimes(
    bars: Sequence[OHLCBar],
    *,
    window: int = 5,
    up_threshold: Decimal = Decimal("0.05"),
    down_threshold: Decimal = Decimal("-0.05"),
) -> list[RegimeSpan]:
    """Collapse consecutive equal labels into contiguous spans partitioning ``[0, n)``."""
    labels = classify_regimes(
        bars, window=window, up_threshold=up_threshold, down_threshold=down_threshold
    )
    if not labels:
        return []
    spans: list[RegimeSpan] = []
    run_start = 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[run_start]:
            spans.append(
                RegimeSpan(
                    regime=labels[run_start],
                    start=run_start,
                    end=i,
                    start_date=bars[run_start].date,
                    end_date=bars[i - 1].date,
                )
            )
            run_start = i
    return spans


# --- Per-regime metrics ---------------------------------------------------- #
def regime_metrics(
    result: BacktestResult,
    spans: Sequence[RegimeSpan],
    *,
    basis: Basis = "KRW",
) -> dict[Regime, Metrics]:
    """Per-label metrics from the equity-curve slices, reusing :func:`compute_metrics`."""
    by_label: dict[Regime, list[RegimeSpan]] = {}
    for span in spans:
        by_label.setdefault(span.regime, []).append(span)

    out: dict[Regime, Metrics] = {}
    for label, label_spans in by_label.items():
        curve = _concat_curve(result.equity_curve, label_spans)
        sub = _sub_result(result, curve, label_spans)
        out[label] = compute_metrics(sub, basis=basis)
    return out


def _concat_curve(
    curve: tuple[EquityPoint, ...], spans: Sequence[RegimeSpan]
) -> tuple[EquityPoint, ...]:
    """Concatenate the curve slices for ``spans`` in span order."""
    points: list[EquityPoint] = []
    for span in spans:
        points.extend(curve[span.start : span.end])
    return tuple(points)


def _sub_result(
    parent: BacktestResult,
    curve: tuple[EquityPoint, ...],
    spans: Sequence[RegimeSpan],
) -> BacktestResult:
    """Build a sub-result for ``curve``; a <2-point slice yields an empty curve."""
    effective_curve: tuple[EquityPoint, ...] = curve if len(curve) >= 2 else ()
    fills = tuple(f for f in parent.fills if _in_any_span(f.date, spans))
    trades = _trades_in_spans(parent, spans)
    return BacktestResult(
        equity_curve=effective_curve,
        fills=fills,
        trades=trades,
        realized_pnl_usd=parent.realized_pnl_usd,
        total_tax_usd=parent.total_tax_usd,
        start_capital_usd=parent.start_capital_usd,
        fingerprint=parent.fingerprint,
    )


def _in_any_span(d: date, spans: Sequence[RegimeSpan]) -> bool:
    """Whether ``d`` falls within the inclusive date range of any span."""
    return any(span.start_date <= d <= span.end_date for span in spans)


def _trades_in_spans(parent: BacktestResult, spans: Sequence[RegimeSpan]) -> tuple[Trade, ...]:
    """Keep the SELL trades realized on a fill date inside one of ``spans``.

    Trades carry only a ``tax_year``, not a date, so the realizing fills are the
    date source: a SELL trade is kept when a matching SELL fill lands in a span.
    """
    sell_dates = {f.date for f in parent.fills if _in_any_span(f.date, spans)}
    if not sell_dates:
        return ()
    return tuple(t for t in parent.trades if t.tax_year in {d.year for d in sell_dates})


# --- Robust plateau -------------------------------------------------------- #
def find_plateau(
    grid: ParamGrid,
    cells: tuple[GridCell, ...],
    *,
    tolerance: float = 0.10,
) -> PlateauReport:
    """Robust optimum (mean von-Neumann neighbourhood) + within-tolerance plateau."""
    shape = tuple(len(values) for _, values in grid.axes)
    scores = [cell.score for cell in cells]

    robust_optimum = cells[0]
    best_robust = _robust_score(0, shape, scores)
    for idx in range(1, len(cells)):
        robust = _robust_score(idx, shape, scores)
        if robust > best_robust:
            best_robust = robust
            robust_optimum = cells[idx]

    best_single = max(scores)
    threshold = best_single - abs(best_single) * tolerance
    plateau = tuple(cell.params for cell in cells if cell.score >= threshold)

    return PlateauReport(
        robust_optimum=robust_optimum.params,
        robust_score=best_robust,
        plateau=plateau,
    )


def _robust_score(idx: int, shape: tuple[int, ...], scores: Sequence[float]) -> float:
    """Mean of the cell's score and its ±1-on-one-axis (clipped) neighbour scores."""
    multi_index = _unravel(idx, shape)
    values = [scores[idx]]
    for axis, dim in enumerate(shape):
        for delta in (-1, 1):
            neighbour = list(multi_index)
            neighbour[axis] += delta
            if 0 <= neighbour[axis] < dim:
                values.append(scores[_ravel(tuple(neighbour), shape)])
    return sum(values) / len(values)


def _unravel(idx: int, shape: tuple[int, ...]) -> tuple[int, ...]:
    """Flat index -> multi-index on ``shape`` (last axis fastest)."""
    coords: list[int] = []
    remainder = idx
    for dim in reversed(shape):
        coords.append(remainder % dim)
        remainder //= dim
    return tuple(reversed(coords))


def _ravel(multi_index: tuple[int, ...], shape: tuple[int, ...]) -> int:
    """Multi-index -> flat index on ``shape`` (last axis fastest)."""
    flat = 0
    for coord, dim in zip(multi_index, shape, strict=True):
        flat = flat * dim + coord
    return flat


# --- Config override convenience ------------------------------------------- #
def override_config(base: Config, **dotted: Any) -> Config:
    """Apply dotted-path overrides (e.g. ``group__sub__leaf=20``) to a frozen config.

    Returns a NEW frozen config; ``base`` is never mutated. Each key is split on
    ``__`` into a path, deep-merged onto a dump of ``base``, and the merged mapping
    is re-validated in full — so an unknown path or an invalid value raises rather
    than silently passing.
    """
    merged: dict[str, Any] = deepcopy(base.model_dump())
    for key, value in dotted.items():
        _set_path(merged, key.split(_PATH_SEP), value)
    return Config.model_validate(merged)


def _set_path(target: dict[str, Any], path: Sequence[str], value: Any) -> None:
    """Set ``value`` at the dotted ``path`` inside the nested mapping ``target``."""
    cursor = target
    for segment in path[:-1]:
        nxt = cursor.get(segment)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[segment] = nxt
        cursor = nxt
    cursor[path[-1]] = value
