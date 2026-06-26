"""CORE ``Order`` -> ``OrderIntent`` mapping + deterministic id (REQ-ORDER-001-R1).

@CODE:SPEC-ORDER-001

Applies the FD2 ``OrderType -> (kind, tif)`` table and derives the deterministic
``client_order_id`` (FD3). Both functions are pure: no randomness, no UUID, and
no wall-clock read. The clock is injected via ``cycle_key``.
"""

from __future__ import annotations

import hashlib
import re

from ballast.core.models import Order, OrderType, quantize_money
from ballast.orders.models import OrderIntent, OrderKind, Tif

# The Toss ``clientOrderId`` constraint (FD3): pattern + max length.
_COID_MAX_LEN = 36
_COID_TAIL_LEN = 16  # 16 hex chars (64 bits) of collision-resistant entropy.
_ILLEGAL_COID_CHARS = re.compile(r"[^a-zA-Z0-9\-_]")

# FD2: CORE OrderType -> (OrderKind, Tif). The only mapping P2 owns.
_TYPE_MAP: dict[OrderType, tuple[OrderKind, Tif]] = {
    OrderType.RESERVED_LIMIT: (OrderKind.LIMIT, Tif.DAY),
    OrderType.LOC: (OrderKind.LIMIT, Tif.CLS),
    OrderType.MARKET: (OrderKind.MARKET, Tif.DAY),
}


def _sanitize(text: str) -> str:
    """Reduce ``text`` to the ``^[a-zA-Z0-9\\-_]+$`` character set."""
    return _ILLEGAL_COID_CHARS.sub("", text)


def derive_client_order_id(ns: str, cycle_key: str, signature: str) -> str:
    """Derive a deterministic ``client_order_id`` from its inputs (FD3).

    Pure: identical ``(ns, cycle_key, signature)`` always yield a byte-identical
    string, with no randomness, UUID, or wall-clock read. The output matches
    ``^[a-zA-Z0-9\\-_]+$`` and is at most 36 chars, so P3 can pass it straight
    through to Toss's ``clientOrderId``.

    A readable ``ns``/``cycle_key`` prefix is preserved where it fits; a
    fixed-length hash tail over all three inputs guarantees that any input change
    (including ``ns``/``cycle_key``, even when the prefix is truncated) changes
    the id, while staying collision-resistant within a cycle.
    """
    digest = hashlib.sha256(f"{ns}|{cycle_key}|{signature}".encode()).hexdigest()
    tail = digest[:_COID_TAIL_LEN]
    readable = _sanitize(f"{ns}-{cycle_key}")
    # Reserve room for the separator and the tail; trim the readable prefix.
    max_readable = _COID_MAX_LEN - 1 - _COID_TAIL_LEN
    readable = readable[:max_readable]
    return f"{readable}-{tail}"


def _order_signature(order: Order, *, kind: OrderKind, tif: Tif, limit_price: object) -> str:
    """Build a stable digest of the order-defining fields (FD3).

    ``Decimal`` money values are stringified via their canonical 2-dp form so
    equal money values hash equally; the result is a hex SHA-256 digest.
    """
    raw = f"{order.side}|{order.ticker}|{order.qty}|{kind}|{tif}|{limit_price}|{order.account_seq}"
    return hashlib.sha256(raw.encode()).hexdigest()


def order_to_intent(order: Order, *, ns: str, cycle_key: str) -> OrderIntent:
    """Map a CORE ``Order`` to a broker-neutral ``OrderIntent`` (FD2/FD3).

    Applies the ``OrderType -> (kind, tif)`` table, carries ``limit_price`` only
    for LIMIT kinds (dropping it for MARKET), and sets the deterministic
    ``client_order_id``. A reserved-limit / DAY LIMIT requires a ``limit_price``;
    omitting it raises ``ValueError``.
    """
    kind, tif = _TYPE_MAP[order.order_type]

    if kind is OrderKind.MARKET:
        limit_price = None
    elif tif is Tif.DAY and order.limit_price is None:
        raise ValueError("a reserved_limit (LIMIT + DAY) order requires a non-null limit_price")
    else:
        # LIMIT: carry the price (re-normalized); None is allowed only for CLS.
        limit_price = quantize_money(order.limit_price) if order.limit_price is not None else None

    signature = _order_signature(order, kind=kind, tif=tif, limit_price=limit_price)
    client_order_id = derive_client_order_id(ns, cycle_key, signature)

    return OrderIntent(
        client_order_id=client_order_id,
        account_seq=order.account_seq,
        side=order.side,
        ticker=order.ticker,
        qty=order.qty,
        kind=kind,
        tif=tif,
        limit_price=limit_price,
    )
