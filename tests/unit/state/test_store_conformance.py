"""Shared port-conformance suite over BOTH backends (REQ-STATE-001-R1/R2/R3/R5).

@TEST:SPEC-STATE-001

Every scenario here runs against ``InMemoryStateStore`` AND ``SqliteStateStore``
(temp file) via the ``store`` / ``store_factory`` fixtures, asserting the two
backends are behaviorally identical (FD8 parity, AC-17). The factory re-opens a
store over the same backing to model a worker restart (durability / restart-safe).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ballast.state.models import (
    ConcurrencyError,
    OrderState,
    WriterLeaseHeldError,
)
from ballast.state.ports import StateStorePort

from .conftest import StoreFactory

# --- AC-1 / AC-2 — strategy-state apply-delta + durable reload -----------------------


def test_load_strategy_state_defaults_to_empty(store: StateStorePort) -> None:
    rec = store.load_strategy_state("vr")
    assert rec.ns == "vr"
    assert rec.data == {}
    assert rec.version == 0


def test_apply_delta_advances_version_and_value(store: StateStorePort) -> None:
    new_version = store.apply_strategy_delta("vr", {"V_n": Decimal("1042.37")}, expected_version=0)
    assert new_version == 1
    # read-your-writes
    rec = store.load_strategy_state("vr")
    assert rec.data["V_n"] == Decimal("1042.37")
    assert rec.version == 1
    assert isinstance(rec.data["V_n"], Decimal)


def test_apply_delta_persists_across_restart(store_factory: StoreFactory) -> None:
    store_factory().apply_strategy_delta("vr", {"V_n": Decimal("1050.00")}, expected_version=0)
    # "restart": a fresh store over the same backing returns the last value.
    rec = store_factory().load_strategy_state("vr")
    assert rec.data["V_n"] == Decimal("1050.00")
    assert isinstance(rec.data["V_n"], Decimal)


def test_apply_delta_ignores_unknown_keys(store: StateStorePort) -> None:
    store.apply_strategy_delta(
        "vr", {"V_n": Decimal("900.00"), "future_key": Decimal("7")}, expected_version=0
    )
    rec = store.load_strategy_state("vr")
    assert rec.data["V_n"] == Decimal("900.00")
    assert "future_key" not in rec.data


def test_apply_delta_stale_version_raises_and_does_not_clobber(store: StateStorePort) -> None:
    store.apply_strategy_delta("vr", {"V_n": Decimal("10.00")}, expected_version=0)
    store.apply_strategy_delta("vr", {"V_n": Decimal("20.00")}, expected_version=1)
    assert store.load_strategy_state("vr").version == 2
    with pytest.raises(ConcurrencyError):
        store.apply_strategy_delta("vr", {"V_n": Decimal("1.00")}, expected_version=1)
    # unchanged: no clobber by a stale/second writer.
    assert store.load_strategy_state("vr").data["V_n"] == Decimal("20.00")


def test_mab_state_persists_fill_derived_keys(store: StateStorePort) -> None:
    store.apply_strategy_delta(
        "mab",
        {
            "avg_price": Decimal("82.50"),
            "holdings": Decimal("12"),
            "seed_remaining": Decimal("300.00"),
            "round_idx": Decimal("3"),
        },
        expected_version=0,
    )
    data = store.load_strategy_state("mab").data
    assert data["avg_price"] == Decimal("82.50")
    assert data["holdings"] == Decimal("12")
    assert data["seed_remaining"] == Decimal("300.00")
    assert data["round_idx"] == Decimal("3")


def test_mab_empty_delta_is_noop_but_bumps_version(store: StateStorePort) -> None:
    store.apply_strategy_delta("mab", {"holdings": Decimal("5")}, expected_version=0)
    v = store.load_strategy_state("mab").version
    new_v = store.apply_strategy_delta("mab", {}, expected_version=v)
    assert new_v == v + 1
    # data unchanged by the empty delta.
    assert store.load_strategy_state("mab").data["holdings"] == Decimal("5")


def test_apply_delta_rejects_float_value(store: StateStorePort) -> None:
    with pytest.raises((ValueError, TypeError)):
        store.apply_strategy_delta("vr", {"V_n": 1.5}, expected_version=0)  # type: ignore[dict-item]


# --- AC-6 / AC-7 / AC-8 — order ledger + cross-process resolve -----------------------


def test_upsert_and_resolve_order_id(store: StateStorePort) -> None:
    store.upsert_order(
        "vr-20260626-abc",
        account_seq="acc-1",
        broker_order_id="toss-9001",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("3"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    assert store.resolve_order_id("vr-20260626-abc") == "toss-9001"
    rec = store.load_order("vr-20260626-abc")
    assert rec is not None
    assert rec.status is OrderState.SUBMITTED


def test_resolve_order_id_across_restart(store_factory: StoreFactory) -> None:
    store_factory().upsert_order(
        "mab-c0007-def",
        account_seq="acc-1",
        broker_order_id="toss-9100",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    # "restart" with a fresh store: the persisted ledger still resolves the id.
    assert store_factory().resolve_order_id("mab-c0007-def") == "toss-9100"


def test_resolve_unknown_order_id_returns_none(store: StateStorePort) -> None:
    assert store.resolve_order_id("nope-000") is None
    assert store.load_order("nope-000") is None


def test_ledger_status_advances_and_never_regresses(store: StateStorePort) -> None:
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("5"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.FILLED,
        ordered_qty=Decimal("5"),
        filled_qty=Decimal("5"),
        ts="t1",
    )
    rec = store.load_order("k1")
    assert rec is not None and rec.status is OrderState.FILLED
    # A regression attempt back to SUBMITTED is ignored (terminal monotonic).
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("5"),
        filled_qty=Decimal("0"),
        ts="t2",
    )
    rec2 = store.load_order("k1")
    assert rec2 is not None and rec2.status is OrderState.FILLED


def test_same_status_reupsert_is_noop(store: StateStorePort) -> None:
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("5"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    # Re-upserting the SAME status (even on a terminal) is an allowed no-op.
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("5"),
        filled_qty=Decimal("0"),
        ts="t1",
    )
    rec = store.load_order("k1")
    assert rec is not None and rec.status is OrderState.SUBMITTED


def test_list_open_orders_scopes_by_account(store: StateStorePort) -> None:
    store.upsert_order(
        "open-1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    store.upsert_order(
        "done-1",
        account_seq="acc-1",
        broker_order_id="toss-2",
        status=OrderState.FILLED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("1"),
        ts="t0",
    )
    store.upsert_order(
        "open-other",
        account_seq="acc-2",
        broker_order_id="toss-3",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    open_acc1 = store.list_open_orders("acc-1")
    coids = {o.client_order_id for o in open_acc1}
    assert coids == {"open-1"}  # only the open one for acc-1


# --- AC-9 — single-writer lease ------------------------------------------------------


def test_writer_lease_rejects_second_writer(store: StateStorePort) -> None:
    lease = store.acquire_writer_lease("worker-A", ttl=30)
    with pytest.raises(WriterLeaseHeldError):
        store.acquire_writer_lease("worker-B", ttl=30)
    # After release, worker-B can take it.
    store.release_writer_lease(lease)
    lease_b = store.acquire_writer_lease("worker-B", ttl=30)
    assert lease_b.owner == "worker-B"


def test_release_with_non_matching_lease_is_noop(store: StateStorePort) -> None:
    from ballast.state.models import Lease

    real = store.acquire_writer_lease("worker-A", ttl=30)
    # A release that does not match the held lease token is a safe no-op: it must
    # not free worker-A's lease for a second writer.
    store.release_writer_lease(Lease(owner="worker-A", ttl=30, token="not-the-token"))
    with pytest.raises(WriterLeaseHeldError):
        store.acquire_writer_lease("worker-B", ttl=30)
    # The genuine lease still releases correctly.
    store.release_writer_lease(real)
    assert store.acquire_writer_lease("worker-B", ttl=30).owner == "worker-B"


def test_same_owner_can_reacquire_lease(store: StateStorePort) -> None:
    store.acquire_writer_lease("worker-A", ttl=30)
    # The same owner re-acquiring is allowed (re-entrant / refresh), not a 2nd writer.
    lease = store.acquire_writer_lease("worker-A", ttl=30)
    assert lease.owner == "worker-A"


def test_reads_never_require_the_lease(store: StateStorePort) -> None:
    store.acquire_writer_lease("worker-A", ttl=30)
    # Reads succeed with no lease held by the reader.
    assert store.load_strategy_state("vr").version == 0
    assert store.load_order("nope") is None
    assert store.get_config().version == 0


# --- AC-10 — atomic + read-your-writes -----------------------------------------------


def test_read_your_writes_for_every_mutating_path(store: StateStorePort) -> None:
    v = store.apply_strategy_delta("vr", {"V_n": Decimal("1.00")}, expected_version=0)
    assert store.load_strategy_state("vr").version == v
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    assert store.load_order("k1") is not None
    cv = store.set_config({"dry_run": True}, expected_version=0)
    assert store.get_config().version == cv


def test_reload_returns_last_committed_values(store_factory: StoreFactory) -> None:
    s = store_factory()
    s.apply_strategy_delta("vr", {"V_n": Decimal("7.00")}, expected_version=0)
    s.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    s.set_config({"ticker": "QLD"}, expected_version=0)

    reopened = store_factory()
    assert reopened.load_strategy_state("vr").data["V_n"] == Decimal("7.00")
    assert reopened.resolve_order_id("k1") == "toss-1"
    assert reopened.get_config().data["ticker"] == "QLD"


# --- AC-18 — generic config snapshot -------------------------------------------------


def test_config_snapshot_round_trip(store: StateStorePort) -> None:
    new_version = store.set_config(
        {
            "ticker": "QLD",
            "account_seq": "acc-1",
            "allocation": {"vr": "0.6", "mab": "0.4"},
            "dry_run": True,
            "kill_switch": False,
        },
        expected_version=0,
    )
    assert new_version == 1
    cfg = store.get_config()
    assert cfg.version == 1
    assert cfg.data["ticker"] == "QLD"
    assert cfg.data["allocation"] == {"vr": "0.6", "mab": "0.4"}
    assert cfg.data["dry_run"] is True
    assert cfg.data["kill_switch"] is False


def test_config_snapshot_stale_version_raises(store: StateStorePort) -> None:
    store.set_config({"dry_run": True}, expected_version=0)
    with pytest.raises(ConcurrencyError):
        store.set_config({"dry_run": False}, expected_version=0)


def test_config_persists_across_restart(store_factory: StoreFactory) -> None:
    store_factory().set_config({"ticker": "SOXL"}, expected_version=0)
    assert store_factory().get_config().data["ticker"] == "SOXL"


# --- AC-19 — Decimal lossless round-trip ---------------------------------------------


def test_decimal_round_trips_losslessly(store: StateStorePort) -> None:
    store.apply_strategy_delta(
        "mab",
        {"avg_price": Decimal("82.55"), "seed_remaining": Decimal("0.10")},
        expected_version=0,
    )
    data = store.load_strategy_state("mab").data
    assert data["avg_price"] == Decimal("82.55")
    assert data["seed_remaining"] == Decimal("0.10")
    assert str(data["seed_remaining"]) == "0.10"  # canonical string, never binary float


# --- AC-16 — structural Protocol satisfaction ----------------------------------------


def test_store_satisfies_protocol(store: StateStorePort) -> None:
    assert isinstance(store, StateStorePort)
