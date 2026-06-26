"""Tests for the immutable domain types (REQ-CORE-001-R1).

@TEST:SPEC-CORE-001
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ballast.core import (
    Decision,
    DecisionSide,
    InstrumentMeta,
    Market,
    Order,
    OrderType,
    Side,
    State,
    quantize_money,
)


def decimal_places(value: Decimal) -> int:
    """Number of fractional digits of a finite ``Decimal``."""
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)  # finite Decimal: exponent is an int
    return -exponent


# --------------------------------------------------------------------------- #
# Scenario 4 — Order/money uses Decimal rounded to 2 places (R1 / R5)
# --------------------------------------------------------------------------- #
def test_order_quantizes_qty_and_price_half_up() -> None:
    order = Order(
        side=Side.BUY,
        ticker="QLD",
        qty=Decimal("3.005"),
        limit_price=Decimal("42.119"),
        order_type=OrderType.RESERVED_LIMIT,
        account_seq="0001",
    )
    assert order.qty == Decimal("3.01")
    assert order.limit_price == Decimal("42.12")


def test_order_money_fields_are_decimal_never_float() -> None:
    order = Order(
        side=Side.BUY,
        ticker="QLD",
        qty=Decimal("3.005"),
        limit_price=Decimal("42.119"),
        order_type=OrderType.RESERVED_LIMIT,
        account_seq="0001",
    )
    assert type(order.qty) is Decimal
    assert type(order.limit_price) is Decimal


def test_order_quantization_is_idempotent() -> None:
    once = Order(
        side=Side.SELL,
        ticker="TQQQ",
        qty=Decimal("1.20"),
        limit_price=Decimal("10.00"),
        order_type=OrderType.LOC,
        account_seq="0002",
    )
    twice = Order(
        side=once.side,
        ticker=once.ticker,
        qty=once.qty,
        limit_price=once.limit_price,
        order_type=once.order_type,
        account_seq=once.account_seq,
    )
    assert once.qty == twice.qty == Decimal("1.20")
    assert once.limit_price == twice.limit_price == Decimal("10.00")


def test_order_allows_none_limit_price() -> None:
    order = Order(
        side=Side.BUY,
        ticker="TQQQ",
        qty=Decimal("2"),
        limit_price=None,
        order_type=OrderType.MARKET,
        account_seq="0001",
    )
    assert order.limit_price is None
    assert order.qty == Decimal("2.00")


# --------------------------------------------------------------------------- #
# Immutability of all five domain types
# --------------------------------------------------------------------------- #
def test_order_is_immutable() -> None:
    order = Order(
        side=Side.BUY,
        ticker="TQQQ",
        qty=Decimal("1"),
        limit_price=None,
        order_type=OrderType.MARKET,
        account_seq="0001",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        order.qty = Decimal("2")  # type: ignore[misc]


def test_decision_is_immutable_and_quantizes() -> None:
    decision = Decision(side=DecisionSide.BUY, target_amount=Decimal("100.005"))
    assert decision.target_amount == Decimal("100.01")
    assert type(decision.target_amount) is Decimal
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.target_amount = Decimal("0")  # type: ignore[misc]


def test_decision_supports_hold() -> None:
    decision = Decision(side=DecisionSide.HOLD, target_amount=Decimal("0"))
    assert decision.side is DecisionSide.HOLD
    assert decision.target_amount == Decimal("0.00")


def test_market_is_immutable_and_quantizes_money() -> None:
    market = Market(
        ticker="TQQQ",
        current_price=Decimal("55.555"),
        fx_rate=Decimal("1300.005"),
        is_open=True,
        is_holiday=False,
    )
    assert market.current_price == Decimal("55.56")
    assert market.fx_rate == Decimal("1300.01")
    assert type(market.current_price) is Decimal
    with pytest.raises(dataclasses.FrozenInstanceError):
        market.current_price = Decimal("0")  # type: ignore[misc]


def test_state_is_immutable_and_namespaced() -> None:
    state = State(ns="vr", data={"pool": Decimal("10.00")})
    assert state.ns == "vr"
    assert state.data["pool"] == Decimal("10.00")
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.ns = "mab"  # type: ignore[misc]


def test_instrument_meta_is_immutable() -> None:
    meta = InstrumentMeta(
        ticker="TQQQ",
        leverage=3,
        underlying="NDX",
        default_target_pct=Decimal("0.50"),
        default_band=Decimal("0.15"),
    )
    assert meta.leverage == 3
    assert meta.default_target_pct == Decimal("0.50")
    assert type(meta.default_target_pct) is Decimal
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.leverage = 1  # type: ignore[misc]


def test_instrument_meta_allows_none_defaults() -> None:
    meta = InstrumentMeta(
        ticker="TQQQ",
        leverage=3,
        underlying="NDX",
        default_target_pct=None,
        default_band=None,
    )
    assert meta.default_target_pct is None
    assert meta.default_band is None


# --------------------------------------------------------------------------- #
# quantize_money helper
# --------------------------------------------------------------------------- #
def test_quantize_money_rounds_half_up() -> None:
    assert quantize_money(Decimal("3.005")) == Decimal("3.01")
    assert quantize_money(Decimal("42.119")) == Decimal("42.12")
    assert quantize_money(Decimal("2.5")) == Decimal("2.50")


def test_quantize_money_custom_digits() -> None:
    assert quantize_money(Decimal("1.23456"), digits=3) == Decimal("1.235")


def test_quantize_money_returns_decimal() -> None:
    assert type(quantize_money(Decimal("1"))) is Decimal


# --------------------------------------------------------------------------- #
# Property-based invariants (hypothesis) — quantization (R1 / R5)
# --------------------------------------------------------------------------- #
@given(
    value=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    )
)
def test_quantize_is_idempotent(value: Decimal) -> None:
    once = quantize_money(value)
    twice = quantize_money(once)
    assert once == twice


@given(
    value=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    )
)
def test_quantize_yields_exactly_two_places(value: Decimal) -> None:
    result = quantize_money(value)
    assert decimal_places(result) == 2


@given(
    value=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    )
)
def test_quantize_never_returns_float(value: Decimal) -> None:
    assert type(quantize_money(value)) is Decimal


@given(
    qty=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("100000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    ),
    price=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("100000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    ),
)
def test_order_fields_always_two_places(qty: Decimal, price: Decimal) -> None:
    order = Order(
        side=Side.BUY,
        ticker="TQQQ",
        qty=qty,
        limit_price=price,
        order_type=OrderType.RESERVED_LIMIT,
        account_seq="0001",
    )
    assert decimal_places(order.qty) == 2
    assert order.limit_price is not None
    assert decimal_places(order.limit_price) == 2
    assert type(order.qty) is Decimal
    assert type(order.limit_price) is Decimal
