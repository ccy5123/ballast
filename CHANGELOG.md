# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

- 7 SPECs implemented: SPEC-CORE-001, SPEC-VR-001, SPEC-MAB-001, SPEC-BACKTEST-001,
  SPEC-BACKTEST-002, SPEC-ADAPTER-001, SPEC-ORDER-001.
- Quality gates green: `ruff check` clean, `ruff format` clean, `mypy --strict` clean
  (33 source files), 319 tests passing at 100% coverage.
- Tracked on Draft PR #3, the cumulative trunk superseding PR #1 and PR #2.
