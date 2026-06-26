"""Tests for the dry-run (default-ON) manager path (REQ-ORDER-001-R2).

@TEST:SPEC-ORDER-001

Under dry-run the manager records an inspectable OrderPlan and calls NO port; a
spy port asserts zero place_order calls. dry-run is the default and is fully
deterministic across repeated calls.
"""

from __future__ import annotations

from decimal import Decimal

from ballast.core.models import Order, OrderType, Side
from ballast.orders.manager import OrderManager
from ballast.orders.models import OrderKind, SubmissionStatus, Tif
from ballast.orders.recording import RecordingBrokerOrderPort


def _orders() -> list[Order]:
    return [
        Order(
            side=Side.BUY,
            ticker="QLD",
            qty=Decimal("3"),
            limit_price=Decimal("80.00"),
            order_type=OrderType.RESERVED_LIMIT,
            account_seq="acc-1",
        ),
        Order(
            side=Side.SELL,
            ticker="SOXL",
            qty=Decimal("10"),
            limit_price=None,
            order_type=OrderType.LOC,
            account_seq="acc-2",
        ),
    ]


# AC-6 — dry-run records the plan as RECORDED and never calls the port.
def test_dry_run_records_plan_and_never_calls_port() -> None:
    spy = RecordingBrokerOrderPort()
    manager = OrderManager()
    plan = manager.place(
        _orders(),
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=True,
        kill_switch=False,
        port=spy,
    )
    assert all(r.status == SubmissionStatus.RECORDED for r in plan.results)
    assert spy.recorded_intents == ()  # port never invoked
    assert len(plan.intents) == 2
    assert len(plan.results) == 2


# AC-6 — the recorded plan exposes the deterministic id and mapped kind/tif/price.
def test_dry_run_plan_is_inspectable() -> None:
    manager = OrderManager()
    plan = manager.place(
        _orders(),
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=True,
        kill_switch=False,
    )
    reserved, loc = plan.intents
    assert reserved.kind == OrderKind.LIMIT and reserved.tif == Tif.DAY
    assert reserved.limit_price == Decimal("80.00")
    assert reserved.client_order_id  # deterministic id present
    assert loc.kind == OrderKind.LIMIT and loc.tif == Tif.CLS
    assert loc.limit_price is None


# AC-6 — dry-run works without any port (port may be None).
def test_dry_run_without_port() -> None:
    manager = OrderManager()
    plan = manager.place(
        _orders(),
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=True,
        kill_switch=False,
        port=None,
    )
    assert all(r.status == SubmissionStatus.RECORDED for r in plan.results)


# AC-7 — dry-run is deterministic across repeated calls and submits nothing.
def test_dry_run_is_deterministic_across_calls() -> None:
    spy = RecordingBrokerOrderPort()
    manager = OrderManager()
    plan_a = manager.place(
        _orders(), ns="vr", cycle_key="2026-06-26", dry_run=True, kill_switch=False, port=spy
    )
    plan_b = manager.place(
        _orders(), ns="vr", cycle_key="2026-06-26", dry_run=True, kill_switch=False, port=spy
    )
    ids_a = [i.client_order_id for i in plan_a.intents]
    ids_b = [i.client_order_id for i in plan_b.intents]
    assert ids_a == ids_b
    assert spy.recorded_intents == ()  # still nothing submitted
