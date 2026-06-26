---
id: SPEC-BACKTEST-002
version: 0.1.0
status: draft
created: 2026-06-26
updated: 2026-06-26
author: ccy5123
priority: high
lifecycle_level: spec-first
---

## HISTORY

### v0.1.0 (2026-06-26)

- Initial draft. Defines the **parameter-sweep + walk-forward + regime-split + robust-plateau**
  layer for ballast, the P0 deliverable explicitly deferred from SPEC-BACKTEST-001
  (see that SPEC's §Out of Scope). Module home: `src/ballast/backtest/sweep.py`.
- This SPEC adds NO new financial mechanics: it ORCHESTRATES the existing SPEC-BACKTEST-001
  `run_backtest` + `compute_metrics` over a grid of parameter combinations, slices the bar series
  into walk-forward train/test windows and into market regimes, and reduces the grid to a **robust
  plateau** rather than a single overfit optimum (product.md: "단일 최적값이 아닌 강건한 고원").
- Five EARS requirement modules (`grid`, `walk-forward`, `regime`, `plateau`, `determinism`) — one
  per capability — covering all five EARS types. Adds sweep-only types (`ParamGrid`,
  `BacktestScenario`, `GridCell`, `SweepResult`, `Window`, `WalkForwardResult`, `Regime`,
  `RegimeSpan`, `PlateauReport`) under `src/ballast/backtest/sweep.py`. REUSES the
  SPEC-BACKTEST-001 `BacktestResult` / `Metrics` / `run_backtest` / `compute_metrics` and the
  CORE-001 `Config` / `State` / `Strategy`; it redefines none of them.
- **Decoupling by injection.** The sweep core never reaches into `Config`'s internal structure. A
  caller-supplied `build(params) -> BacktestScenario` maps each parameter combination to a runnable
  scenario, and a caller-supplied `score(metrics) -> float` collapses a `Metrics` bundle to one
  comparable number (default: CAGR). A convenience `override_config(base, **dotted)` helper applies
  dotted-path overrides to a frozen pydantic `Config` for the common "sweep the strategy knobs" case.
- Per the constitution, `pandas` / `numpy` are allowed in THIS backtest layer (forbidden only in
  `src/ballast/core/`). This SPEC's own ratio/score math is `float` (it never touches the `Decimal`
  cash ledger, which lives entirely inside the BACKTEST-001 result it consumes).

---

# SPEC-BACKTEST-002 — Parameter Sweep + Walk-Forward + Regime Split + Robust Plateau

`@SPEC:SPEC-BACKTEST-002`

## Environment

- Language: Python `>=3.11` (single language).
- Module home: `src/ballast/backtest/sweep.py`. This is an **orchestration** module on top of the
  BACKTEST-001 engine; it is the place pandas/numpy may be used (NOT the pure core).
- Dependencies: SPEC-BACKTEST-001 (`run_backtest`, `BacktestResult`, `compute_metrics`, `Metrics`,
  `OHLCBar`, `EquityPoint`, `CostModel`) and SPEC-CORE-001 (`Config`, `State`, `Strategy`); all
  consumed, none redefined. `numpy` for the regime/plateau ratio math; standard library otherwise.
- Money policy: this layer performs NO money arithmetic of its own. Every `Decimal` quantity flows
  through unchanged from the `BacktestResult` / `Metrics` it consumes. The sweep's own comparison
  math (scores, robust scores, regime trend) is `float` and never feeds back onto any `Decimal`
  ledger (that ledger is sealed inside BACKTEST-001).
- Determinism: given the same `bars`, `ParamGrid`, `build`, and `score`, the sweep produces a
  byte-identical `SweepResult` / `WalkForwardResult` (the grid order, tie-breaks, window slicing,
  regime labels, and plateau selection are all deterministic). No wall clock, no network, no
  randomness.
- Tests: `pytest`, `pytest-cov`, `hypothesis` (property-based for grid-size, window-coverage, and
  plateau invariants).
- Lint/format/type: `ruff` (lint + format), `mypy --strict`.

## Assumptions

- A1: The sweep is a **pure orchestrator**. It calls `run_backtest` (BACKTEST-001) once per
  (parameter combination × bar slice) and `compute_metrics` on each result; it adds no new fill,
  cost, or tax mechanics. Backtest determinism (BACKTEST-001 A-invariants) therefore lifts directly
  to sweep determinism.
- A2: **Parameters are injected, not introspected.** The sweep core is handed a
  `build: Callable[[Mapping[str, Any]], BacktestScenario]` that turns one combination into a runnable
  scenario (strategy + `Config` + `start_state` + `cycle_length` + `costs` + optional `index`). The
  sweep never imports `Config` field names; only `build` knows them. Tests supply a `build` that
  parameterizes a conforming stub strategy and/or `override_config`.
- A3: **Scoring is injected.** A `score: Callable[[Metrics], float]` reduces a `Metrics` bundle to
  one comparable `float` (higher is better). The default is `metrics.cagr`. A robust caller may pass
  a Calmar-like `lambda m: m.cagr / m.mdd if m.mdd else m.cagr`.
- A4: **Walk-forward windows are bar-index slices**, not calendar slices — `train` / `test` / `step`
  are bar counts. Rolling windows (default) advance the train start by `step`; anchored windows keep
  the train start at bar 0 and grow the train span. A window is emitted only if its full `test` slice
  fits within the series (no partial trailing test window — silently dropping a partial would
  misreport coverage, so it is simply not emitted and the emitted-vs-total bar coverage is derivable).
- A5: **Regimes are a deterministic function of the close series**, computed from a trailing trend
  over a `window` of bars and two thresholds (`up_threshold` / `down_threshold`). They are a
  reporting lens, not a trading input — they never alter any order or fill. Per-regime metrics are
  computed by slicing the equity curve into the regime's contiguous spans and reusing
  `compute_metrics` on the sub-result.
- A6: **The robust plateau is the deliverable, not the single max.** The grid is an N-dimensional
  lattice (one axis per `ParamGrid` axis, in declaration order, last axis varying fastest). Each
  cell's **robust score** is the mean `score` over the cell and its von-Neumann neighbours (cells
  differing by ±1 index on exactly one axis). The robust optimum is the cell maximizing the robust
  score; the plateau set is every cell whose own `score` is within a relative `tolerance` of the
  best single score.
- A7: This SPEC depends on SPEC-BACKTEST-001 being implemented (it calls `run_backtest` /
  `compute_metrics` directly) and, transitively, on SPEC-CORE-001 / VR-001 / MAB-001 for the
  strategies a realistic `build` will construct. Engine-only sweep tests use the same trivial
  conforming stub strategies the BACKTEST-001 suite already provides.

## Fixed Definitions (normative)

These definitions are **fixed** and MUST be reflected in code (docstrings) and tests.

- **Grid enumeration order** — `ParamGrid` carries ordered axes `((name, values), …)`. The
  combinations are the Cartesian product in declaration order with the **last axis varying fastest**
  (identical to `itertools.product` over the axes' value lists). For axes
  `a ∈ [1, 2], b ∈ [10, 20]` the order is exactly
  `{a:1,b:10}, {a:1,b:20}, {a:2,b:10}, {a:2,b:20}`. The total cell count is the product of the axis
  lengths. An empty grid (no axes) yields exactly one empty combination `{}`.

- **Score and tie-breaks** — `score(metrics) -> float`; **higher is better**. When two cells tie on
  the comparison key, the **earlier cell in enumeration order wins** (stable, deterministic). The
  default score is `metrics.cagr`.

- **Walk-forward window** — for a series of `n` bars and bar counts `train`, `test`, `step`
  (`step` default `= test`):
  - rolling: window `w` has `train_slice = [s, s + train)` and `test_slice = [s + train, s + train +
test)` where `s = w * step`, for `w = 0, 1, …`;
  - anchored: `train_slice = [0, w*step + train)`, `test_slice = [w*step + train, w*step + train +
  test)`.
    A window is emitted **iff** `test_slice`'s end `≤ n`. Worked example (`n=10, train=4, test=2,
step=2`, rolling): windows are `(train [0,4), test [4,6))`, `(train [2,6), test [6,8))`,
    `(train [4,8), test [8,10))` — exactly **3** windows. Anchored on the same inputs:
    `(train [0,4), test [4,6))`, `(train [0,6), test [6,8))`, `(train [0,8), test [8,10))`.

- **Walk-forward selection** — for each window: run the full grid on `bars[train_slice]`, pick the
  in-sample (IS) best cell by `score` (tie-break = enumeration order), then run THAT cell's scenario
  on `bars[test_slice]` and record the out-of-sample (OOS) `Metrics` and `score`. The aggregate OOS
  score is the arithmetic mean of the per-window OOS scores (`0.0` when there are no windows).

- **Regime classification** — for close series `c[0..n-1]` and a trailing `window` (default `5`):
  the trend at bar `i` is `trend[i] = c[i] / c[i - window] - 1` for `i ≥ window`, and
  `c[i] / c[0] - 1` for `0 < i < window`; `trend[0] = 0`. Label: `UP` if
  `trend[i] > up_threshold`, `DOWN` if `trend[i] < down_threshold`, else `SIDEWAYS`
  (defaults `up_threshold = +0.05`, `down_threshold = -0.05`). `segment_regimes` collapses runs of
  the same label into contiguous `RegimeSpan`s (each a half-open `[start, end)` bar-index range with
  its `date` endpoints).

- **Per-regime metrics** — for a `BacktestResult` and a `RegimeSpan [start, end)`, slice the equity
  curve to `equity_curve[start:end]`, keep the `Fill`s/`Trade`s whose `date` lies in the span's date
  range, build a sub-`BacktestResult` (same `start_capital_usd` as the parent for ratio basis), and
  call `compute_metrics`. A span shorter than two curve points yields the empty-metrics bundle
  (CAGR/MDD/vol = 0) rather than raising.

- **Robust score & plateau** — lay the grid cells on the N-dim lattice of the axis lengths. A cell's
  **neighbours** are the cells whose multi-index differs by exactly ±1 on exactly one axis (von
  Neumann, no diagonals, clipped at the lattice edge). `robust_score(cell) = mean(score(cell) ∪
{score(nb) for nb in neighbours})`. The **robust optimum** is the cell with the highest robust
  score (tie-break = enumeration order). The **plateau set** is every cell whose own `score ≥
best_single_score × (1 − tolerance)` when `best_single_score > 0`, or `score ≥ best_single_score +
|best_single_score| × tolerance`… — to avoid sign ambiguity the normative rule is:
  `score ≥ best_single_score − |best_single_score| × tolerance` (default `tolerance = 0.10`).
  Worked 1-D example: axis `g ∈ [5,10,15,20]` with scores `[0.10, 0.30, 0.32, 0.05]` →
  robust scores (mean of cell+neighbours) `[0.200, 0.240, 0.2233…, 0.185]` → **robust optimum
  `g=10`**; with `tolerance=0.10` the plateau threshold is `0.32 − 0.032 = 0.288`, so the plateau set
  is `{g=10 (0.30), g=15 (0.32)}`.

## Requirements

The sweep layer MUST satisfy the following EARS requirements. All are tagged to
`@SPEC:SPEC-BACKTEST-002`. There are exactly five requirement modules (one per capability); together
they cover all five EARS types.

### REQ-BACKTEST-002-R1 — Parameter Grid + Sweep (Ubiquitous) — grid

`@SPEC:SPEC-BACKTEST-002` `REQ-BACKTEST-002-R1`

The system **shall always** enumerate `ParamGrid` combinations as the Cartesian product of its
ordered axes (last axis varying fastest, §Fixed Definitions), and for each combination call
`build(combination)` to obtain a `BacktestScenario`, run it through `run_backtest` over the supplied
`bars`, compute its `Metrics` via `compute_metrics`, and apply `score` — yielding one `GridCell`
(combination + metrics + score) per combination. `run_grid` **shall** return a `SweepResult` holding
the ordered `GridCell`s, the single best cell by `score` (tie-break = enumeration order), and the
plateau (REQ-BACKTEST-002-R4). An empty grid **shall** produce exactly one cell for the empty
combination `{}`.

### REQ-BACKTEST-002-R2 — Walk-Forward Windows + OOS Evaluation (Event-driven) — walk-forward

`@SPEC:SPEC-BACKTEST-002` `REQ-BACKTEST-002-R2`

The function `walk_forward_windows(n, *, train, test, step=test, anchored=False)` **shall** produce
the deterministic list of `Window`s defined in §Fixed Definitions, emitting a window **only when**
its full `test` slice fits within `n` (no partial trailing test window).

**When** `run_walk_forward(grid, build, bars, *, score, train, test, step, anchored)` is invoked, the
system **shall**, for each window, run the full grid on the train slice, select the in-sample best
cell by `score` (tie-break = enumeration order), evaluate THAT cell's scenario on the test slice, and
record the per-window in-sample best combination, its in-sample score, and its out-of-sample
`Metrics` + score. The result **shall** report the per-window records and the mean out-of-sample
score (`0.0` when no windows are produced).

### REQ-BACKTEST-002-R3 — Regime Classification + Per-Regime Metrics (State-driven) — regime

`@SPEC:SPEC-BACKTEST-002` `REQ-BACKTEST-002-R3`

**While** classifying a bar series, the system **shall** label each bar `UP` / `DOWN` / `SIDEWAYS`
from the trailing-`window` trend and the two thresholds (§Fixed Definitions), `segment_regimes`
**shall** collapse consecutive equal labels into contiguous `RegimeSpan`s covering every bar exactly
once (the spans partition `[0, n)`), and `regime_metrics(result, spans)` **shall** compute a
`Metrics` bundle per span by slicing the equity curve to the span and reusing `compute_metrics`. A
span with fewer than two curve points **shall** yield the empty-metrics bundle (no exception).

### REQ-BACKTEST-002-R4 — Robust-Plateau Detection (Optional + Ubiquitous) — plateau

`@SPEC:SPEC-BACKTEST-002` `REQ-BACKTEST-002-R4`

The system **shall** compute, from a completed grid, a `PlateauReport` carrying (a) the **robust
optimum** — the cell maximizing the mean `score` over itself and its von-Neumann grid neighbours
(tie-break = enumeration order) — and (b) the **plateau set** — every cell whose own `score` is
within the relative `tolerance` of the best single score (§Fixed Definitions). **Where** the grid has
a single cell, the robust optimum **shall** be that cell and the plateau set **shall** be `{that
cell}`. The report **shall** state the robust optimum's combination, its robust score, and the
plateau combinations.

### REQ-BACKTEST-002-R5 — Determinism + Decoupling Guard (Unwanted) — determinism

`@SPEC:SPEC-BACKTEST-002` `REQ-BACKTEST-002-R5`

The system **shall not** introduce any non-determinism (no wall clock, no RNG, no set-ordering
dependence): identical `(bars, grid, build, score, window/regime/tolerance params)` **shall** always
yield identical `SweepResult` / `WalkForwardResult` / regime labels / `PlateauReport`. The system
**shall not** import `Config` field names into the sweep core nor mutate the caller's `Config` /
`State` / `bars`; parameter application happens ONLY inside the injected `build` (and the optional
`override_config` helper, which returns a NEW frozen `Config` via pydantic copy and never mutates its
input). The system **shall not** perform money arithmetic on the sweep's own `float` score path (all
`Decimal` quantities pass through from the consumed `BacktestResult` / `Metrics` unchanged).

## Specifications

| Capability                    | Symbol (in `src/ballast/backtest/sweep.py`)                                                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sweep-only types              | `ParamGrid`, `BacktestScenario`, `GridCell`, `SweepResult`, `Window`, `WalkForwardWindowResult`, `WalkForwardResult`, `Regime`, `RegimeSpan`, `PlateauReport` (all immutable) |
| Grid enumeration              | `ParamGrid.combinations() -> list[dict[str, Any]]`                                                                                                                            |
| Grid sweep                    | `run_grid(grid, build, bars, *, score=default_score, tolerance=0.10) -> SweepResult`                                                                                          |
| Walk-forward windows          | `walk_forward_windows(n, *, train, test, step=None, anchored=False) -> list[Window]`                                                                                          |
| Walk-forward run              | `run_walk_forward(grid, build, bars, *, score=default_score, train, test, step=None, anchored=False) -> WalkForwardResult`                                                    |
| Regime classification         | `classify_regimes(bars, *, window=5, up_threshold=Decimal("0.05"), down_threshold=Decimal("-0.05")) -> list[Regime]`                                                          |
| Regime segmentation           | `segment_regimes(bars, *, window=5, up_threshold=..., down_threshold=...) -> list[RegimeSpan]`                                                                                |
| Per-regime metrics            | `regime_metrics(result, spans, *, basis="KRW") -> dict[Regime, Metrics]` (aggregated per label)                                                                               |
| Robust plateau                | `find_plateau(grid, cells, *, tolerance=0.10) -> PlateauReport`                                                                                                               |
| Config override (convenience) | `override_config(base: Config, **dotted: Any) -> Config` (dotted paths e.g. `strategies__vr__g=20`)                                                                           |
| Default score                 | `default_score(metrics: Metrics) -> float` (returns `metrics.cagr`)                                                                                                           |
| Scenario runner               | `run_scenario(scenario: BacktestScenario, bars) -> BacktestResult` (thin wrapper over `run_backtest`)                                                                         |

### Sweep-only types (new in this SPEC; REUSE BACKTEST-001 / CORE-001 elsewhere)

- **`ParamGrid`** — ordered axes `axes: tuple[tuple[str, tuple[Any, ...]], ...]`; `combinations()`
  returns the deterministic Cartesian product. Immutable.
- **`BacktestScenario`** — everything `run_backtest` needs for one run: `strategy: Strategy`,
  `cfg: Config`, `start_state: State`, `cycle_length: Literal["monthly"] | int = "monthly"`,
  `costs: CostModel = CostModel()`, `index: Sequence[tuple[date, Decimal]] | None = None`. Immutable.
- **`GridCell`** — `params: Mapping[str, Any]`, `metrics: Metrics`, `score: float`. Immutable.
- **`SweepResult`** — `cells: tuple[GridCell, ...]`, `best: GridCell`, `plateau: PlateauReport`.
  Immutable.
- **`Window`** — `train: tuple[int, int]`, `test: tuple[int, int]` (half-open bar-index ranges).
  Immutable.
- **`WalkForwardWindowResult`** — `window: Window`, `is_best_params: Mapping[str, Any]`,
  `is_score: float`, `oos_metrics: Metrics`, `oos_score: float`. Immutable.
- **`WalkForwardResult`** — `windows: tuple[WalkForwardWindowResult, ...]`, `mean_oos_score: float`.
  Immutable.
- **`Regime`** — `enum` `UP` / `DOWN` / `SIDEWAYS`.
- **`RegimeSpan`** — `regime: Regime`, `start: int`, `end: int`, `start_date: date`,
  `end_date: date`. Immutable.
- **`PlateauReport`** — `robust_optimum: Mapping[str, Any]`, `robust_score: float`,
  `plateau: tuple[Mapping[str, Any], ...]`. Immutable.

### Reused types (do NOT redefine)

- `BacktestResult`, `Metrics`, `EquityPoint`, `OHLCBar`, `CostModel`, `Basis` — `ballast.backtest`.
- `run_backtest`, `compute_metrics` — `ballast.backtest`.
- `Config`, `State`, `Strategy` — `ballast.core`.

## Dependency on SPEC-BACKTEST-001 / SPEC-CORE-001

This SPEC has a **hard dependency** on SPEC-BACKTEST-001 (it calls `run_backtest` and
`compute_metrics` and consumes their `BacktestResult` / `Metrics`) and, transitively, on
SPEC-CORE-001 (and VR-001 / MAB-001 for realistic strategies a `build` constructs). The sweep never
re-implements any engine, cost, or metric mechanic; it only orchestrates them.

## TBD Items (carried as parameters, not blockers)

| TBD   | Item                           | Resolution carried as                                                                                            |
| ----- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| T-sw1 | Objective / score function     | Injected `score: Callable[[Metrics], float]`; **default `metrics.cagr`** (callers may pass Calmar/Sharpe-based). |
| T-sw2 | Regime trend window/thresholds | `window` (default `5`), `up_threshold` (`+0.05`), `down_threshold` (`-0.05`) params; tunable per study.          |
| T-sw3 | Plateau tolerance              | `tolerance` param (default `0.10`); a wider band admits more cells into the plateau set.                         |
| T-sw4 | Walk-forward window mode       | `anchored` flag (default rolling) + `train`/`test`/`step` bar counts; calendar-aligned windows deferred.         |

## Out of Scope (later SPECs)

- The **concrete data fetcher** (yfinance / CSV) that materializes `bars` / `index` — still a later
  thin adapter (carried over from BACKTEST-001 §Out of Scope).
- A **CLI** `sweep` subcommand and report rendering (tables / CSV / plots) — a later SPEC consumes
  this module's `SweepResult` / `WalkForwardResult`.
- Parallel / distributed sweep execution and result caching — the contract here is a deterministic
  single-process orchestrator; a parallel executor can wrap it later without changing results.
- Calendar-aligned walk-forward windows and multi-objective (Pareto) plateau selection — deferred
  (T-sw4 / T-sw1 carry the single-objective, bar-index contract).

## Traceability

- `@SPEC:SPEC-BACKTEST-002` — this document.
- `@TEST:SPEC-BACKTEST-002` — see `acceptance.md` Given/When/Then scenarios +
  `tests/unit/backtest/test_sweep.py`.
- `@CODE:SPEC-BACKTEST-002` — `src/ballast/backtest/sweep.py`.
- `@DOC:SPEC-BACKTEST-002` — generated during `/moai sync`.
- Depends on `@SPEC:SPEC-BACKTEST-001`, `@SPEC:SPEC-CORE-001`, `@SPEC:SPEC-VR-001`,
  `@SPEC:SPEC-MAB-001`.

### Requirement Index

| Requirement ID      | EARS Type             | Summary                                                                                                |
| ------------------- | --------------------- | ------------------------------------------------------------------------------------------------------ |
| REQ-BACKTEST-002-R1 | Ubiquitous            | Grid: Cartesian combos, build→run_backtest→metrics→score per cell; best + plateau in `SweepResult`     |
| REQ-BACKTEST-002-R2 | Event-driven          | Walk-forward: deterministic windows; per window IS-best→OOS evaluation; mean OOS score                 |
| REQ-BACKTEST-002-R3 | State-driven          | Regime: per-bar UP/DOWN/SIDEWAYS labels, contiguous spans partition `[0,n)`, per-regime metrics        |
| REQ-BACKTEST-002-R4 | Optional + Ubiquitous | Plateau: robust optimum (mean neighbourhood score) + plateau set within tolerance of best single score |
| REQ-BACKTEST-002-R5 | Unwanted              | Determinism + decoupling: no RNG/clock, no Config introspection/mutation, no money math on score path  |
