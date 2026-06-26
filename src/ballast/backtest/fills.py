"""Close-based fill model with no look-ahead (REQ-BACKTEST-001-R2).

``simulate_fill`` decides whether a CORE-001 :class:`Order` executes on a single
:class:`OHLCBar`, using ONLY that bar's ``open/high/low/close`` — it never reads a
later bar. Fill semantics (all close-based):

* ``LOC`` with a limit: a BUY fills at the bar ``close`` iff ``close <= limit``; a
  SELL fills at the bar ``close`` iff ``close >= limit``.
* ``LOC`` (or ``MARKET``) with ``limit_price is None`` — the MAB MOC-like
  quarter-sell — fills at the bar ``close`` unconditionally.
* ``RESERVED_LIMIT`` (VR): fills at the ``limit`` price iff ``low <= limit <= high``;
  otherwise it is a miss (``None``) — cancelled for the cycle, never carried over.

A miss returns ``None`` so the caller leaves the position untouched. Slippage is a
basis-point parameter: a BUY fills at ``price * (1 + bps/10000)``, a SELL at
``price * (1 - bps/10000)`` (default ``0`` bps). All prices are :class:`Decimal`.

@CODE:SPEC-BACKTEST-001
"""

from __future__ import annotations

from decimal import Decimal

from ballast.backtest.types import Fill, OHLCBar
from ballast.core.models import Order, OrderType, Side, quantize_money

_BPS_DENOMINATOR = Decimal("10000")
_ONE = Decimal("1")
_ZERO_COMMISSION = Decimal("0.00")


def _apply_slippage(price: Decimal, side: Side, slippage_bps: Decimal) -> Decimal:
    """Adjust ``price`` adversely by ``slippage_bps`` (BUY up, SELL down)."""
    if slippage_bps == Decimal("0"):
        return price
    factor = slippage_bps / _BPS_DENOMINATOR
    adjusted = price * (_ONE + factor) if side is Side.BUY else price * (_ONE - factor)
    return adjusted


def simulate_fill(
    order: Order, bar: OHLCBar, *, slippage_bps: Decimal = Decimal("0")
) -> Fill | None:
    """Return a :class:`Fill` for ``order`` on ``bar``, or ``None`` on a miss.

    Uses only ``bar``'s own fields (no look-ahead). The returned ``Fill`` carries a
    zero commission placeholder; the engine stamps the real per-trade commission
    from its :class:`CostModel` when it applies the fill.
    """
    fill_price = _resolve_fill_price(order, bar)
    if fill_price is None:
        return None
    fill_price = _apply_slippage(fill_price, order.side, slippage_bps)
    return Fill(
        order=order,
        fill_price=quantize_money(fill_price),
        qty=order.qty,
        date=bar.date,
        commission=_ZERO_COMMISSION,
    )


def _resolve_fill_price(order: Order, bar: OHLCBar) -> Decimal | None:
    """The execution price for ``order`` on ``bar``, or ``None`` if it does not fill."""
    if order.order_type is OrderType.RESERVED_LIMIT:
        limit = order.limit_price
        # A reserved-limit always carries a limit; fill only when the range crosses.
        if limit is not None and bar.low <= limit <= bar.high:
            return limit
        return None

    # LOC / MARKET: a None limit (MOC-like quarter-sell) always fills at the close.
    limit = order.limit_price
    if limit is None:
        return bar.close
    if order.side is Side.BUY:
        return bar.close if bar.close <= limit else None
    return bar.close if bar.close >= limit else None
