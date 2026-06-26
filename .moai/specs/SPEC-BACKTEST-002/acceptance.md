# Acceptance Criteria — SPEC-BACKTEST-002 (Parameter Sweep + Walk-Forward + Regime Split + Robust Plateau)

`@SPEC:SPEC-BACKTEST-002` `@TEST:SPEC-BACKTEST-002`

All scenarios use Given/When/Then. The sweep performs NO money arithmetic of its own: every `Decimal`
quantity flows through unchanged from the SPEC-BACKTEST-001 `BacktestResult` / `Metrics` it consumes,
and the sweep's own comparison math (scores, robust scores, regime trend) is `float`. Everything is
deterministic: identical inputs always yield an identical result. Scenarios reuse the BACKTEST-001
stub strategies (`NoopStrategy`, `OneShotBuyStrategy`, `CountingStrategy`) and the `bar(...)` helper.

`OHLCBar` rows give `(date, open, high, low, close, adj_close, fx_usdkrw)`. Unless a scenario
overrides it, FX is constant `fx_usdkrw = "1300.00"` and `adj_close = None`. A `build(params)` maps a
combination to a `BacktestScenario`; a `score(metrics)` collapses metrics to one `float` (default
`metrics.cagr`).

## Scenario 1 — Grid enumerates the Cartesian product in declaration order (REQ-BACKTEST-002-R1)

**Given** a `ParamGrid` with ordered axes `a = [1, 2]` then `b = [10, 20]`,
**When** `grid.combinations()` is evaluated,
**Then** it returns exactly four combinations in this order (last axis varies fastest):
`[{a:1,b:10}, {a:1,b:20}, {a:2,b:10}, {a:2,b:20}]`,
**And (empty grid)** a `ParamGrid` with no axes returns exactly one combination `[{}]`,
**And (count)** for axes of lengths `2 × 3` the combination count is exactly `6`.

## Scenario 2 — run_grid scores every cell and reports the single best (REQ-BACKTEST-002-R1)

**Given** a `ParamGrid` axis `qty = ["10.00", "20.00"]`, a `build` that returns a
`BacktestScenario(strategy=OneShotBuyStrategy(qty=params["qty"]), cfg, start_state pool="100000.00")`,
a 3-bar up-trending series, and a `score = metrics.cagr`,
**When** `run_grid(grid, build, bars, score=score)` is evaluated,
**Then** the `SweepResult.cells` has one `GridCell` per combination, in enumeration order, each
carrying its `params`, a `Metrics` bundle (from `compute_metrics` on that combination's
`BacktestResult`), and its `score`,
**And** `SweepResult.best` is the cell with the highest `score`,
**And (tie-break)** when two cells tie on `score`, `best` is the earlier cell in enumeration order,
**And (determinism)** re-running `run_grid` with identical inputs yields an identical `SweepResult`.

## Scenario 3 — Empty grid yields a single empty-combination cell (REQ-BACKTEST-002-R1)

**Given** a `ParamGrid` with no axes and a `build` that ignores `params`,
**When** `run_grid(empty_grid, build, bars)` is evaluated,
**Then** `SweepResult.cells` has exactly one cell whose `params == {}`,
**And** `SweepResult.best` is that single cell,
**And** the plateau set is exactly that one cell (REQ-BACKTEST-002-R4 single-cell rule).

## Scenario 4 — Walk-forward windows: rolling slices fitting fully within n (REQ-BACKTEST-002-R2)

**Given** `n = 10` bars, `train = 4`, `test = 2`, `step = 2`,
**When** `walk_forward_windows(10, train=4, test=2, step=2)` (rolling) is evaluated,
**Then** it returns exactly three windows:
`(train [0,4), test [4,6))`, `(train [2,6), test [6,8))`, `(train [4,8), test [8,10))`,
**And (no partial)** with `n = 9` the third window's test `[8,10)` would exceed `9`, so only the first
two windows are emitted (a partial trailing test window is never produced),
**And (anchored)** `walk_forward_windows(10, train=4, test=2, step=2, anchored=True)` returns
`(train [0,4), test [4,6))`, `(train [0,6), test [6,8))`, `(train [0,8), test [8,10))`.

## Scenario 5 — run_walk_forward selects in-sample best and scores it out-of-sample (REQ-BACKTEST-002-R2)

**Given** a multi-window series, a `ParamGrid` over a strategy knob, a `build`, and a `score`,
**When** `run_walk_forward(grid, build, bars, score=score, train=..., test=..., step=...)` is
evaluated,
**Then** for each `Window` the system runs the full grid on the train slice, records the in-sample
best combination and its in-sample score, then runs THAT combination on the test slice and records
the out-of-sample `Metrics` and score,
**And** `WalkForwardResult.mean_oos_score` equals the arithmetic mean of the per-window OOS scores,
**And (no windows)** when the series is too short to emit any window, `windows` is empty and
`mean_oos_score == 0.0`,
**And** the in-sample selection tie-breaks on enumeration order (deterministic).

## Scenario 6 — Regime labels from the trailing trend (REQ-BACKTEST-002-R3)

**Given** a close series that rises strongly, then falls strongly, then moves flat, with
`window = 2`, `up_threshold = "0.05"`, `down_threshold = "-0.05"`,
**When** `classify_regimes(bars, window=2, up_threshold=..., down_threshold=...)` is evaluated,
**Then** bar `0` is `SIDEWAYS` (`trend[0] = 0`), bars whose trailing-2 return `> +5%` are `UP`,
bars whose trailing-2 return `< -5%` are `DOWN`, and the rest `SIDEWAYS`,
**And (boundary)** a trailing return of exactly `+0.05` is `SIDEWAYS` (strict `>`), and exactly
`-0.05` is `SIDEWAYS` (strict `<`),
**And** the labels are a pure deterministic function of the close series and the parameters.

## Scenario 7 — segment_regimes partitions [0, n) into contiguous spans (REQ-BACKTEST-002-R3)

**Given** a labelled series `[SIDEWAYS, UP, UP, DOWN, DOWN, SIDEWAYS]`,
**When** `segment_regimes(bars, ...)` is evaluated,
**Then** it returns four contiguous `RegimeSpan`s with half-open ranges
`[0,1) SIDEWAYS`, `[1,3) UP`, `[3,5) DOWN`, `[5,6) SIDEWAYS`,
**And** the spans cover every bar exactly once (`sum of span lengths == n`, no gaps, no overlaps),
**And** each span carries its `start_date` / `end_date` (the first and last bar dates of the span).

## Scenario 8 — Per-regime metrics slice the equity curve and reuse compute_metrics (REQ-BACKTEST-002-R3)

**Given** a `BacktestResult` over a series whose regimes are `UP` then `DOWN`,
**When** `regime_metrics(result, segment_regimes(bars, ...))` is evaluated,
**Then** it returns a `Metrics` bundle per regime label, each computed by `compute_metrics` on the
equity-curve slice for that regime's span(s),
**And (degenerate span)** a span with fewer than two curve points yields the empty-metrics bundle
(CAGR/MDD/vol `== 0`) and raises no exception,
**And** the per-regime CAGR of the `UP` span is positive and that of the `DOWN` span is non-positive
for a curve that rises then falls.

## Scenario 9 — Robust plateau: robust optimum is the high-neighbourhood cell, not the lone max (REQ-BACKTEST-002-R4)

**Given** a 1-D grid axis `g = [5, 10, 15, 20]` whose cells score `[0.10, 0.30, 0.32, 0.05]`,
**When** `find_plateau(grid, cells, tolerance="0.10")` is evaluated,
**Then** the **robust score** of each cell is the mean of itself and its ±1 grid neighbours:
`g=5 → 0.200`, `g=10 → 0.240`, `g=15 → 0.2233…`, `g=20 → 0.185`,
**And** the **robust optimum** is `g = 10` (highest robust score `0.240`) — NOT the single max
`g = 15`, because `g = 15`'s neighbour `g = 20` craters to `0.05`,
**And** the **plateau set** (threshold `0.32 − |0.32|×0.10 = 0.288`) is exactly `{g=10 (0.30),
g=15 (0.32)}`,
**And (single cell)** a grid with one cell returns that cell as both the robust optimum and the sole
plateau member.

## Scenario 10 — override_config returns a new frozen Config without mutating the base (REQ-BACKTEST-002-R5)

**Given** a base `Config` with `strategies.vr.g == 10`,
**When** `override_config(base, strategies__vr__g=20)` is evaluated,
**Then** it returns a NEW `Config` whose `strategies.vr.g == 20`,
**And** the base `Config` is unchanged (`base.strategies.vr.g` is still `10`) — the helper never
mutates its input,
**And** the returned `Config` is still frozen and re-validates (an unknown dotted path or an invalid
value raises rather than silently passing).

## Scenario 11 — Determinism + decoupling invariant (REQ-BACKTEST-002-R5)

**Given** any `(bars, grid, build, score)` and the same window/regime/tolerance parameters,
**When** `run_grid` / `run_walk_forward` / `classify_regimes` / `find_plateau` are each evaluated
twice,
**Then** the two `SweepResult` / `WalkForwardResult` / label-list / `PlateauReport` outputs are
identical (no RNG, no wall clock, no set-ordering dependence),
**And** the input `bars`, `Config`, and `State` are unchanged after the calls (no mutation),
**And** the sweep core contains no reference to `Config` field names (parameter application lives only
in the injected `build` / the `override_config` helper).

---

## Quality-Gate Criteria

A change implementing SPEC-BACKTEST-002 is accepted only when all of the following hold:

- **Tested**: `pytest --cov=src/ballast --cov-report=term-missing` passes with coverage **≥ 85%**
  (the repo standard is 100% on the backtest layer — keep it); each scenario above has a
  corresponding test, and the `hypothesis` invariants hold:
  - **grid size** — `len(run_grid(...).cells)` equals the product of the axis lengths (`1` for an
    empty grid),
  - **window coverage** — every emitted `Window`'s `test` end is `≤ n` and `train`/`test` ranges are
    non-empty and ordered,
  - **regime partition** — `segment_regimes` spans partition `[0, n)` exactly (no gaps/overlaps),
  - **determinism** — identical inputs yield identical results,
  - **no mutation** — `bars` / `Config` / `State` are unchanged after any sweep call.
- **Readable**: `ruff check .` reports **0 errors**; naming follows the constitution (`snake_case`
  functions, `PascalCase` types).
- **Unified**: `ruff format --check .` is clean.
- **Secured / Type-safe**: `mypy --strict src` reports **0 errors**; the sweep core imports no
  `Config` field names (decoupling), performs no money arithmetic on its `float` score path, and
  never mutates a passed-in `Config` / `State` / `bars`.
- **Trackable**: `@SPEC:SPEC-BACKTEST-002` is linked to `@TEST` / `@CODE` / `@DOC` tags; commits use
  conventional-commit format referencing SPEC-BACKTEST-002.

Note: `pandas` / `numpy` **are allowed** in `src/ballast/backtest/` (forbidden only in
`src/ballast/core/`).

## Definition of Done

- All eleven scenarios pass as automated tests under `tests/unit/backtest/test_sweep.py`.
- `src/ballast/backtest/sweep.py` implements the contracts in `spec.md`, reusing the BACKTEST-001
  `run_backtest` / `compute_metrics` and CORE-001 `Config` / `State` / `Strategy` without redefining
  them and adding no new fill/cost/tax mechanic.
- LSP run-phase gates clean: errors = 0, type errors = 0, lint errors = 0.
- Coverage ≥ 85%; the worked examples resolve exactly: the `n=10, train=4, test=2, step=2` rolling
  walk-forward yields the **3** windows listed; the `g ∈ [5,10,15,20]` plateau example resolves to
  robust optimum **`g=10`** and plateau set **`{g=10, g=15}`**.
- The TBD items remain parameters, not hardcoded values: T-sw1 (`score`, default CAGR), T-sw2
  (regime `window` / thresholds), T-sw3 (plateau `tolerance`), T-sw4 (`anchored` + `train`/`test`/
  `step`).

## Traceability

- `@SPEC:SPEC-BACKTEST-002` → `@TEST:SPEC-BACKTEST-002`
  (this file → `tests/unit/backtest/test_sweep.py`) →
  `@CODE:SPEC-BACKTEST-002` (`src/ballast/backtest/sweep.py`) → `@DOC:SPEC-BACKTEST-002`.
- Depends on `@SPEC:SPEC-BACKTEST-001` (`src/ballast/backtest/{engine,metrics,types,costs}.py`) and
  `@SPEC:SPEC-CORE-001` / `@SPEC:SPEC-VR-001` / `@SPEC:SPEC-MAB-001`
  (`src/ballast/core/{models,config,strategy,vr,mab}.py`).
