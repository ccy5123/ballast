"""Ordered safety guards for the Order Manager (REQ-ORDER-001-R4).

@CODE:SPEC-ORDER-001

Pure functions over injected inputs (no IO, no wall-clock read). They run in a
fixed order before any recording or live submission: kill-switch (precedence
over everything, FD7) -> max_position_pct clamp/block (never increases qty, FD8)
-> dry-run gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from ballast.core.models import Side, quantize_money
from ballast.orders.models import OrderIntent

_KILL_SWITCH_REASON = "kill-switch"
_OVERFLOW_REASON = "max_position_pct overflow"


class GuardAction(StrEnum):
    """The decision a guard returns for an order."""

    PASS = "PASS"  # leave the order unchanged
    CLAMP = "CLAMP"  # continue with a reduced qty
    BLOCK = "BLOCK"  # do not record or submit this order


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """A guard's decision: an action, an optional clamped qty, and a reason."""

    action: GuardAction
    qty: Decimal | None = None
    reason: str | None = None


def kill_switch_guard(intent: OrderIntent, *, engaged: bool) -> GuardOutcome:
    """Block every order while the global kill-switch is engaged (FD7)."""
    if engaged:
        return GuardOutcome(action=GuardAction.BLOCK, reason=_KILL_SWITCH_REASON)
    return GuardOutcome(action=GuardAction.PASS, qty=intent.qty)


def position_cap_guard(
    intent: OrderIntent,
    *,
    current_position_value: Decimal,
    base_value: Decimal,
    max_position_pct: Decimal,
    reference_price: Decimal,
) -> GuardOutcome:
    """Clamp or block a BUY that would breach ``max_position_pct`` (FD8).

    Only BUYs that increase a position are constrained. The cap is
    ``quantize_money(base_value * max_position_pct)``; an order with no headroom
    is BLOCKED, an order that fits PASSes unchanged, and an over-cap order is
    CLAMPed to the largest qty that fits (floored, never rounded up). A clamp to
    zero degrades to BLOCK. The clamp never increases qty.
    """
    if intent.side is not Side.BUY:
        return GuardOutcome(action=GuardAction.PASS, qty=intent.qty)

    cap_value = quantize_money(base_value * max_position_pct)
    headroom = cap_value - current_position_value
    if headroom <= 0:
        return GuardOutcome(action=GuardAction.BLOCK, reason=_OVERFLOW_REASON)

    order_value = intent.qty * reference_price
    if order_value <= headroom:
        return GuardOutcome(action=GuardAction.PASS, qty=intent.qty)

    # Floor the affordable qty to 2 dp so the clamp never rounds up / overshoots.
    max_qty = (headroom / reference_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if max_qty <= 0:
        return GuardOutcome(action=GuardAction.BLOCK, reason=_OVERFLOW_REASON)

    return GuardOutcome(
        action=GuardAction.CLAMP,
        qty=max_qty,
        reason=f"clamped {intent.qty}->{max_qty}",
    )


def dry_run_route(*, dry_run: bool) -> bool:
    """Return ``True`` for the live (submit) path, ``False`` for record-only."""
    return not dry_run
