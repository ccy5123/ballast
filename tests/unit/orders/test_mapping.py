"""Tests for the CORE Order -> OrderIntent mapping and id derivation (R1).

@TEST:SPEC-ORDER-001

Covers FD2 (order_type -> kind/tif/limit_price) and FD3 (deterministic,
signature-sensitive client_order_id constrained to the Toss clientOrderId shape).
Property tests use hypothesis to assert the regex/length bound holds universally.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ballast.core.models import Order, OrderType, Side
from ballast.orders.mapping import derive_client_order_id, order_to_intent
from ballast.orders.models import OrderKind, Tif

_COID_RE = re.compile(r"^[a-zA-Z0-9\-_]+$")


# AC-1 — reserved_limit -> LIMIT + DAY, carrying the limit_price.
def test_reserved_limit_maps_to_limit_day(buy_reserved_limit: Order) -> None:
    intent = order_to_intent(buy_reserved_limit, ns="vr", cycle_key="2026-06-26")
    assert intent.kind == OrderKind.LIMIT
    assert intent.tif == Tif.DAY
    assert intent.limit_price == Decimal("80.00")
    assert isinstance(intent.limit_price, Decimal)
    assert intent.side == Side.BUY
    assert intent.ticker == "QLD"
    assert intent.qty == Decimal("3.00")
    assert intent.account_seq == "acc-1"
    assert _COID_RE.match(intent.client_order_id)
    assert len(intent.client_order_id) <= 36


# AC-2 — LOC -> LIMIT + CLS, limit_price may be None (MOC-style close).
def test_loc_maps_to_limit_cls_allows_none_price(sell_loc: Order) -> None:
    intent = order_to_intent(sell_loc, ns="mab", cycle_key="2026-06-26")
    assert intent.kind == OrderKind.LIMIT
    assert intent.tif == Tif.CLS
    assert intent.limit_price is None
    assert intent.side == Side.SELL
    assert intent.ticker == "SOXL"


# AC-3 — market -> MARKET + DAY and drops limit_price.
def test_market_maps_to_market_day_drops_price(buy_market: Order) -> None:
    intent = order_to_intent(buy_market, ns="vr", cycle_key="2026-06-26")
    assert intent.kind == OrderKind.MARKET
    assert intent.tif == Tif.DAY
    assert intent.limit_price is None


# AC-4 — LIMIT + DAY without a limit_price is rejected.
def test_reserved_limit_without_price_raises() -> None:
    order = Order(
        side=Side.BUY,
        ticker="QLD",
        qty=Decimal("3"),
        limit_price=None,
        order_type=OrderType.RESERVED_LIMIT,
        account_seq="acc-1",
    )
    with pytest.raises(ValueError, match="limit_price"):
        order_to_intent(order, ns="vr", cycle_key="2026-06-26")


# AC-5 — determinism: identical inputs => byte-identical id.
def test_client_order_id_is_deterministic(buy_reserved_limit: Order) -> None:
    a = order_to_intent(buy_reserved_limit, ns="vr", cycle_key="2026-06-26")
    b = order_to_intent(buy_reserved_limit, ns="vr", cycle_key="2026-06-26")
    assert a.client_order_id == b.client_order_id


# AC-5 — a qty change => a different id.
def test_client_order_id_changes_with_qty() -> None:
    def _order(qty: str) -> Order:
        return Order(
            side=Side.BUY,
            ticker="QLD",
            qty=Decimal(qty),
            limit_price=Decimal("80.00"),
            order_type=OrderType.RESERVED_LIMIT,
            account_seq="acc-1",
        )

    o3 = _order("3")
    o4 = _order("4")
    id3 = order_to_intent(o3, ns="vr", cycle_key="2026-06-26").client_order_id
    id4 = order_to_intent(o4, ns="vr", cycle_key="2026-06-26").client_order_id
    assert id3 != id4


# AC-5 — a cycle_key change => a different id.
def test_client_order_id_changes_with_cycle_key(buy_reserved_limit: Order) -> None:
    a = order_to_intent(buy_reserved_limit, ns="vr", cycle_key="2026-06-26")
    b = order_to_intent(buy_reserved_limit, ns="vr", cycle_key="2026-06-27")
    assert a.client_order_id != b.client_order_id


# AC-5 — an ns change => a different id.
def test_client_order_id_changes_with_ns(buy_reserved_limit: Order) -> None:
    a = order_to_intent(buy_reserved_limit, ns="vr", cycle_key="2026-06-26")
    b = order_to_intent(buy_reserved_limit, ns="mab", cycle_key="2026-06-26")
    assert a.client_order_id != b.client_order_id


def test_derive_client_order_id_is_pure_and_repeatable() -> None:
    a = derive_client_order_id("vr", "2026-06-26", "deadbeef")
    b = derive_client_order_id("vr", "2026-06-26", "deadbeef")
    assert a == b
    assert _COID_RE.match(a)
    assert len(a) <= 36


def test_derive_client_order_id_signature_sensitive() -> None:
    a = derive_client_order_id("vr", "2026-06-26", "sig-a")
    b = derive_client_order_id("vr", "2026-06-26", "sig-b")
    assert a != b


def test_derive_client_order_id_sanitizes_illegal_chars() -> None:
    # ns/cycle_key with illegal characters still produce a valid id.
    coid = derive_client_order_id("v r/!", "2026/06/26 12:00", "sig")
    assert _COID_RE.match(coid)
    assert len(coid) <= 36


# AC-5 — property: every produced id matches the regex and length bound.
@given(
    ns=st.text(min_size=0, max_size=40),
    cycle_key=st.text(min_size=0, max_size=40),
    signature=st.text(alphabet="0123456789abcdef", min_size=1, max_size=64),
)
def test_derive_client_order_id_always_valid(ns: str, cycle_key: str, signature: str) -> None:
    coid = derive_client_order_id(ns, cycle_key, signature)
    assert _COID_RE.match(coid), coid
    assert len(coid) <= 36
