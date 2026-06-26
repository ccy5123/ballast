# Implementation Plan — SPEC-BACKTEST-002 (Parameter Sweep + Walk-Forward + Regime Split + Robust Plateau)

`@SPEC:SPEC-BACKTEST-002`

This plan covers a single orchestration module — `src/ballast/backtest/sweep.py` — that sits on top
of the SPEC-BACKTEST-001 engine. It adds NO new financial mechanic: it runs `run_backtest` +
`compute_metrics` across a parameter grid, slices the series into walk-forward train/test windows and
into market regimes, and reduces the grid to a robust plateau. The concrete data fetcher, a CLI
`sweep` subcommand, parallel execution, and Pareto/multi-objective selection remain out of scope.

## Module Layout

One new file alongside the existing backtest layer (same layer, so pandas/numpy are allowed here):

```
src/ballast/backtest/
├── ... (BACKTEST-001 modules, unchanged)
└── sweep.py        # ParamGrid, BacktestScenario, run_grid, walk_forward_windows, run_walk_forward,
                    # classify_regimes, segment_regimes, regime_metrics, find_plateau,
                    # override_config, default_score, run_scenario  (@CODE:SPEC-BACKTEST-002)
```

Dependency direction (no cycles; sweep depends on the BACKTEST-001 surface + the core, never reverse):

```
sweep.py ─► (backtest) engine.run_backtest, metrics.compute_metrics, metrics.Metrics/Basis,
                       types.BacktestResult/EquityPoint/OHLCBar, costs.CostModel
         ─► (core)     config.Config, models.State, strategy.Strategy
         ─► numpy      # regime trend + robust-score ratio math (float, isolated)
```

The sweep core imports NO `Config` field names — parameter application lives only inside the injected
`build` callback and the optional `override_config` helper. `sweep.py` is added to the
`ballast.backtest` package `__all__`.

## Decoupling-by-Injection (A2 / A3 — the central design choice)

The sweep never introspects `Config`. Two callbacks carry all coupling:

- `build: Callable[[Mapping[str, Any]], BacktestScenario]` — maps one parameter combination to a
  runnable scenario. Only `build` knows which param name maps to which `Config` / `CostModel` /
  strategy field. Tests supply a `build` that parameterizes a stub strategy and/or uses
  `override_config`.
- `score: Callable[[Metrics], float]` — collapses a `Metrics` bundle to one comparable `float`
  (higher better). Default `default_score(m) = m.cagr`.

This keeps `sweep.py` a pure orchestrator: swap strategies/parameters/objectives without touching it.

## Function / Type Signatures (target)

```python
from __future__ import annotations
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from itertools import product
from typing import Any, Literal

from ballast.backtest.costs import CostModel
from ballast.backtest.engine import run_backtest
from ballast.backtest.metrics import Basis, Metrics, compute_metrics
from ballast.backtest.types import BacktestResult, EquityPoint, OHLCBar
from ballast.core.config import Config
from ballast.core.models import State
from ballast.core.strategy import Strategy


@dataclass(frozen=True, slots=True)
class ParamGrid:
    axes: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    def combinations(self) -> list[dict[str, Any]]: ...   # itertools.product, last axis fastest


@dataclass(frozen=True, slots=True)
class BacktestScenario:
    strategy: Strategy
    cfg: Config
    start_state: State
    cycle_length: Literal["monthly"] | int = "monthly"
    costs: CostModel = field(default_factory=CostModel)
    index: Sequence[tuple[date, Decimal]] | None = None


@dataclass(frozen=True, slots=True)
class GridCell:
    params: Mapping[str, Any]
    metrics: Metrics
    score: float


@dataclass(frozen=True, slots=True)
class Window:
    train: tuple[int, int]
    test: tuple[int, int]


class Regime(Enum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


def default_score(metrics: Metrics) -> float: ...                # m.cagr
def run_scenario(scenario: BacktestScenario, bars: Sequence[OHLCBar]) -> BacktestResult: ...
def run_grid(grid, build, bars, *, score=default_score, basis="KRW", tolerance=0.10) -> SweepResult: ...
def walk_forward_windows(n, *, train, test, step=None, anchored=False) -> list[Window]: ...
def run_walk_forward(grid, build, bars, *, score=default_score, train, test,
                     step=None, anchored=False, basis="KRW") -> WalkForwardResult: ...
def classify_regimes(bars, *, window=5,
                     up_threshold=Decimal("0.05"), down_threshold=Decimal("-0.05")) -> list[Regime]: ...
def segment_regimes(bars, *, window=5, up_threshold=..., down_threshold=...) -> list[RegimeSpan]: ...
def regime_metrics(result, spans, *, basis="KRW") -> dict[Regime, Metrics]: ...
def find_plateau(grid, cells, *, tolerance=0.10) -> PlateauReport: ...
def override_config(base: Config, **dotted: Any) -> Config: ...  # e.g. strategies__vr__g=20
```

## Float-vs-Decimal Boundary Policy

The sweep performs NO money arithmetic. Every `Decimal` (final assets, tax drag, …) passes through
untouched from the `BacktestResult` / `Metrics` it consumes. The sweep's OWN math — grid scores,
robust scores, the regime trailing-trend ratio, the mean OOS score — is `float` and never writes back
onto any `Decimal` ledger (that ledger is sealed inside BACKTEST-001). The only `Decimal` the sweep
computes with directly is the regime trend (`close[i]/close[i-window] - 1`), kept in `Decimal` for an
exact threshold comparison, then the label is a plain branch — no money is produced.

## Algorithms (normative — match the §Fixed Definitions in spec.md)

1. **Grid enumeration** — `itertools.product(*[values for _, values in axes])`, zipped with axis
   names into dicts; last axis varies fastest; empty axes → `[{}]`.
2. **`run_grid`** — for each combination: `scenario = build(combo)`; `result =
run_scenario(scenario, bars)`; `metrics = compute_metrics(result, basis=basis)`; `s =
score(metrics)`; collect `GridCell`. `best` = max by score with first-wins tie-break (iterate in
   order, replace only on strictly greater). `plateau = find_plateau(grid, cells, tolerance)`.
3. **`walk_forward_windows`** — `step = step or test`; iterate `w = 0, 1, …`; rolling `s = w*step`,
   train `[s, s+train)`, test `[s+train, s+train+test)`; anchored train `[0, s+train)`. Emit while
   `test_end ≤ n`; stop at the first window that would overflow.
4. **`run_walk_forward`** — for each `Window`: `is_sweep = run_grid(grid, build, bars[train_slice],
…)`; `is_best = is_sweep.best`; `oos_result = run_scenario(build(is_best.params),
bars[test_slice])`; `oos_metrics = compute_metrics(...)`; `oos_score = score(oos_metrics)`. Record
   a `WalkForwardWindowResult`. `mean_oos_score = mean(oos_scores)` or `0.0` if no windows.
5. **`classify_regimes`** — `trend[0] = 0`; for `0 < i < window`, `trend = close[i]/close[0] - 1`;
   for `i ≥ window`, `trend = close[i]/close[i-window] - 1`; label by strict `>` / `<` thresholds,
   else `SIDEWAYS`. (All `Decimal` to make the threshold comparison exact.)
6. **`segment_regimes`** — single pass collapsing equal consecutive labels into `RegimeSpan(label,
start, end, start_date, end_date)`; the spans partition `[0, n)`.
7. **`regime_metrics`** — group spans by label; for each label, for each span slice
   `result.equity_curve[start:end]` and the `fills`/`trades` whose `date ∈ [start_date, end_date]`,
   build a sub-`BacktestResult` (parent `start_capital_usd`), `compute_metrics`; aggregate spans of
   the same label by concatenating their curve slices in order before the metrics call. A <2-point
   slice → the empty-metrics bundle.
8. **`find_plateau`** — index cells by their multi-index on the lattice of axis lengths; neighbours =
   ±1 on exactly one axis (clipped); `robust_score = mean(score(cell) + neighbour scores)`; robust
   optimum = max robust score (first-wins); `best_single = max(score)`; plateau = cells with
   `score ≥ best_single − |best_single|×tolerance`. Single cell → that cell is both.
9. **`override_config`** — split each `key` on `__` into a path; deep-copy via pydantic
   `model_copy(update=...)` at each level so the result is a NEW frozen `Config` and `base` is
   untouched; re-validate (raise on unknown path / invalid value).

## Task Decomposition

Ordered by dependency; priority labels only (no time estimates).

### Primary Goal — types + grid (`ParamGrid`, `run_grid`, `default_score`, `run_scenario`) [High]

1. Define the immutable sweep types and `Regime` enum.
2. `ParamGrid.combinations()` (Cartesian, last axis fastest, empty → `[{}]`).
3. `run_scenario` (thin `run_backtest` wrapper) + `default_score` (CAGR).
4. `run_grid` → `SweepResult` (cells in order, best with first-wins tie-break, plateau attached).

### Secondary Goal — walk-forward (`walk_forward_windows`, `run_walk_forward`) [High]

5. `walk_forward_windows` rolling + anchored, no partial trailing test window.
6. `run_walk_forward`: per-window IS-best → OOS evaluation; mean OOS score; empty when no windows.

### Tertiary Goal — regimes (`classify_regimes`, `segment_regimes`, `regime_metrics`) [High]

7. `classify_regimes` trailing-trend labels (strict thresholds, `Decimal` compare).
8. `segment_regimes` contiguous spans partitioning `[0, n)`.
9. `regime_metrics` per-label metrics from equity-curve slices, reusing `compute_metrics`.

### Final Goal — plateau + override helper (`find_plateau`, `override_config`) [High]

10. `find_plateau`: von-Neumann robust score → robust optimum; plateau set within tolerance.
11. `override_config`: dotted-path overrides via pydantic copy; never mutate `base`.
12. Export the public surface via `ballast.backtest.__all__`.

## Technical Approach

- **Pure orchestration, no new mechanics:** every fill/cost/tax decision stays inside BACKTEST-001;
  the sweep only chooses inputs and reads outputs. This lifts BACKTEST-001's determinism and
  no-look-ahead guarantees to the sweep for free.
- **Injection over introspection:** `build` and `score` are the only coupling points; `sweep.py`
  references no `Config` field, so sweeping a new knob needs only a new `build`, not a sweep edit.
- **Deterministic everywhere:** Cartesian order is `itertools.product`; ties resolve to enumeration
  order (first-wins); windows/regimes/plateau are index math; no RNG, no wall clock, no set ordering.
- **Robust plateau over single max:** the deliverable is a parameter region that stays good under
  ±1-step perturbation (mean-neighbour robust score), directly serving product.md's "강건한 고원".
- **Immutability:** all result types are `frozen`/`slots`; `override_config` returns a NEW `Config`;
  `bars`/`Config`/`State` are never mutated.

## Risk Analysis

| Risk                                                         | Impact                                             | Mitigation                                                                                                             |
| ------------------------------------------------------------ | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Look-ahead via in-sample leakage** (test slice seen in IS) | Walk-forward overstates OOS generalization         | Windows are disjoint index slices; the IS sweep runs ONLY on `bars[train]`, OOS ONLY on `bars[test]`; test the slices  |
| **Partial trailing test window** silently scored             | Misleading OOS coverage                            | A window is emitted iff its full `test` fits in `n`; a `hypothesis` invariant asserts every `test` end `≤ n`           |
| **Plateau = lone max** (overfit point chosen)                | Fragile parameters shipped                         | Robust score = mean over cell + von-Neumann neighbours; the worked `g∈[5,10,15,20]` example pins robust optimum `g=10` |
| **Non-determinism** (dict/set ordering, tie ambiguity)       | Non-reproducible `SweepResult`                     | Ordered combos; first-wins tie-break; a test runs each entry point twice and asserts identical results                 |
| **`Config` mutation** in `override_config`                   | Caller's base config silently changes              | `model_copy(update=...)` returns a NEW frozen `Config`; assert `base` unchanged; re-validate on override               |
| **Coupling creep** (`sweep.py` learns `Config` fields)       | The decoupling guarantee erodes                    | All param→config mapping is in `build` / `override_config`; a test asserts `sweep.py` imports no `Config` field name   |
| **Money math leaking onto the float score path**             | Precision loss / Decimal corruption                | The sweep does NO money arithmetic; `Decimal` passes through from `Metrics`; only ratios/scores are `float`            |
| **Regime span gaps/overlaps**                                | Bars double-counted or dropped in per-regime stats | `segment_regimes` is a single contiguous pass; a `hypothesis` invariant asserts spans partition `[0, n)` exactly       |
| **Degenerate regime span (<2 points)** raising in metrics    | Crash on short spans                               | `regime_metrics` returns the empty-metrics bundle for a <2-point slice (no exception); explicit test                   |

## Test Approach

- Framework: `pytest` + `pytest-cov`; property-based invariants with `hypothesis`.
- Location: `tests/unit/backtest/test_sweep.py` (reuse `conftest.py` stubs + `bar(...)`).
- **Deterministic unit tests (hand-computed expectations):**
  - grid Cartesian order + empty-grid single cell + axis-length product count,
  - `run_grid` scores each cell, picks best with first-wins tie-break, is reproducible,
  - `walk_forward_windows` rolling (3 windows for `n=10,train=4,test=2,step=2`), anchored, and the
    `n=9` no-partial case,
  - `run_walk_forward` IS-best → OOS, mean OOS score, empty-when-too-short,
  - `classify_regimes` labels incl. strict-threshold boundaries; `segment_regimes` contiguous spans,
  - `regime_metrics` per-label metrics + degenerate <2-point span → empty metrics,
  - `find_plateau` worked example: robust optimum `g=10`, plateau `{g=10, g=15}`; single-cell case,
  - `override_config` returns a new config, leaves `base` unchanged, raises on bad path/value.
- **Property-based (`hypothesis`) invariants:**
  - **grid size** — `len(cells)` equals the product of axis lengths (`1` for empty),
  - **window coverage** — every emitted `test` end `≤ n`; `train`/`test` ranges non-empty, ordered,
  - **regime partition** — `segment_regimes` spans partition `[0, n)` (no gaps/overlaps),
  - **determinism** — identical inputs → identical results across all entry points,
  - **no mutation** — `bars` / `Config` / `State` unchanged after any sweep call.
- Decoupling check: a test asserts `sweep.py` source references no `Config` field name and that the
  core `src/ballast/core/` is untouched.

## Quality Gates (must pass before merge)

- `ruff check .` → 0 errors; `ruff format --check .` → clean.
- `mypy --strict src` → 0 errors.
- `pytest --cov=src/ballast --cov-report=term-missing` → coverage ≥ 85% (keep the layer at 100%).
- The sweep adds no new fill/cost/tax mechanic and no `Config` introspection; no money math on the
  float score path; no mutation of inputs.
- Note: `pandas` / `numpy` ARE allowed in `src/ballast/backtest/` (forbidden only in
  `src/ballast/core/`).

## Traceability

- `@SPEC:SPEC-BACKTEST-002` → `@TEST:SPEC-BACKTEST-002` (`tests/unit/backtest/test_sweep.py`) →
  `@CODE:SPEC-BACKTEST-002` (`src/ballast/backtest/sweep.py`) → `@DOC:SPEC-BACKTEST-002`.
- Depends on `@SPEC:SPEC-BACKTEST-001`, `@SPEC:SPEC-CORE-001`, `@SPEC:SPEC-VR-001`,
  `@SPEC:SPEC-MAB-001`.
