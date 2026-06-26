# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **SPEC-ADAPTER-002 — Toss write (order) adapter (P3).** New `TossOrderAdapter`
  (`src/ballast/adapters/toss/orders.py`) — the concrete `BrokerOrderPort` (`place_order` /
  `cancel_order`) that the P2 Order Manager already calls. It maps a broker-neutral `OrderIntent`
  onto the Toss `OrderCreateRequest` and POSTs `POST /api/v1/orders` (cancel via `/cancel`), then
  maps the Toss response/errors back onto `SubmissionResult`. This turns the dry-run path into a
  real **live-submission** backend without touching the Order Manager: the `BrokerOrderPort`,
  `OrderIntent`, `SubmissionResult` contracts come verbatim from SPEC-ORDER-001 (no fork). Every
  endpoint, field, and enum is grounded in `docs/reference/toss-openapi.json` (Toss Open API,
  OpenAPI 3.1.0, v1.1.5). Verified against mocked HTTP only; the real small-size soak is user-local.
- **SPEC-STATE-001 — State Store + Reconciliation (P-state).** New `src/ballast/state/` package
  (`ports`, `models`, `memory`, `sqlite`, `reconcile`). A backend-agnostic `StateStorePort`
  `typing.Protocol` provides durable, namespaced persistence of per-strategy state (VR `V_n`; MAB
  `avg_price` / `holdings` / `seed_remaining` / `round_idx`, applied via the SPEC-STRATEGY-001
  `PlanResult.state_delta` apply-delta channel), the order/idempotency ledger
  (`client_order_id → {orderId, status, ts}`), and a generic config snapshot
  (ticker / account / allocation / dry_run / kill-switch). Single-writer, restart-safe semantics
  (writer lease + optimistic concurrency + atomic read-your-writes) sit behind the port, with both
  an **in-memory** store (test vehicle) and a **SQLite-on-volume** backend shipping. A **pure,
  idempotent** `reconcile` core marks ledger orders against broker fills and updates positions /
  avg-cost, converging when re-run over the same fills. This resolves the SPEC-ADAPTER-002
  cross-process cancel follow-up and is the durable foundation for 24/7 operation.

### Changed

- **SPEC-STRATEGY-001 — pre-live strategy correctness (P-live).** Fixes both HANDOFF §4 pre-live
  follow-ups. **Contract change:** `Strategy.plan_orders` now returns a `PlanResult(orders,
state_delta)` instead of a bare orders tuple (`src/ballast/core/strategy.py`); `state_delta` is a
  namespace-scoped `Decimal` mapping the driver carries into the next cycle (VR surfaces at least
  `{"V_n": ...}`, MAB surfaces an empty delta). This lets the backtest engine advance VR `V_n`
  faithfully across cycles (engine `_Ledger.v_n`), fixing previously non-faithful multi-cycle VR
  replay. **MAB quarter-sell LOC price:** `mab_on_seed_exhausted` now carries a deterministic
  injected limit price (was `limit_price=None` / effectively MOC), so the live Toss path accepts it
  as a valid priced LOC (`LIMIT` + `timeInForce=CLS`) while the backtest close-fill behavior is
  preserved. VR / MAB / the engine are updated consistently with the new `Strategy` contract.

- **SPEC-ORDER-001 — Order Manager + dry-run (P2).** New broker-neutral `src/ballast/orders/`
  package (`models`, `ports`, `mapping`, `guards`, `manager`, `recording`). It converts a CORE
  `Order` into a broker-neutral `OrderIntent`, derives a deterministic idempotency
  `client_order_id` (strategy namespace + date/cycle + stable order signature, valid as a Toss
  `clientOrderId`), and defaults to **dry-run** — recording and logging the order plan with no
  submission. The live path (`execution.dry_run=False`) submits through a broker-agnostic
  `BrokerOrderPort` with idempotent dedup and submission-result tracking. Safety guards (global
  kill-switch, `max_position_pct` clamp, dry-run gate) wrap every path. P2 ships **no write
  adapter**; the concrete Toss write adapter is deferred to P3 (SPEC-ADAPTER-002).
- **SPEC-BACKTEST-002 — robustness evaluation.** Parameter sweep, walk-forward analysis,
  regime split (uptrend / downtrend / sideways), and plateau (robust-region) evaluation for
  the backtest engine. Integrated onto this branch via the PR #2 merge.
- **Toss env credential bootstrap.** `Adapter.from_env` factory
  (`src/ballast/adapters/toss/factory.py`) loading `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET` from
  the environment, an `.env.example` template, a `scripts/toss_smoke.py` smoke test, and a
  `src/ballast/py.typed` marker. Live-verified against the real Toss Open API. Integrated onto
  this branch via the PR #2 merge.

### Notes

- 10 SPECs implemented: SPEC-CORE-001, SPEC-VR-001, SPEC-MAB-001, SPEC-BACKTEST-001,
  SPEC-BACKTEST-002, SPEC-ADAPTER-001, SPEC-ORDER-001, SPEC-ADAPTER-002, SPEC-STRATEGY-001,
  SPEC-STATE-001.
- Quality gates green: `ruff check` clean, `ruff format` clean, `mypy --strict` clean
  (41 source files), 474 tests passing at 100% coverage.
- Tracked on Draft PR #3, the cumulative trunk. PR #1 and PR #2 are now closed (superseded by #3).
- Next: the 24/7 path — Runner/Config entry point, Scheduler, Notifier, Streamlit dashboard, deploy
  (worker is the always-on engine; Streamlit is the dashboard, decoupled through the State Store).
