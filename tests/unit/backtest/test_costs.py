"""Tests for the Decimal cost model (REQ-BACKTEST-001-R3).

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from ballast.backtest.costs import CostModel


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return -exponent


def test_defaults_match_spec() -> None:
    costs = CostModel()
    assert costs.commission_per_trade == Decimal("0.00")
    assert costs.tax_rate == Decimal("0.22")
    assert costs.annual_deduction_krw == Decimal("2500000")
    assert costs.slippage_bps == Decimal("0")
    for v in (costs.commission_per_trade, costs.tax_rate, costs.annual_deduction_krw):
        assert type(v) is Decimal


# --- Scenario 5 — commission per trade ------------------------------------- #
def test_realized_gain_subtracts_commission() -> None:
    costs = CostModel(commission_per_trade=Decimal("1.00"))
    # SELL 5 @ 110 with avg cost 100: (110-100)*5 - 1 = 49.00
    gain = costs.realized_gain_usd(
        sell_price=Decimal("110.00"),
        avg_cost=Decimal("100.00"),
        qty=Decimal("5.00"),
        commission=Decimal("1.00"),
    )
    assert gain == Decimal("49.00")
    assert type(gain) is Decimal


def test_realized_gain_default_commission_zero() -> None:
    costs = CostModel()
    gain = costs.realized_gain_usd(
        sell_price=Decimal("110.00"),
        avg_cost=Decimal("100.00"),
        qty=Decimal("5.00"),
        commission=Decimal("0.00"),
    )
    assert gain == Decimal("50.00")


# --- Scenario 6 — 22% tax above 2.5M KRW deduction ------------------------- #
def test_annual_tax_above_deduction_matches_worked_example() -> None:
    costs = CostModel()
    # deduction USD = 2_500_000 / 1300 = 1923.08; base = 3000 - 1923.08 = 1076.92
    # tax = 1076.92 * 0.22 = 236.92 (worked example in acceptance.md)
    tax = costs.annual_tax(
        year_net_gain_usd=Decimal("3000.00"),
        fx_at_realization=Decimal("1300.00"),
    )
    assert tax == Decimal("236.92")
    assert type(tax) is Decimal


def test_annual_tax_below_deduction_is_zero() -> None:
    costs = CostModel()
    # 1500 < 1923.08 -> taxable base max(0, ...) == 0 -> tax 0
    tax = costs.annual_tax(
        year_net_gain_usd=Decimal("1500.00"),
        fx_at_realization=Decimal("1300.00"),
    )
    assert tax == Decimal("0.00")


def test_annual_tax_loss_year_is_zero_never_negative() -> None:
    costs = CostModel()
    tax = costs.annual_tax(
        year_net_gain_usd=Decimal("-500.00"),
        fx_at_realization=Decimal("1300.00"),
    )
    assert tax == Decimal("0.00")


def test_annual_tax_is_independent_per_call_so_deduction_resets() -> None:
    # The deduction is per-call (per calendar year): two separate years each get
    # their own fresh 2.5M KRW deduction, so Y1 usage never bleeds into Y2.
    costs = CostModel()
    y1 = costs.annual_tax(Decimal("3000.00"), Decimal("1300.00"))
    y2 = costs.annual_tax(Decimal("3000.00"), Decimal("1300.00"))
    assert y1 == Decimal("236.92")
    assert y2 == Decimal("236.92")  # fresh deduction, not carried over from Y1


def test_annual_tax_uses_fx_for_deduction_conversion() -> None:
    costs = CostModel()
    # At fx 1000, deduction USD = 2_500_000 / 1000 = 2500.00; base = 3000 - 2500 = 500
    # tax = 500 * 0.22 = 110.00
    tax = costs.annual_tax(Decimal("3000.00"), Decimal("1000.00"))
    assert tax == Decimal("110.00")


# --- Invariant: tax non-negativity, two-place Decimal ---------------------- #
@given(
    gain=st.decimals(min_value="-100000", max_value="100000", places=2),
    fx=st.decimals(min_value="800", max_value="2000", places=2),
)
def test_annual_tax_is_non_negative_and_two_places(gain: Decimal, fx: Decimal) -> None:
    tax = CostModel().annual_tax(gain, fx)
    assert tax >= Decimal("0.00")
    assert type(tax) is Decimal
    assert _decimal_places(tax) == 2
