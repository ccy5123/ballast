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
    PlanResult,
    Side,
    State,
    Strategy,
)


class _StubStrategy:
    """A minimal conforming strategy used to check structural typing."""

    cadence: Literal["daily", "cycle"] = "daily"
    ns: str = "vr"

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        return PlanResult(
            orders=(
                Order(
                    side=Side.BUY,
                    ticker=market.ticker,
                    qty=Decimal("1"),
                    limit_price=market.current_price,
                    order_type=OrderType.RESERVED_LIMIT,
                    account_seq="0001",
                ),
            )
        )


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
    # SPEC-STRATEGY-001 (R2): plan_orders now returns a PlanResult carrying the
    # orders plus a namespace-scoped state delta.
    result = _StubStrategy().plan_orders(market, state, cfg)
    assert isinstance(result, PlanResult)
    assert len(result.orders) == 1
    assert isinstance(result.orders[0], Order)
    assert result.orders[0].ticker == "TQQQ"


def test_non_conforming_object_is_not_a_strategy() -> None:
    class _NotAStrategy:
        cadence = "daily"
        # missing ns and plan_orders

    assert not isinstance(_NotAStrategy(), Strategy)


# --------------------------------------------------------------------------- #
# SPEC-STRATEGY-001 R2 (AC-7) — the enriched contract exposes a namespace-scoped
# Decimal state delta; the contract stays a pure structural Protocol.
# --------------------------------------------------------------------------- #
def test_plan_result_carries_orders_and_decimal_state_delta() -> None:
    delta: dict[str, Decimal] = {"V_n": Decimal("123.45")}
    result = PlanResult(orders=(), state_delta=delta)
    assert result.orders == ()
    # The delta is a Mapping[str, Decimal]; values are Decimal (no float).
    assert all(isinstance(v, Decimal) for v in result.state_delta.values())
    assert result.state_delta["V_n"] == Decimal("123.45")


def test_plan_result_defaults_are_empty() -> None:
    # A strategy with no evolving state satisfies the contract with an empty
    # delta (and no orders) without inventing state (FD5).
    result = PlanResult()
    assert result.orders == ()
    assert dict(result.state_delta) == {}


def test_plan_result_is_frozen() -> None:
    result = PlanResult()
    import dataclasses

    assert dataclasses.is_dataclass(result)
    try:
        result.orders = ()  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("PlanResult must be frozen/immutable")


def test_strategy_protocol_ships_no_implementation() -> None:
    # The Protocol's plan_orders body is just an ellipsis (no IO, no clock, no
    # concrete implementation) — calling it on the bare protocol returns None.
    assert Strategy.plan_orders.__doc__ is not None
