"""Tests for the in-memory RecordingBrokerOrderPort (REQ-ORDER-001-R5).

@TEST:SPEC-ORDER-001

A network-free, credential-free idempotent stand-in for a broker write adapter.
Covers structural conformance to the runtime_checkable BrokerOrderPort, intent
recording, the synthetic broker_order_id, and idempotent DUPLICATE on repeat.
"""

from __future__ import annotations

from decimal import Decimal

from ballast.core.models import Side
from ballast.orders.models import OrderIntent, OrderKind, SubmissionStatus, Tif
from ballast.orders.ports import BrokerOrderPort
from ballast.orders.recording import RecordingBrokerOrderPort


def _intent(coid: str = "vr-20260626-abc") -> OrderIntent:
    return OrderIntent(
        client_order_id=coid,
        account_seq="acc-1",
        side=Side.BUY,
        ticker="QLD",
        qty=Decimal("3.00"),
        kind=OrderKind.LIMIT,
        tif=Tif.DAY,
        limit_price=Decimal("80.00"),
    )


# AC-15 — structurally satisfies the runtime_checkable BrokerOrderPort.
def test_recording_port_satisfies_protocol() -> None:
    port = RecordingBrokerOrderPort()
    assert isinstance(port, BrokerOrderPort)


# AC-15 — place_order records the intent and returns SUBMITTED + synthetic id.
def test_place_order_records_and_submits() -> None:
    port = RecordingBrokerOrderPort()
    intent = _intent()
    result = port.place_order(intent)
    assert result.status == SubmissionStatus.SUBMITTED
    assert result.broker_order_id == "rec-0"
    assert result.client_order_id == intent.client_order_id
    assert port.recorded_intents == (intent,)


def test_place_order_synthesizes_incrementing_broker_ids() -> None:
    port = RecordingBrokerOrderPort()
    r0 = port.place_order(_intent("vr-20260626-a"))
    r1 = port.place_order(_intent("vr-20260626-b"))
    assert r0.broker_order_id == "rec-0"
    assert r1.broker_order_id == "rec-1"
    assert len(port.recorded_intents) == 2


# AC-16 — repeating a client_order_id yields DUPLICATE; no duplicate intent.
def test_place_order_is_idempotent_on_repeat() -> None:
    port = RecordingBrokerOrderPort()
    intent = _intent()
    first = port.place_order(intent)
    second = port.place_order(intent)
    assert second.status == SubmissionStatus.DUPLICATE
    # the prior broker_order_id is carried through.
    assert second.broker_order_id == first.broker_order_id
    # no duplicate intent appended; results remain inspectable.
    assert port.recorded_intents == (intent,)
    assert len(port.results) == 1


def test_cancel_order_is_recorded() -> None:
    port = RecordingBrokerOrderPort()
    result = port.cancel_order("acc-1", "vr-20260626-abc")
    assert result.client_order_id == "vr-20260626-abc"
    assert port.cancels == (("acc-1", "vr-20260626-abc"),)
