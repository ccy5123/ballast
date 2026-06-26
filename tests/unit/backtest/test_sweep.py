"""Tests for the parameter-sweep / walk-forward / regime / plateau layer.

One test per acceptance scenario (1-11) plus the ``hypothesis`` invariants from
acceptance.md §Quality-Gate (grid size, window coverage, regime partition,
determinism, no-mutation) and the decoupling guard.

@TEST:SPEC-BACKTEST-002
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ballast.backtest.metrics import Metrics
from ballast.backtest.sweep import (
    BacktestScenario,
    GridCell,
    ParamGrid,
    PlateauReport,
    Regime,
    RegimeSpan,
    SweepResult,
    WalkForwardResult,
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
from ballast.backtest.types import OHLCBar
from ballast.core.config import Config
from ballast.core.models import State

from .conftest import NoopStrategy, OneShotBuyStrategy, bar

# --- shared helpers -------------------------------------------------------- #

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

strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
    g: 10
  mab:
    account_seq: "0002"
    ticker: SOXL
"""


def _config(tmp_path: Path) -> Config:
    """Load the reference Config (vr.g == 10) from a temp YAML file."""
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG_YAML, encoding="utf-8")
    return Config.load(path)


def _vr_start_state(pool: str) -> State:
    return State(ns="vr", data={"V_n": Decimal("0"), "pool": Decimal(pool), "qty": Decimal("0")})


def _day(i: int) -> date:
    return date(2024, 1, 1) + timedelta(days=i)


def _up_series(n: int) -> list[OHLCBar]:
    """An ``n``-bar strictly up-trending series (close 100, 110, 120, ...)."""
    return [bar(_day(i), "100", "9999", "1", str(100 + 10 * i)) for i in range(n)]


def _build_oneshot(params: Mapping[str, Any]) -> BacktestScenario:
    """A ``build`` that parameterizes ``OneShotBuyStrategy(qty=...)``; no Config touch."""
    qty = str(params.get("qty", "10.00"))
    return BacktestScenario(
        strategy=OneShotBuyStrategy(qty=qty, limit="100.00", cadence="daily", ns="vr"),
        cfg=None,  # type: ignore[arg-type]
        start_state=_vr_start_state("100000.00"),
    )


def _build_always_fill(params: Mapping[str, Any]) -> BacktestScenario:
    """Like ``_build_oneshot`` but with a high limit so the BUY fills in every window."""
    qty = str(params.get("qty", "10.00"))
    return BacktestScenario(
        strategy=OneShotBuyStrategy(qty=qty, limit="1000000.00", cadence="daily", ns="vr"),
        cfg=None,  # type: ignore[arg-type]
        start_state=_vr_start_state("100000.00"),
    )


def _build_noop(params: Mapping[str, Any]) -> BacktestScenario:
    return BacktestScenario(
        strategy=NoopStrategy(cadence="daily", ns="vr"),
        cfg=None,  # type: ignore[arg-type]
        start_state=_vr_start_state("1000.00"),
    )


def _cell(g: int, score: float) -> GridCell:
    """A GridCell with a hand-set score (metrics content irrelevant to plateau)."""
    metrics = Metrics(
        basis="KRW",
        cagr=score,
        mdd=0.0,
        volatility=0.0,
        sharpe=0.0,
        rebalance_count=0,
        profit_take_count=0,
        turnover=0.0,
        tax_drag=Decimal("0.00"),
        final_assets_usd=Decimal("0.00"),
        final_assets_krw=Decimal("0.00"),
    )
    return GridCell(params={"g": g}, metrics=metrics, score=score)


# --- Scenario 1 — grid enumerates the Cartesian product -------------------- #
def test_scenario_1_grid_cartesian_order() -> None:
    grid = ParamGrid(axes=(("a", (1, 2)), ("b", (10, 20))))
    combos = grid.combinations()
    assert combos == [
        {"a": 1, "b": 10},
        {"a": 1, "b": 20},
        {"a": 2, "b": 10},
        {"a": 2, "b": 20},
    ]
    # Empty grid → exactly one empty combination.
    assert ParamGrid().combinations() == [{}]
    # Count is the product of axis lengths (2 x 3 == 6).
    big = ParamGrid(axes=(("a", (1, 2)), ("b", (10, 20, 30))))
    assert len(big.combinations()) == 6


# --- Scenario 2 — run_grid scores every cell and reports the best ---------- #
def test_scenario_2_run_grid_scores_and_best() -> None:
    grid = ParamGrid(axes=(("qty", ("10.00", "20.00")),))
    bars = _up_series(3)
    result = run_grid(grid, _build_oneshot, bars, score=default_score)

    assert isinstance(result, SweepResult)
    assert [c.params for c in result.cells] == [{"qty": "10.00"}, {"qty": "20.00"}]
    for cell in result.cells:
        assert isinstance(cell.metrics, Metrics)
        assert cell.score == cell.metrics.cagr
    # Buying more in an up-trend yields the higher score.
    assert result.best is max(result.cells, key=lambda c: c.score)
    assert result.best.params == {"qty": "20.00"}

    # Determinism: identical inputs → identical result.
    again = run_grid(grid, _build_oneshot, bars, score=default_score)
    assert result == again


def test_scenario_2_best_tie_breaks_on_enumeration_order() -> None:
    # NoopStrategy → every cell scores 0.0 → tie resolves to the FIRST cell.
    grid = ParamGrid(axes=(("x", (1, 2, 3)),))
    bars = _up_series(3)
    result = run_grid(grid, _build_noop, bars)
    assert all(c.score == 0.0 for c in result.cells)
    assert result.best is result.cells[0]


# --- Scenario 3 — empty grid yields a single empty-combination cell -------- #
def test_scenario_3_empty_grid_single_cell() -> None:
    bars = _up_series(3)
    result = run_grid(ParamGrid(), _build_noop, bars)
    assert len(result.cells) == 1
    assert result.cells[0].params == {}
    assert result.best is result.cells[0]
    assert result.plateau.plateau == ({},)
    assert result.plateau.robust_optimum == {}


# --- Scenario 4 — walk-forward windows ------------------------------------- #
def test_scenario_4_walk_forward_windows_rolling() -> None:
    windows = walk_forward_windows(10, train=4, test=2, step=2)
    assert windows == [
        Window(train=(0, 4), test=(4, 6)),
        Window(train=(2, 6), test=(6, 8)),
        Window(train=(4, 8), test=(8, 10)),
    ]


def test_scenario_4_no_partial_trailing_test_window() -> None:
    windows = walk_forward_windows(9, train=4, test=2, step=2)
    assert windows == [
        Window(train=(0, 4), test=(4, 6)),
        Window(train=(2, 6), test=(6, 8)),
    ]


def test_scenario_4_walk_forward_windows_anchored() -> None:
    windows = walk_forward_windows(10, train=4, test=2, step=2, anchored=True)
    assert windows == [
        Window(train=(0, 4), test=(4, 6)),
        Window(train=(0, 6), test=(6, 8)),
        Window(train=(0, 8), test=(8, 10)),
    ]


def test_scenario_4_step_defaults_to_test() -> None:
    # step=None → step == test (== 2 here): non-overlapping train/test windows.
    windows = walk_forward_windows(8, train=2, test=2)
    assert windows == [
        Window(train=(0, 2), test=(2, 4)),
        Window(train=(2, 4), test=(4, 6)),
        Window(train=(4, 6), test=(6, 8)),
    ]


# --- Scenario 5 — run_walk_forward IS-best → OOS --------------------------- #
def test_scenario_5_run_walk_forward_is_best_then_oos() -> None:
    grid = ParamGrid(axes=(("qty", ("10.00", "20.00")),))
    bars = _up_series(10)
    result = run_walk_forward(
        grid, _build_always_fill, bars, score=default_score, train=4, test=2, step=2
    )
    assert isinstance(result, WalkForwardResult)
    assert len(result.windows) == 3
    for wr in result.windows:
        # With a strategy that fills in every window, buying more wins in an
        # up-trend; the OOS leg runs THAT in-sample best combination.
        assert wr.is_best_params == {"qty": "20.00"}
        assert isinstance(wr.oos_metrics, Metrics)
        assert wr.oos_score == wr.oos_metrics.cagr
        # is_score equals the score of the in-sample best cell for that train slice.
        train_bars = bars[wr.window.train[0] : wr.window.train[1]]
        in_sample = run_grid(grid, _build_always_fill, train_bars)
        assert wr.is_score == in_sample.best.score
    expected_mean = sum(wr.oos_score for wr in result.windows) / len(result.windows)
    assert result.mean_oos_score == pytest.approx(expected_mean)


def test_scenario_5_is_selection_tie_breaks_on_enumeration_order() -> None:
    # NoopStrategy → every cell ties at 0.0 → IS-best is the first cell each window.
    grid = ParamGrid(axes=(("x", (1, 2, 3)),))
    bars = _up_series(10)
    result = run_walk_forward(grid, _build_noop, bars, train=4, test=2, step=2)
    assert len(result.windows) == 3
    for wr in result.windows:
        assert wr.is_best_params == {"x": 1}


def test_scenario_5_no_windows_mean_is_zero() -> None:
    grid = ParamGrid(axes=(("qty", ("10.00",)),))
    bars = _up_series(3)  # too short for train=4
    result = run_walk_forward(grid, _build_oneshot, bars, train=4, test=2, step=2)
    assert result.windows == ()
    assert result.mean_oos_score == 0.0


# --- Scenario 6 — regime labels from the trailing trend -------------------- #
def test_scenario_6_classify_regimes_trailing_trend() -> None:
    # close: 100, 110, 130 (rise), 100, 80 (fall), 80, 80 (flat); window=2.
    closes = ["100", "110", "130", "100", "80", "80", "80"]
    bars = [bar(_day(i), "1", "9999", "1", c) for i, c in enumerate(closes)]
    labels = classify_regimes(
        bars, window=2, up_threshold=Decimal("0.05"), down_threshold=Decimal("-0.05")
    )
    # i0: trend 0 → SIDEWAYS
    # i1 (<window): 110/100-1 = +0.10 → UP
    # i2: 130/100-1 = +0.30 → UP
    # i3: 100/130-1 ≈ -0.23 → DOWN
    # i4: 80/100-1 = -0.20 → DOWN
    # i5: 80/130-1 ≈ -0.38 → DOWN
    # i6: 80/80-1 = 0 → SIDEWAYS
    assert labels == [
        Regime.SIDEWAYS,
        Regime.UP,
        Regime.UP,
        Regime.DOWN,
        Regime.DOWN,
        Regime.DOWN,
        Regime.SIDEWAYS,
    ]


def test_scenario_6_strict_threshold_boundaries() -> None:
    # Exactly +5% over window=1 → SIDEWAYS (strict >); exactly -5% → SIDEWAYS.
    up = [bar(_day(i), "1", "9999", "1", c) for i, c in enumerate(["100", "105"])]
    down = [bar(_day(i), "1", "9999", "1", c) for i, c in enumerate(["100", "95"])]
    assert classify_regimes(up, window=1)[1] is Regime.SIDEWAYS
    assert classify_regimes(down, window=1)[1] is Regime.SIDEWAYS


def test_scenario_6_zero_base_close_is_sideways() -> None:
    # A zero trend-base close cannot form a ratio → SIDEWAYS (guarded, no divide).
    bars = [bar(_day(i), "1", "9999", "1", c) for i, c in enumerate(["0", "100"])]
    assert classify_regimes(bars, window=1) == [Regime.SIDEWAYS, Regime.SIDEWAYS]


# --- Scenario 7 — segment_regimes partitions [0, n) ------------------------ #
def test_scenario_7_segment_regimes_contiguous_spans() -> None:
    # Labels [SIDEWAYS, UP, UP, DOWN, DOWN, SIDEWAYS] via window=1 thresholds.
    # closes engineered so trends land on the right labels for window=1.
    closes = ["100", "100", "200", "150", "100", "100"]
    bars = [bar(_day(i), "1", "9999", "1", c) for i, c in enumerate(closes)]
    labels = classify_regimes(bars, window=1)
    assert labels == [
        Regime.SIDEWAYS,
        Regime.SIDEWAYS,
        Regime.UP,
        Regime.DOWN,
        Regime.DOWN,
        Regime.SIDEWAYS,
    ]
    spans = segment_regimes(bars, window=1)
    assert spans == [
        RegimeSpan(regime=Regime.SIDEWAYS, start=0, end=2, start_date=_day(0), end_date=_day(1)),
        RegimeSpan(regime=Regime.UP, start=2, end=3, start_date=_day(2), end_date=_day(2)),
        RegimeSpan(regime=Regime.DOWN, start=3, end=5, start_date=_day(3), end_date=_day(4)),
        RegimeSpan(regime=Regime.SIDEWAYS, start=5, end=6, start_date=_day(5), end_date=_day(5)),
    ]
    # Spans cover every bar exactly once.
    assert sum(s.end - s.start for s in spans) == len(bars)


def test_scenario_7_empty_series_no_spans() -> None:
    assert segment_regimes([], window=2) == []
    assert classify_regimes([], window=2) == []


# --- Scenario 8 — per-regime metrics --------------------------------------- #
def test_scenario_8_regime_metrics_slice_and_reuse_compute_metrics() -> None:
    # A curve that rises then falls; UP span CAGR > 0, DOWN span CAGR <= 0.
    closes = ["100", "150", "200", "150", "100"]  # up, up, down, down
    bars = [bar(_day(i * 120), "1", "9999", "1", c) for i, c in enumerate(closes)]
    strat = OneShotBuyStrategy(qty="100.00", limit="100.00", cadence="daily", ns="vr")
    result = run_scenario(
        BacktestScenario(
            strategy=strat,
            cfg=None,  # type: ignore[arg-type]
            start_state=_vr_start_state("100000.00"),
        ),
        bars,
    )
    spans = segment_regimes(bars, window=1)
    per = regime_metrics(result, spans)
    assert Regime.UP in per
    assert Regime.DOWN in per
    assert per[Regime.UP].cagr > 0.0
    assert per[Regime.DOWN].cagr <= 0.0


def test_scenario_8_degenerate_span_yields_empty_metrics() -> None:
    bars = [bar(_day(0), "1", "9999", "1", "100")]
    strat = NoopStrategy(cadence="daily", ns="vr")
    result = run_scenario(
        BacktestScenario(
            strategy=strat,
            cfg=None,  # type: ignore[arg-type]
            start_state=_vr_start_state("1000.00"),
        ),
        bars,
    )
    span = RegimeSpan(regime=Regime.SIDEWAYS, start=0, end=1, start_date=_day(0), end_date=_day(0))
    per = regime_metrics(result, [span])
    assert per[Regime.SIDEWAYS].cagr == 0.0
    assert per[Regime.SIDEWAYS].mdd == 0.0
    assert per[Regime.SIDEWAYS].volatility == 0.0


# --- Scenario 9 — robust plateau ------------------------------------------- #
def test_scenario_9_find_plateau_robust_optimum_and_set() -> None:
    grid = ParamGrid(axes=(("g", (5, 10, 15, 20)),))
    scores = [0.10, 0.30, 0.32, 0.05]
    cells = tuple(_cell(g, s) for g, s in zip((5, 10, 15, 20), scores, strict=True))
    report = find_plateau(grid, cells, tolerance=0.10)
    assert isinstance(report, PlateauReport)
    # Robust optimum is g=10 (robust score 0.240), NOT the lone max g=15.
    assert report.robust_optimum == {"g": 10}
    assert report.robust_score == pytest.approx(0.240)
    # Plateau threshold 0.32 - 0.032 = 0.288 -> {g=10, g=15}.
    assert report.plateau == ({"g": 10}, {"g": 15})


def test_scenario_9_single_cell_is_optimum_and_sole_plateau() -> None:
    grid = ParamGrid(axes=(("g", (5,)),))
    cells = (_cell(5, 0.42),)
    report = find_plateau(grid, cells, tolerance=0.10)
    assert report.robust_optimum == {"g": 5}
    assert report.robust_score == pytest.approx(0.42)
    assert report.plateau == ({"g": 5},)


def test_scenario_9_plateau_2d_grid() -> None:
    # 2x2 lattice exercises the multi-axis von-Neumann neighbour clipping.
    grid = ParamGrid(axes=(("a", (1, 2)), ("b", (10, 20))))
    cells = tuple(
        GridCell(params=p, metrics=_cell(0, s).metrics, score=s)
        for p, s in zip(grid.combinations(), (0.10, 0.20, 0.30, 0.05), strict=True)
    )
    report = find_plateau(grid, cells, tolerance=0.10)
    # best single 0.30 → threshold 0.27 → plateau {a:2,b:10}.
    assert report.plateau == ({"a": 2, "b": 10},)


# --- Scenario 10 — override_config ----------------------------------------- #
def test_scenario_10_override_config_returns_new_without_mutating(tmp_path: Path) -> None:
    base = _config(tmp_path)
    assert base.strategies.vr.g == 10
    new = override_config(base, strategies__vr__g=20)
    assert new.strategies.vr.g == 20
    # Base is untouched.
    assert base.strategies.vr.g == 10
    # Returned config is still frozen.
    assert new.model_config["frozen"] is True
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen-instance error
        new.strategies.vr.g = 99  # type: ignore[misc]


def test_scenario_10_override_config_rejects_bad_path(tmp_path: Path) -> None:
    base = _config(tmp_path)
    with pytest.raises((KeyError, ValueError, AttributeError)):
        override_config(base, strategies__vr__nonexistent=1)


def test_scenario_10_override_config_rejects_unknown_deep_path(tmp_path: Path) -> None:
    # An entirely unknown top-level path auto-vivifies nested dicts, which the
    # ``extra="forbid"`` schema then rejects on re-validation.
    base = _config(tmp_path)
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError (extra forbidden)
        override_config(base, bogus__sub__leaf=1)
    assert base.strategies.vr.g == 10


def test_scenario_10_override_config_rejects_invalid_value(tmp_path: Path) -> None:
    base = _config(tmp_path)
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on float money
        override_config(base, execution__max_position_pct=0.5)


def test_scenario_10_override_config_multiple_keys(tmp_path: Path) -> None:
    base = _config(tmp_path)
    new = override_config(base, strategies__vr__g=15, strategies__mab__n_splits=20)
    assert new.strategies.vr.g == 15
    assert new.strategies.mab.n_splits == 20
    assert base.strategies.vr.g == 10
    assert base.strategies.mab.n_splits == 40


# --- Scenario 11 — determinism + decoupling -------------------------------- #
def test_scenario_11_entry_points_are_deterministic() -> None:
    grid = ParamGrid(axes=(("qty", ("10.00", "20.00")),))
    bars = _up_series(10)
    assert run_grid(grid, _build_oneshot, bars) == run_grid(grid, _build_oneshot, bars)
    wf1 = run_walk_forward(grid, _build_oneshot, bars, train=4, test=2, step=2)
    wf2 = run_walk_forward(grid, _build_oneshot, bars, train=4, test=2, step=2)
    assert wf1 == wf2
    assert classify_regimes(bars, window=2) == classify_regimes(bars, window=2)
    cells = tuple(_cell(g, s) for g, s in zip((5, 10), (0.1, 0.2), strict=True))
    g2 = ParamGrid(axes=(("g", (5, 10)),))
    assert find_plateau(g2, cells) == find_plateau(g2, cells)


def test_scenario_11_inputs_not_mutated(tmp_path: Path) -> None:
    grid = ParamGrid(axes=(("qty", ("10.00", "20.00")),))
    bars = _up_series(5)
    bars_before = copy.deepcopy(bars)
    state = _vr_start_state("100000.00")
    state_before = copy.deepcopy(state.data)
    cfg = _config(tmp_path)
    cfg_dump_before = cfg.model_dump()

    def build(params: Mapping[str, Any]) -> BacktestScenario:
        return BacktestScenario(
            strategy=OneShotBuyStrategy(qty=str(params["qty"]), limit="100.00"),
            cfg=cfg,
            start_state=state,
        )

    run_grid(grid, build, bars)
    override_config(cfg, strategies__vr__g=20)

    assert bars == bars_before
    assert state.data == state_before
    assert cfg.model_dump() == cfg_dump_before


def test_scenario_11_sweep_core_imports_no_config_field_names() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "src" / "ballast" / "backtest" / "sweep.py"
    ).read_text(encoding="utf-8")
    for field_name in _config_field_names():
        assert field_name not in source, f"sweep.py must not reference Config field {field_name!r}"


def _config_field_names() -> set[str]:
    """Collect every field name across the nested Config pydantic models."""
    from ballast.core import config as config_module

    names: set[str] = set()
    for attr in vars(config_module).values():
        fields = getattr(attr, "model_fields", None)
        if isinstance(fields, dict):
            names.update(fields)
    # Drop names too generic to be Config-specific markers (single/two-char names
    # like ``r`` / ``g`` / ``vr`` are common substrings; ``version`` / ``broker``
    # are ordinary English words). Keep the distinctive, compound field names whose
    # presence would genuinely betray Config introspection.
    generic = {"version", "broker"}
    return {n for n in names if len(n) >= 3 and n not in generic}


# --- Quality-Gate: hypothesis invariants ----------------------------------- #
@settings(max_examples=50, deadline=None)
@given(
    a_len=st.integers(min_value=0, max_value=3),
    b_len=st.integers(min_value=1, max_value=3),
)
def test_invariant_grid_size_equals_product(a_len: int, b_len: int) -> None:
    axes: tuple[tuple[str, tuple[Any, ...]], ...]
    if a_len == 0:
        axes = ()
        expected = 1
    else:
        axes = (("a", tuple(range(a_len))), ("b", tuple(range(b_len))))
        expected = a_len * b_len
    grid = ParamGrid(axes=axes)
    bars = _up_series(3)
    result = run_grid(grid, _build_noop, bars)
    assert len(result.cells) == expected
    assert len(grid.combinations()) == expected


@settings(max_examples=100, deadline=None)
@given(
    n=st.integers(min_value=0, max_value=30),
    train=st.integers(min_value=1, max_value=8),
    test=st.integers(min_value=1, max_value=8),
    step=st.integers(min_value=1, max_value=8),
    anchored=st.booleans(),
)
def test_invariant_window_coverage(
    n: int, train: int, test: int, step: int, anchored: bool
) -> None:
    windows = walk_forward_windows(n, train=train, test=test, step=step, anchored=anchored)
    for w in windows:
        # train/test ranges are non-empty and ordered.
        assert w.train[0] < w.train[1]
        assert w.test[0] < w.test[1]
        assert w.test[0] == w.train[1]
        # No partial trailing test window: every test end is within n.
        assert w.test[1] <= n


@settings(max_examples=100, deadline=None)
@given(
    closes=st.lists(st.integers(min_value=1, max_value=1000), min_size=0, max_size=20),
    window=st.integers(min_value=1, max_value=5),
)
def test_invariant_regime_partition(closes: list[int], window: int) -> None:
    bars = [bar(_day(i), "1", "9999", "1", str(c)) for i, c in enumerate(closes)]
    spans = segment_regimes(bars, window=window)
    if not bars:
        assert spans == []
        return
    # Spans partition [0, n): contiguous, no gaps, no overlaps, full cover.
    assert spans[0].start == 0
    assert spans[-1].end == len(bars)
    for prev, nxt in pairwise(spans):
        assert prev.end == nxt.start
    assert sum(s.end - s.start for s in spans) == len(bars)


@settings(max_examples=30, deadline=None)
@given(qtys=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=4, unique=True))
def test_invariant_determinism_and_no_mutation(qtys: list[int]) -> None:
    grid = ParamGrid(axes=(("qty", tuple(f"{q}.00" for q in qtys)),))
    bars = _up_series(6)
    bars_before = copy.deepcopy(bars)
    r1 = run_grid(grid, _build_oneshot, bars)
    r2 = run_grid(grid, _build_oneshot, bars)
    assert r1 == r2
    assert bars == bars_before


def test_default_score_returns_cagr() -> None:
    metrics = _cell(0, 0.123).metrics
    assert default_score(metrics) == 0.123


def test_run_scenario_passes_costs_and_cycle_length() -> None:
    bars = _up_series(3)
    scenario = BacktestScenario(
        strategy=OneShotBuyStrategy(qty="10.00", limit="100.00", cadence="cycle", ns="vr"),
        cfg=None,  # type: ignore[arg-type]
        start_state=_vr_start_state("100000.00"),
        cycle_length=2,
    )
    result = run_scenario(scenario, bars)
    assert len(result.equity_curve) == 3


def _unused_sequence_type_check(_: Sequence[OHLCBar]) -> None:  # pragma: no cover
    return None
