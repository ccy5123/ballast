"""Tests for the ordered safety guards (REQ-ORDER-001-R4).

@TEST:SPEC-ORDER-001

Guards are pure functions over injected inputs (no IO). Covers the kill-switch
(block-all, precedence), the max_position_pct clamp/block (never increases qty),
and the dry-run gate routing.
"""

from __future__ import annotations

from decimal import Decimal

from ballast.core.models import Side
from ballast.orders.guards import (
    GuardAction,
    dry_run_route,
    kill_switch_guard,
    position_cap_guard,
)
from ballast.orders.models import OrderIntent, OrderKind, Tif


def _buy(qty: str, ticker: str = "QLD") -> OrderIntent:
    return OrderIntent(
        client_order_id="vr-20260626-abc",
        account_seq="acc-1",
        side=Side.BUY,
        ticker=ticker,
        qty=Decimal(qty),
        kind=OrderKind.LIMIT,
        tif=Tif.DAY,
        limit_price=Decimal("10.00"),
    )


def _sell(qty: str) -> OrderIntent:
    return OrderIntent(
        client_order_id="vr-20260626-xyz",
        account_seq="acc-1",
        side=Side.SELL,
        ticker="QLD",
        qty=Decimal(qty),
        kind=OrderKind.LIMIT,
        tif=Tif.CLS,
        limit_price=None,
    )


# AC-11 — kill-switch blocks; precedence handled by the manager ordering.
def test_kill_switch_engaged_blocks() -> None:
    outcome = kill_switch_guard(_buy("3"), engaged=True)
    assert outcome.action == GuardAction.BLOCK
    assert outcome.reason is not None
    assert "kill-switch" in outcome.reason


def test_kill_switch_disengaged_passes() -> None:
    outcome = kill_switch_guard(_buy("3"), engaged=False)
    assert outcome.action == GuardAction.PASS


# AC-12 — an over-cap BUY is clamped down (never up), value <= cap.
def test_position_cap_clamps_over_cap_buy_down() -> None:
    # cap = 1000 * 0.20 = 200; current = 100 => headroom = 100.
    # order_value = 15 * 10 = 150 > headroom 100 => clamp to floor(100/10) = 10.
    outcome = position_cap_guard(
        _buy("15"),
        current_position_value=Decimal("100.00"),
        base_value=Decimal("1000.00"),
        max_position_pct=Decimal("0.20"),
        reference_price=Decimal("10.00"),
    )
    assert outcome.action == GuardAction.CLAMP
    assert outcome.qty == Decimal("10.00")
    assert outcome.qty <= Decimal("15")  # never increases
    # resulting position value <= cap (200).
    assert Decimal("100.00") + outcome.qty * Decimal("10.00") <= Decimal("200.00")
    assert outcome.reason is not None
    assert "clamp" in outcome.reason.lower()


def test_position_cap_passes_when_within_headroom() -> None:
    outcome = position_cap_guard(
        _buy("5"),
        current_position_value=Decimal("100.00"),
        base_value=Decimal("1000.00"),
        max_position_pct=Decimal("0.20"),
        reference_price=Decimal("10.00"),
    )
    assert outcome.action == GuardAction.PASS
    assert outcome.qty == Decimal("5.00")


# AC-12 — clamp floors (never rounds up) the fractional max qty.
def test_position_cap_clamp_floors_quantity() -> None:
    # headroom = 100; ref = 30 => max = floor(100/30) = 3.33 (ROUND_DOWN), not 3.34.
    outcome = position_cap_guard(
        _buy("10"),
        current_position_value=Decimal("100.00"),
        base_value=Decimal("1000.00"),
        max_position_pct=Decimal("0.20"),
        reference_price=Decimal("30.00"),
    )
    assert outcome.action == GuardAction.CLAMP
    assert outcome.qty == Decimal("3.33")
    assert Decimal("100.00") + outcome.qty * Decimal("30.00") <= Decimal("200.00")


# AC-13 — no headroom blocks with an overflow reason.
def test_position_cap_blocks_when_no_headroom() -> None:
    outcome = position_cap_guard(
        _buy("1"),
        current_position_value=Decimal("200.00"),  # cap already met
        base_value=Decimal("1000.00"),
        max_position_pct=Decimal("0.20"),
        reference_price=Decimal("10.00"),
    )
    assert outcome.action == GuardAction.BLOCK
    assert outcome.reason is not None
    assert "overflow" in outcome.reason.lower()


# AC-13 — a clamp that would reduce qty to 0 (sub-2dp headroom) degrades to BLOCK.
def test_position_cap_clamp_to_zero_degrades_to_block() -> None:
    # headroom = 0.04; ref = 10 => floor(0.04/10) = floor(0.004) = 0.00 => BLOCK.
    outcome = position_cap_guard(
        _buy("1"),
        current_position_value=Decimal("199.96"),
        base_value=Decimal("1000.00"),
        max_position_pct=Decimal("0.20"),
        reference_price=Decimal("10.00"),
    )
    assert outcome.action == GuardAction.BLOCK


# SELLs are not constrained by the position cap (they reduce a position).
def test_position_cap_ignores_sell() -> None:
    outcome = position_cap_guard(
        _sell("10"),
        current_position_value=Decimal("200.00"),
        base_value=Decimal("1000.00"),
        max_position_pct=Decimal("0.20"),
        reference_price=Decimal("10.00"),
    )
    assert outcome.action == GuardAction.PASS


# AC-14 — dry-run gate routes record-only vs live.
def test_dry_run_route() -> None:
    assert dry_run_route(dry_run=True) is False
    assert dry_run_route(dry_run=False) is True
