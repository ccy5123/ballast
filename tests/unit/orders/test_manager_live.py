"""Tests for the live submission path and safety guards in the manager (R3/R4).

@TEST:SPEC-ORDER-001

The live path is exercised ONLY through in-memory ports (no network). Covers
submit-once + SUBMITTED tracking, idempotent dedup (DUPLICATE), FAILED on port
raise, kill-switch block-all precedence, and the position-cap clamp/block.
"""

from __future__ import annotations

from decimal import Decimal

from ballast.core.models import Order, OrderType, Side
from ballast.orders.manager import OrderManager
from ballast.orders.models import OrderIntent, SubmissionResult, SubmissionStatus
from ballast.orders.ports import BrokerOrderPort
from ballast.orders.recording import RecordingBrokerOrderPort


def _buy(qty: str = "3", ticker: str = "QLD", account: str = "acc-1") -> Order:
    return Order(
        side=Side.BUY,
        ticker=ticker,
        qty=Decimal(qty),
        limit_price=Decimal("10.00"),
        order_type=OrderType.RESERVED_LIMIT,
        account_seq=account,
    )


class _RaisingPort:
    """A BrokerOrderPort whose place_order always raises (live failure path)."""

    def place_order(self, intent: OrderIntent) -> SubmissionResult:
        raise RuntimeError("broker rejected the order")

    def cancel_order(self, account_seq: str, client_order_id: str) -> SubmissionResult:
        raise RuntimeError("broker rejected the cancel")


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


# AC-8 — live submit calls the port exactly once and tracks SUBMITTED.
def test_live_submit_calls_port_once_and_tracks_submitted() -> None:
    port = RecordingBrokerOrderPort()
    manager = OrderManager()
    plan = manager.place(
        [_buy()], ns="vr", cycle_key="2026-06-26", dry_run=False, kill_switch=False, port=port
    )
    (result,) = plan.results
    assert result.status == SubmissionStatus.SUBMITTED
    assert result.broker_order_id is not None
    assert len(port.recorded_intents) == 1
    assert manager.status_of(result.client_order_id) == SubmissionStatus.SUBMITTED


# AC-9 — re-placing the same client_order_id yields DUPLICATE, no second call.
def test_live_dedup_returns_duplicate() -> None:
    port = RecordingBrokerOrderPort()
    manager = OrderManager()
    first = manager.place(
        [_buy()], ns="vr", cycle_key="2026-06-26", dry_run=False, kill_switch=False, port=port
    )
    second = manager.place(
        [_buy()], ns="vr", cycle_key="2026-06-26", dry_run=False, kill_switch=False, port=port
    )
    assert first.results[0].status == SubmissionStatus.SUBMITTED
    assert second.results[0].status == SubmissionStatus.DUPLICATE
    # exactly one place_order issued for that key across both placements.
    assert len(port.recorded_intents) == 1


# AC-10 — a port that raises records FAILED with a reason, no infinite retry.
def test_live_port_raise_records_failed() -> None:
    manager = OrderManager()
    plan = manager.place(
        [_buy()],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=False,
        kill_switch=False,
        port=_RaisingPort(),
    )
    (result,) = plan.results
    assert result.status == SubmissionStatus.FAILED
    assert result.reason
    assert manager.status_of(result.client_order_id) == SubmissionStatus.FAILED


# AC-10 — a port that returns FAILED is recorded as FAILED with its reason.
def test_live_port_returns_failed() -> None:
    manager = OrderManager()
    plan = manager.place(
        [_buy()],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=False,
        kill_switch=False,
        port=_FailingPort(),
    )
    (result,) = plan.results
    assert result.status == SubmissionStatus.FAILED
    assert result.reason == "rejected by venue"


# AC-10 — a FAILED key may be retried explicitly; success then records SUBMITTED.
def test_live_failed_key_can_be_retried() -> None:
    port = RecordingBrokerOrderPort()
    manager = OrderManager()
    # first attempt fails.
    manager.place(
        [_buy()],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=False,
        kill_switch=False,
        port=_RaisingPort(),
    )
    # explicit retry against a working port succeeds (FAILED key is not pinned).
    plan = manager.place(
        [_buy()], ns="vr", cycle_key="2026-06-26", dry_run=False, kill_switch=False, port=port
    )
    assert plan.results[0].status == SubmissionStatus.SUBMITTED
    assert len(port.recorded_intents) == 1


# AC-11 — kill-switch blocks everything (live), nothing submitted.
def test_kill_switch_blocks_live() -> None:
    port = RecordingBrokerOrderPort()
    manager = OrderManager()
    plan = manager.place(
        [_buy(), _buy(ticker="SOXL")],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=False,
        kill_switch=True,
        port=port,
    )
    assert all(r.status == SubmissionStatus.BLOCKED for r in plan.results)
    assert all("kill-switch" in (r.reason or "") for r in plan.results)
    assert port.recorded_intents == ()


# AC-11 — kill-switch takes precedence over dry-run (blocks even recording).
def test_kill_switch_precedence_over_dry_run() -> None:
    spy = RecordingBrokerOrderPort()
    manager = OrderManager()
    plan = manager.place(
        [_buy()],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=True,  # would normally RECORD
        kill_switch=True,
        port=spy,
    )
    assert plan.results[0].status == SubmissionStatus.BLOCKED
    assert spy.recorded_intents == ()


# AC-12 — over-cap BUY is clamped down before live submit.
def test_live_position_cap_clamps_qty() -> None:
    port = RecordingBrokerOrderPort()
    manager = OrderManager()
    # cap = 1000 * 0.2 = 200; current = 100; ref price 10 => max qty 10 (from 15).
    plan = manager.place(
        [_buy(qty="15")],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=False,
        kill_switch=False,
        max_position_pct=Decimal("0.20"),
        base_values={"acc-1": Decimal("1000.00")},
        positions={"QLD": Decimal("100.00")},
        prices={"QLD": Decimal("10.00")},
        port=port,
    )
    (result,) = plan.results
    assert result.status == SubmissionStatus.SUBMITTED
    submitted_intent = port.recorded_intents[0]
    assert submitted_intent.qty == Decimal("10.00")  # clamped down, never up
    assert result.reason is not None and "clamp" in result.reason.lower()


# AC-13 — no-headroom blocks the order and submits nothing.
def test_live_position_cap_blocks_when_no_headroom() -> None:
    port = RecordingBrokerOrderPort()
    manager = OrderManager()
    plan = manager.place(
        [_buy(qty="1")],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=False,
        kill_switch=False,
        max_position_pct=Decimal("0.20"),
        base_values={"acc-1": Decimal("1000.00")},
        positions={"QLD": Decimal("200.00")},  # cap already met
        prices={"QLD": Decimal("10.00")},
        port=port,
    )
    assert plan.results[0].status == SubmissionStatus.BLOCKED
    assert "overflow" in (plan.results[0].reason or "").lower()
    assert port.recorded_intents == ()


# AC-14 — dry-run gate forces RECORDED even after a clamp (no submit).
def test_dry_run_gate_records_after_clamp() -> None:
    spy = RecordingBrokerOrderPort()
    manager = OrderManager()
    plan = manager.place(
        [_buy(qty="15")],
        ns="vr",
        cycle_key="2026-06-26",
        dry_run=True,
        kill_switch=False,
        max_position_pct=Decimal("0.20"),
        base_values={"acc-1": Decimal("1000.00")},
        positions={"QLD": Decimal("100.00")},
        prices={"QLD": Decimal("10.00")},
        port=spy,
    )
    (result,) = plan.results
    assert result.status == SubmissionStatus.RECORDED
    assert plan.intents[0].qty == Decimal("10.00")  # clamp still applied in preview
    assert spy.recorded_intents == ()


def test_status_of_unknown_key_is_none() -> None:
    manager = OrderManager()
    assert manager.status_of("never-seen") is None


def test_protocol_negative_membership() -> None:
    # a non-conforming object is not a BrokerOrderPort.
    assert not isinstance(object(), BrokerOrderPort)
