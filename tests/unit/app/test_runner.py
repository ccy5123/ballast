"""Tests for the Runner's one-cycle assembly (REQ-RUNNER-001-R2..R6).

@TEST:SPEC-RUNNER-001

Drives the whole Runner against the STATE-001 ``InMemoryStateStore`` and the
ORDER-001 ``RecordingBrokerOrderPort`` (or a mock ``BrokerOrderPort``) — no real
Toss network, no real DB, no credentials. Time / market / lease ``owner`` /
``ttl`` are injected for determinism. Covers config seed/load (R2), the
single-writer lease (R3), the one-cycle assembly + persistence (R4), the reused
kill-switch / dry_run / max_position_pct guards (R5), and idempotent / re-entrant
re-runs plus cross-process cancel resolution (R6). Maps to acceptance.md
AC-4 .. AC-18.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ballast.app import (
    AccountInputs,
    CycleResult,
    Runner,
    RunnerConfig,
    RunnerNotStartedError,
    resolve_owner,
)
from ballast.core.config import Config
from ballast.core.models import Market
from ballast.orders.models import OrderIntent, SubmissionResult, SubmissionStatus
from ballast.orders.ports import BrokerOrderPort
from ballast.orders.recording import RecordingBrokerOrderPort
from ballast.state.memory import InMemoryStateStore
from ballast.state.models import (
    ConcurrencyError,
    OrderState,
    WriterLeaseHeldError,
)

from .conftest import _buy_order, _FakeStrategy

_OWNER = "worker-A"
_CYCLE = "vr-20260626"


def _seed(store: InMemoryStateStore, runner_config: RunnerConfig) -> None:
    """Seed the operational snapshot directly (version 0 -> 1)."""
    store.set_config(runner_config.to_snapshot(), expected_version=0)


def _started_runner(
    store: InMemoryStateStore, *, owner: str = _OWNER, ttl: int | None = 60
) -> Runner:
    """A Runner with its single-writer lease already acquired."""
    runner = Runner(store, owner=owner, ttl=ttl)
    runner.start()
    return runner


# ====================================================================================
# R2 — config load / first-boot seed / persist via the State Store
# ====================================================================================


# AC-4 — first boot seeds the snapshot once from the typed config.
def test_first_boot_seeds_snapshot_once(
    store: InMemoryStateStore, runner_config: RunnerConfig
) -> None:
    assert store.get_config().version == 0
    runner = Runner(store, owner=_OWNER, ttl=60)
    new_version = runner.seed_config(runner_config)
    assert new_version == 1
    record = store.get_config()
    assert record.version == 1
    assert record.data == runner_config.to_snapshot()


# AC-4 — a second boot does NOT re-seed (snapshot is the runtime authority).
def test_second_boot_does_not_reseed(
    store: InMemoryStateStore, runner_config: RunnerConfig
) -> None:
    runner = Runner(store, owner=_OWNER, ttl=60)
    runner.seed_config(runner_config)
    # A dashboard flips kill_switch durably AFTER the first seed.
    flipped = runner_config.model_copy(update={"kill_switch": True})
    store.set_config(flipped.to_snapshot(), expected_version=1)
    # The next boot must NOT overwrite the dashboard's change.
    result = runner.seed_config(runner_config)
    assert result is None
    assert RunnerConfig.from_snapshot(store.get_config().data).kill_switch is True


# AC-5 — the snapshot is the runtime authority; a dashboard flip is observed next cycle.
def test_dashboard_kill_switch_flip_observed_next_cycle(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)  # dry_run=False, kill_switch=False
    runner = _started_runner(store)
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta={"V_n": Decimal("1042.37")})

    # First cycle: live, the order is SUBMITTED.
    first = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key="vr-c1", port=port)
    assert first.plan.results[0].status == SubmissionStatus.SUBMITTED

    # A dashboard-style writer flips kill_switch durably.
    flipped = runner_config.model_copy(update={"kill_switch": True})
    store.set_config(flipped.to_snapshot(), expected_version=store.get_config().version)

    # The worker observes kill_switch=True on its NEXT cycle (snapshot wins).
    second_strategy = _FakeStrategy("vr", orders=(_buy_order(ticker="QLD", qty="5"),))
    second = runner.run_one_cycle(second_strategy, market, cfg=cfg, cycle_key="vr-c2", port=port)
    assert second.plan.results[0].status == SubmissionStatus.BLOCKED


# AC-5 — load_config reconstructs the typed RunnerConfig from the snapshot.
def test_load_config_reads_snapshot(store: InMemoryStateStore, runner_config: RunnerConfig) -> None:
    _seed(store, runner_config)
    runner = Runner(store, owner=_OWNER, ttl=60)
    loaded = runner.load_config()
    assert loaded == runner_config


# AC-6 — persisting a config change uses optimistic concurrency.
def test_stale_config_write_raises_concurrency_error(
    store: InMemoryStateStore, runner_config: RunnerConfig
) -> None:
    _seed(store, runner_config)  # version -> 1
    store.set_config(runner_config.to_snapshot(), expected_version=1)  # version -> 2
    with pytest.raises(ConcurrencyError):
        store.set_config(runner_config.to_snapshot(), expected_version=1)  # stale
    assert store.get_config().version == 2


# ====================================================================================
# R3 — single-writer lease before any mutation or submission
# ====================================================================================


# AC-7 — the lease is acquired before any mutating cycle (no lease-less mutation).
def test_run_one_cycle_requires_lease_held(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = Runner(store, owner=_OWNER, ttl=60)  # NOT started: no lease
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta={"V_n": Decimal("1")})
    with pytest.raises(RunnerNotStartedError):
        runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port)
    # Nothing mutated: no ledger entry, no strategy-state advance.
    assert port.recorded_intents == ()
    assert store.load_strategy_state("vr").version == 0


# AC-7 — start() acquires exactly one lease and holds it (idempotent).
def test_start_acquires_and_holds_lease(store: InMemoryStateStore) -> None:
    runner = Runner(store, owner=_OWNER, ttl=60)
    assert runner.lease_held is False
    lease_a = runner.start()
    assert runner.lease_held is True
    lease_b = runner.start()  # idempotent: no second acquire
    assert lease_a.token == lease_b.token


# AC-8 — a second concurrent writer is rejected; the first can release and the second re-take.
def test_second_writer_rejected_then_succeeds_after_release(store: InMemoryStateStore) -> None:
    worker_a = Runner(store, owner="worker-A", ttl=60)
    worker_a.start()
    worker_b = Runner(store, owner="worker-B", ttl=60)
    with pytest.raises(WriterLeaseHeldError):
        worker_b.start()
    worker_a.shutdown()  # release
    worker_b.start()  # now succeeds
    assert worker_b.lease_held is True


# AC-8 — reads never require the lease (the dashboard reads freely).
def test_reads_need_no_lease(store: InMemoryStateStore, runner_config: RunnerConfig) -> None:
    _seed(store, runner_config)
    # No lease held anywhere; reads still work.
    assert store.get_config().version == 1
    assert store.load_strategy_state("vr").version == 0
    assert store.load_order("nope") is None
    assert store.list_open_orders("0001") == ()


# AC-8 — the Runner can be used as a context manager (acquire on enter, release on exit).
def test_runner_context_manager_acquires_and_releases(store: InMemoryStateStore) -> None:
    with Runner(store, owner=_OWNER, ttl=60) as runner:
        assert runner.lease_held is True
    # On exit the lease is released, so another owner may take it.
    other = Runner(store, owner="worker-B", ttl=60)
    other.start()
    assert other.lease_held is True


def test_shutdown_is_idempotent(store: InMemoryStateStore) -> None:
    runner = Runner(store, owner=_OWNER, ttl=60)
    runner.shutdown()  # no lease yet: no-op
    runner.start()
    runner.shutdown()
    runner.shutdown()  # second release: no-op
    assert runner.lease_held is False


# ====================================================================================
# R4 — run one cycle: strategy -> Order Manager -> (recording) adapter
# ====================================================================================


# AC-9 — one cycle assembles the existing parts and persists the result.
def test_one_cycle_assembles_and_persists(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)  # dry_run=False, kill_switch=False
    runner = _started_runner(store)
    delta = {"V_n": Decimal("1042.37")}
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta=delta)

    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port)

    # plan_orders called exactly once; market is the only source of now/price.
    assert len(strategy.calls) == 1
    called_market, called_state, called_cfg = strategy.calls[0]
    assert called_market is market
    assert called_state.ns == "vr"
    assert called_cfg is cfg

    # The recording port saw exactly one place_order for the BUY.
    assert len(port.recorded_intents) == 1

    # The SUBMITTED result was upserted into the durable ledger.
    (submitted,) = result.plan.results
    assert submitted.status == SubmissionStatus.SUBMITTED
    ledger = store.load_order(submitted.client_order_id)
    assert ledger is not None
    assert ledger.status == OrderState.SUBMITTED
    assert ledger.broker_order_id == submitted.broker_order_id
    assert ledger.ordered_qty == Decimal("3.00")
    assert ledger.ts == _CYCLE

    # The state_delta was applied, bumping the version 0 -> 1.
    assert store.load_strategy_state("vr").version == 1
    assert store.load_strategy_state("vr").data["V_n"] == Decimal("1042.37")

    # An inspectable CycleResult is returned.
    assert isinstance(result, CycleResult)
    assert result.ns == "vr"
    assert result.state_delta == delta
    assert result.new_version == 1
    assert result.ledger_upserts == (submitted.client_order_id,)


# AC-10 — the clock and market-hours are injected; the cycle uses no other source.
def test_market_and_cycle_key_are_the_only_time_source(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta={"V_n": Decimal("1")})
    runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key="stamp-xyz", port=port)
    # The injected cycle_key is the ledger ts (the Runner stamps nothing of its own).
    (res,) = port.results
    ledger = store.load_order(res.client_order_id)
    assert ledger is not None
    assert ledger.ts == "stamp-xyz"
    # The injected market is the snapshot the strategy saw (is_open / is_holiday).
    assert strategy.calls[0][0].is_open is True
    assert strategy.calls[0][0].is_holiday is False


def test_runner_module_reads_no_wall_clock() -> None:
    # Static guard: the app layer imports neither datetime nor time for "now".
    import ballast.app.runner as runner_mod

    source = runner_mod.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "datetime" not in text
    assert "import time" not in text


# AC-11 — MAB's empty state_delta is a no-op on the persisted data.
def test_mab_empty_delta_is_data_noop(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    strategy = _FakeStrategy("mab", orders=(_buy_order(account_seq="0002"),), state_delta={})

    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key="mab-c1", port=port)

    # The orders still flow through the Order Manager and the ledger.
    assert len(port.recorded_intents) == 1
    assert result.plan.results[0].status == SubmissionStatus.SUBMITTED
    # The persisted MAB data is unchanged (the empty delta touches no key).
    assert dict(store.load_strategy_state("mab").data) == {}


# ====================================================================================
# R5 — safety gating: kill-switch + dry_run gate block submission (reused guards)
# ====================================================================================


# AC-12 — the kill-switch blocks every order; nothing is submitted or upserted.
def test_kill_switch_blocks_everything(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    killed = runner_config.model_copy(update={"kill_switch": True})
    _seed(store, killed)
    runner = _started_runner(store)
    strategy = _FakeStrategy(
        "vr",
        orders=(_buy_order(), _buy_order(ticker="QLD", qty="5")),
        state_delta={"V_n": Decimal("1")},
    )

    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port)

    assert all(r.status == SubmissionStatus.BLOCKED for r in result.plan.results)
    assert all("kill-switch" in (r.reason or "") for r in result.plan.results)
    # No live call, no ledger upsert for any blocked order.
    assert port.recorded_intents == ()
    assert result.ledger_upserts == ()
    assert store.load_order(result.plan.results[0].client_order_id) is None


# AC-13 — dry_run takes the record-only path with no Toss/recording-port call.
def test_dry_run_records_only_no_port_call(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    dry = runner_config.model_copy(update={"dry_run": True})
    _seed(store, dry)
    runner = _started_runner(store)
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta={"V_n": Decimal("1")})

    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port)

    assert result.plan.results[0].status == SubmissionStatus.RECORDED
    # The port is never touched (no network, even though a port was injected).
    assert port.recorded_intents == ()
    # No RECORDED order is upserted into the durable ledger.
    assert result.ledger_upserts == ()
    assert store.load_order(result.plan.results[0].client_order_id) is None
    # The cycle still returns an inspectable preview.
    assert len(result.plan.intents) == 1


# AC-14 — max_position_pct is threaded into the reused clamp/block guard.
def test_max_position_pct_clamps_over_cap_buy(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    port: RecordingBrokerOrderPort,
    tmp_path: object,
) -> None:
    # A tight 0.20 cap so a qty=15 @ 80 BUY over-caps and is clamped.
    cfg = _config_with_cap(tmp_path, "0.20")
    _seed(store, runner_config)  # dry_run=False
    runner = _started_runner(store)
    strategy = _FakeStrategy(
        "vr", orders=(_buy_order(qty="15"),), state_delta={"V_n": Decimal("1")}
    )
    # cap = base 1000 * 0.20 = 200; current 0; ref price 80 => max qty 2.50 (from 15).
    inputs = AccountInputs(
        base_values={"0001": Decimal("1000.00")},
        positions={"QLD": Decimal("0.00")},
        prices={"QLD": Decimal("80.00")},
    )
    result = runner.run_one_cycle(
        strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port, account_inputs=inputs
    )
    (res,) = result.plan.results
    assert res.status == SubmissionStatus.SUBMITTED
    submitted_intent = port.recorded_intents[0]
    assert submitted_intent.qty == Decimal("2.50")  # clamped down from 15, never up
    assert res.reason is not None and "clamp" in res.reason.lower()


# AC-14 — no headroom blocks the BUY; nothing is submitted or upserted.
def test_max_position_pct_blocks_when_no_headroom(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    port: RecordingBrokerOrderPort,
    tmp_path: object,
) -> None:
    cfg = _config_with_cap(tmp_path, "0.20")
    _seed(store, runner_config)
    runner = _started_runner(store)
    strategy = _FakeStrategy("vr", orders=(_buy_order(qty="1"),), state_delta={"V_n": Decimal("1")})
    inputs = AccountInputs(
        base_values={"0001": Decimal("1000.00")},
        positions={"QLD": Decimal("200.00")},  # cap already met
        prices={"QLD": Decimal("80.00")},
    )
    result = runner.run_one_cycle(
        strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port, account_inputs=inputs
    )
    assert result.plan.results[0].status == SubmissionStatus.BLOCKED
    assert "overflow" in (result.plan.results[0].reason or "").lower()
    assert port.recorded_intents == ()
    assert result.ledger_upserts == ()


# ====================================================================================
# R6 — idempotent / re-entrant one-cycle + cross-process cancel
# ====================================================================================


# AC-15 — re-running the same cycle does not double-submit.
def test_rerun_same_cycle_does_not_double_submit(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    delta = {"V_n": Decimal("1042.37")}

    first = runner.run_one_cycle(
        _FakeStrategy("vr", orders=(_buy_order(),), state_delta=delta),
        market,
        cfg=cfg,
        cycle_key=_CYCLE,
        port=port,
    )
    coid = first.plan.results[0].client_order_id
    assert first.plan.results[0].status == SubmissionStatus.SUBMITTED

    # Re-run the SAME (ns, cycle_key, market, cfg): same client_order_id, DUPLICATE.
    second = runner.run_one_cycle(
        _FakeStrategy("vr", orders=(_buy_order(),), state_delta=delta),
        market,
        cfg=cfg,
        cycle_key=_CYCLE,
        port=port,
    )
    assert second.plan.results[0].client_order_id == coid
    assert second.plan.results[0].status == SubmissionStatus.DUPLICATE
    # No second distinct live order on the port; one durable ledger entry.
    assert len(port.recorded_intents) == 1
    assert store.load_order(coid) is not None


# AC-16 — re-running does not double-apply the state delta (convergence).
def test_rerun_does_not_double_apply_state_delta(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    delta = {"V_n": Decimal("1042.37")}

    first = runner.run_one_cycle(
        _FakeStrategy("vr", orders=(_buy_order(),), state_delta=delta),
        market,
        cfg=cfg,
        cycle_key=_CYCLE,
        port=port,
    )
    assert first.new_version == 1
    assert store.load_strategy_state("vr").version == 1

    # The re-run is a converged no-op: V_n is NOT advanced a second time.
    second = runner.run_one_cycle(
        _FakeStrategy("vr", orders=(_buy_order(),), state_delta=delta),
        market,
        cfg=cfg,
        cycle_key=_CYCLE,
        port=port,
    )
    assert second.new_version == 1
    assert store.load_strategy_state("vr").version == 1


# AC-16 — the store's expected_version guard rejects a literal stale re-apply.
def test_stale_apply_strategy_delta_raises_concurrency_error(
    store: InMemoryStateStore,
) -> None:
    # First application: expected_version=0 -> version 1.
    v_n = {"V_n": Decimal("1042.37")}
    assert store.apply_strategy_delta("vr", v_n, expected_version=0) == 1
    # A literal stale re-attempt with expected_version=0 is rejected.
    with pytest.raises(ConcurrencyError):
        store.apply_strategy_delta("vr", v_n, expected_version=0)
    # The persisted V_n is NOT advanced a second time.
    assert store.load_strategy_state("vr").version == 1


# AC-17 — cross-process cancel resolves via the persisted ledger after a restart.
def test_cross_process_cancel_resolves_via_persisted_ledger(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    strategy = _FakeStrategy("mab", orders=(_buy_order(account_seq="0002"),), state_delta={})
    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key="mab-c0007", port=port)
    coid = result.plan.results[0].client_order_id
    broker_id = result.plan.results[0].broker_order_id
    assert broker_id is not None

    # "Restart": a fresh TossOrderAdapter would have an empty in-process map and
    # return None; the persisted ledger resolves the broker_order_id durably.
    assert store.resolve_order_id(coid) == broker_id
    # An unknown client_order_id resolves to None (no guessing).
    assert store.resolve_order_id("never-seen") is None


# ====================================================================================
# Owner resolution (FD3, Decision 3) + ledger persistence edge cases
# ====================================================================================


def test_resolve_owner_prefers_worker_id_env() -> None:
    assert resolve_owner({"WORKER_ID": "worker-7"}) == "worker-7"


def test_resolve_owner_falls_back_to_host_pid() -> None:
    assert resolve_owner({}, hostname="host", pid=4242) == "host:4242"


def test_resolve_owner_empty_worker_id_uses_fallback() -> None:
    assert resolve_owner({"WORKER_ID": ""}, hostname="h", pid=1) == "h:1"


# A FAILED live result IS durably upserted (traceability), while BLOCKED is not.
def test_failed_result_is_upserted(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
) -> None:
    _seed(store, runner_config)  # dry_run=False
    runner = _started_runner(store)
    failing = _FailingPort()
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta={"V_n": Decimal("1")})
    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=failing)
    (res,) = result.plan.results
    assert res.status == SubmissionStatus.FAILED
    ledger = store.load_order(res.client_order_id)
    assert ledger is not None
    assert ledger.status == OrderState.FAILED
    assert result.ledger_upserts == (res.client_order_id,)


# A cycle with NO orders applies the delta normally (no DUPLICATE skip).
def test_cycle_with_no_orders_applies_delta(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    strategy = _FakeStrategy("vr", orders=(), state_delta={"V_n": Decimal("999.00")})
    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port)
    assert result.plan.results == ()
    assert result.ledger_upserts == ()
    assert result.new_version == 1
    assert store.load_strategy_state("vr").data["V_n"] == Decimal("999.00")


# run_one_cycle works with a mock BrokerOrderPort (structural conformance).
def test_run_one_cycle_with_mock_port(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    mock_port = _MockPort()
    assert isinstance(mock_port, BrokerOrderPort)
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta={"V_n": Decimal("1")})
    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=mock_port)
    assert result.plan.results[0].status == SubmissionStatus.SUBMITTED
    assert mock_port.calls == 1


# --- AC-18 — secrets are env-only and never in a repr ---------------------------------


def test_no_secret_in_runner_or_cycle_result_repr(
    store: InMemoryStateStore,
    runner_config: RunnerConfig,
    market: Market,
    cfg: Config,
    port: RecordingBrokerOrderPort,
) -> None:
    _seed(store, runner_config)
    runner = _started_runner(store)
    strategy = _FakeStrategy("vr", orders=(_buy_order(),), state_delta={"V_n": Decimal("1")})
    result = runner.run_one_cycle(strategy, market, cfg=cfg, cycle_key=_CYCLE, port=port)
    # No credential material appears in any user-facing repr (no Toss token needed).
    for text in (repr(runner), repr(result), repr(runner_config), runner_config.to_snapshot()):
        rendered = str(text)
        assert "TOSS_CLIENT_ID" not in rendered
        assert "TOSS_CLIENT_SECRET" not in rendered
        assert "secret" not in rendered.lower()


def _config_with_cap(tmp_path: object, cap: str) -> Config:
    """Build a Config whose execution.max_position_pct is ``cap``."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    yaml_text = f"""\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true

execution:
  broker: toss
  dry_run: true
  max_position_pct: "{cap}"

instruments:
  QLD:
    leverage: 2
    underlying: NDX
    default_target_pct: "0.60"
    default_band: "0.10"

strategies:
  vr:
    account_seq: "0001"
    ticker: QLD
  mab:
    account_seq: "0002"
    ticker: QLD
"""
    path = tmp_path / "cap_config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return Config.load(path)


class _FailingPort:
    """A BrokerOrderPort whose place_order returns a FAILED result."""

    def place_order(self, intent: OrderIntent) -> SubmissionResult:
        return SubmissionResult(
            client_order_id=intent.client_order_id,
            status=SubmissionStatus.FAILED,
            reason="rejected by venue",
        )

    def cancel_order(self, account_seq: str, client_order_id: str) -> SubmissionResult:
        return SubmissionResult(client_order_id=client_order_id, status=SubmissionStatus.FAILED)


class _MockPort:
    """A minimal mock BrokerOrderPort that counts calls and returns SUBMITTED."""

    def __init__(self) -> None:
        self.calls = 0

    def place_order(self, intent: OrderIntent) -> SubmissionResult:
        self.calls += 1
        return SubmissionResult(
            client_order_id=intent.client_order_id,
            status=SubmissionStatus.SUBMITTED,
            broker_order_id=f"mock-{self.calls}",
        )

    def cancel_order(self, account_seq: str, client_order_id: str) -> SubmissionResult:
        return SubmissionResult(
            client_order_id=client_order_id,
            status=SubmissionStatus.SUBMITTED,
            broker_order_id="mock-cancel",
        )
