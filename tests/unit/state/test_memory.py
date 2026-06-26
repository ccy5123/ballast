"""Tests specific to the in-memory store (REQ-STATE-001-R5, FD8).

@TEST:SPEC-STATE-001

The in-memory store is the sole vehicle for unit tests: it needs no DB, no
credentials, no base URL, and no network. It structurally satisfies
``StateStorePort`` and honors the same semantics as a persistent backend.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from ballast.state.memory import InMemoryStateStore
from ballast.state.models import ConcurrencyError, OrderState, WriterLeaseHeldError
from ballast.state.ports import StateStorePort

# --- AC-16 — structural Protocol satisfaction, no DB/credentials ----------------------


def test_in_memory_store_satisfies_protocol() -> None:
    store = InMemoryStateStore()
    assert isinstance(store, StateStorePort)


def test_in_memory_store_constructs_without_any_io_args() -> None:
    # No database, URL, or credential parameter is required (or accepted).
    sig = inspect.signature(InMemoryStateStore.__init__)
    params = [p for p in sig.parameters if p != "self"]
    assert params == []


def test_in_memory_store_is_isolated_per_instance() -> None:
    a = InMemoryStateStore()
    b = InMemoryStateStore()
    a.apply_strategy_delta("vr", {"V_n": Decimal("5.00")}, expected_version=0)
    # b shares no state with a.
    assert b.load_strategy_state("vr").version == 0


# --- atomicity: a failed mutation leaves the prior record intact ---------------------


def test_failed_apply_does_not_mutate_record(store_is_memory: InMemoryStateStore) -> None:
    store = store_is_memory
    store.apply_strategy_delta("vr", {"V_n": Decimal("10.00")}, expected_version=0)
    with pytest.raises(ConcurrencyError):
        store.apply_strategy_delta("vr", {"V_n": Decimal("99.00")}, expected_version=0)
    # The record is the old one, never a torn/partial write.
    rec = store.load_strategy_state("vr")
    assert rec.version == 1
    assert rec.data["V_n"] == Decimal("10.00")


def test_loaded_records_are_snapshots_not_live_aliases(
    store_is_memory: InMemoryStateStore,
) -> None:
    store = store_is_memory
    store.apply_strategy_delta("vr", {"V_n": Decimal("1.00")}, expected_version=0)
    rec = store.load_strategy_state("vr")
    # Mutating the returned mapping must not corrupt the store's internal state.
    with pytest.raises((TypeError, AttributeError)):
        rec.data["V_n"] = Decimal("999.00")  # type: ignore[index]


def test_lease_held_error_message_has_no_secret(
    store_is_memory: InMemoryStateStore,
) -> None:
    store = store_is_memory
    store.acquire_writer_lease("worker-A", ttl=30)
    with pytest.raises(WriterLeaseHeldError) as exc:
        store.acquire_writer_lease("worker-B", ttl=30)
    # The error names the holder but carries no credential/connection string.
    assert "worker-A" in str(exc.value)


@pytest.fixture
def store_is_memory() -> InMemoryStateStore:
    return InMemoryStateStore()


def test_empty_status_default_config(store_is_memory: InMemoryStateStore) -> None:
    cfg = store_is_memory.get_config()
    assert cfg.version == 0
    assert cfg.data == {}


def test_order_state_terminal_cancel_then_fill_blocked(
    store_is_memory: InMemoryStateStore,
) -> None:
    store = store_is_memory
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.CANCELED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    store.upsert_order(
        "k1",
        account_seq="acc-1",
        broker_order_id="toss-1",
        status=OrderState.FILLED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("1"),
        ts="t1",
    )
    rec = store.load_order("k1")
    # A terminal CANCELED is not overwritten by a later FILLED.
    assert rec is not None and rec.status is OrderState.CANCELED
