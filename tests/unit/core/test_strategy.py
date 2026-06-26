"""Tests for the Strategy protocol (REQ-CORE-001-R2).

@TEST:SPEC-CORE-001
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from ballast.core import (
    Config,
    Market,
    Order,
    OrderType,
    Side,
    State,
    Strategy,
)


class _StubStrategy:
    """A minimal conforming strategy used to check structural typing."""

    cadence: Literal["daily", "cycle"] = "daily"
    ns: str = "vr"

    def plan_orders(self, market: Market, state: State, cfg: Config) -> list[Order]:
        return [
            Order(
                side=Side.BUY,
                ticker=market.ticker,
                qty=Decimal("1"),
                limit_price=market.current_price,
                order_type=OrderType.RESERVED_LIMIT,
                account_seq="0001",
            )
        ]


def _accepts_strategy(strategy: Strategy) -> Strategy:
    """A typed sink: mypy --strict verifies the stub satisfies Strategy."""
    return strategy


# --------------------------------------------------------------------------- #
# Scenario 8 — Strategy protocol conformance (R2)
# --------------------------------------------------------------------------- #
def test_stub_satisfies_strategy_structurally() -> None:
    stub = _StubStrategy()
    # Static structural conformance is exercised through the typed sink.
    assert _accepts_strategy(stub) is stub


def test_stub_satisfies_strategy_isinstance() -> None:
    # Strategy is runtime_checkable, so isinstance works on shape.
    assert isinstance(_StubStrategy(), Strategy)


def test_strategy_attributes_present() -> None:
    stub = _StubStrategy()
    assert stub.cadence == "daily"
    assert stub.ns == "vr"


def test_stub_plan_orders_returns_orders(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    market = Market(
        ticker="TQQQ",
        current_price=Decimal("55.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )
    state = State(ns="vr", data={})
    orders = _StubStrategy().plan_orders(market, state, cfg)
    assert len(orders) == 1
    assert isinstance(orders[0], Order)
    assert orders[0].ticker == "TQQQ"


def test_non_conforming_object_is_not_a_strategy() -> None:
    class _NotAStrategy:
        cadence = "daily"
        # missing ns and plan_orders

    assert not isinstance(_NotAStrategy(), Strategy)
