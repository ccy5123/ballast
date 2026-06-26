"""Tests for the VR (Value Rebalancing) strategy core (REQ-VR-001-R1..R5).

Mirrors ``src/ballast/core/vr.py``. Covers all eight acceptance scenarios
(``acceptance.md``) plus ``hypothesis`` property tests for financial invariants:
HOLD-inside-band, flow-sign monotonicity, 2-place quantization, and the
BUY-pool / SELL-holdings clamps.

@TEST:SPEC-VR-001
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from ballast.core import (
    Config,
    Decision,
    DecisionSide,
    Market,
    OrderType,
    PlanResult,
    Side,
    State,
    Strategy,
)
from ballast.core.vr import (
    VRStrategy,
    next_value,
    order_from_decision,
    rebalance_decision,
)

# --------------------------------------------------------------------------- #
# Decimal helpers for tests
# --------------------------------------------------------------------------- #
TWO_PLACES = Decimal("0.01")


def _is_two_place_decimal(value: Decimal) -> bool:
    """True iff ``value`` is a Decimal with exactly two fractional digits."""
    return isinstance(value, Decimal) and value.as_tuple().exponent == -2


# --------------------------------------------------------------------------- #
# Scenario 1 — Skill formula computes V2 from known inputs (REQ-VR-001-R1)
# --------------------------------------------------------------------------- #
def test_skill_formula_known_inputs() -> None:
    v2 = next_value(
        Decimal("1000.00"),
        Decimal("200.00"),
        Decimal("1100.00"),
        10,
        Decimal("50.00"),
        use_skill=True,
    )
    # 1000 + pool/G(20) + skill(+15.8113883...) + flow(50) = 1085.81
    assert v2 == Decimal("1085.81")
    assert type(v2) is Decimal


def test_skill_formula_result_is_two_place_decimal() -> None:
    v2 = next_value(
        Decimal("1000.00"),
        Decimal("200.00"),
        Decimal("1100.00"),
        10,
        Decimal("50.00"),
    )
    assert _is_two_place_decimal(v2)


def test_skill_correction_suppressed_on_crash() -> None:
    # Same inputs but E below V1 and no flow: skill correction is negative.
    v2 = next_value(
        Decimal("1000.00"),
        Decimal("200.00"),
        Decimal("900.00"),
        10,
        Decimal("0.00"),
        use_skill=True,
    )
    # 1000 + 20 + (-15.81...) + 0 = 1004.19 (climb suppressed on a crash)
    assert v2 == Decimal("1004.19")


# --------------------------------------------------------------------------- #
# Scenario 2 — Basic formula (provisional) path when use_skill is False
# (REQ-VR-001-R1, [T3])
# --------------------------------------------------------------------------- #
def test_basic_formula_provisional_r_placement() -> None:
    # PROVISIONAL [T3]: V2 = V1*(1 + r) + pool/G +- flow; skill term is absent.
    v2 = next_value(
        Decimal("1000.00"),
        Decimal("200.00"),
        Decimal("1100.00"),  # E is irrelevant to the basic formula
        10,
        Decimal("0.00"),
        use_skill=False,
        r=Decimal("0.01"),
    )
    # 1000 * 1.01 + 20 + 0 = 1030.00
    assert v2 == Decimal("1030.00")


def test_basic_formula_ignores_e() -> None:
    # The skill-correction term (and thus E) does not appear in the basic form.
    v2_high_e = next_value(
        Decimal("1000.00"),
        Decimal("200.00"),
        Decimal("5000.00"),
        10,
        Decimal("0.00"),
        use_skill=False,
        r=Decimal("0.01"),
    )
    v2_low_e = next_value(
        Decimal("1000.00"),
        Decimal("200.00"),
        Decimal("1.00"),
        10,
        Decimal("0.00"),
        use_skill=False,
        r=Decimal("0.01"),
    )
    assert v2_high_e == v2_low_e == Decimal("1030.00")


# --------------------------------------------------------------------------- #
# Scenario 3 — E above band => SELL with center target_amount = E - V
# (REQ-VR-001-R2)
# --------------------------------------------------------------------------- #
def test_e_above_band_sells_center_mode() -> None:
    decision = rebalance_decision(
        Decimal("1100.00"),
        Decimal("1000.00"),
        Decimal("0.05"),
        Decimal("0.05"),
        target_mode="center",
    )
    assert decision.side is DecisionSide.SELL
    assert decision.target_amount == Decimal("100.00")  # E - V


def test_e_above_band_sells_edge_mode() -> None:
    decision = rebalance_decision(
        Decimal("1100.00"),
        Decimal("1000.00"),
        Decimal("0.05"),
        Decimal("0.05"),
        target_mode="edge",
    )
    assert decision.side is DecisionSide.SELL
    # E - V*(1 + max_band) = 1100 - 1050 = 50
    assert decision.target_amount == Decimal("50.00")


# --------------------------------------------------------------------------- #
# Scenario 4 — E below band => BUY (REQ-VR-001-R2)
# --------------------------------------------------------------------------- #
def test_e_below_band_buys_center_mode() -> None:
    decision = rebalance_decision(
        Decimal("900.00"),
        Decimal("1000.00"),
        Decimal("0.05"),
        Decimal("0.05"),
        target_mode="center",
    )
    assert decision.side is DecisionSide.BUY
    assert decision.target_amount == Decimal("100.00")  # V - E


def test_e_below_band_buys_edge_mode() -> None:
    decision = rebalance_decision(
        Decimal("900.00"),
        Decimal("1000.00"),
        Decimal("0.05"),
        Decimal("0.05"),
        target_mode="edge",
    )
    assert decision.side is DecisionSide.BUY
    # V*(1 - min_band) - E = 950 - 900 = 50
    assert decision.target_amount == Decimal("50.00")


# --------------------------------------------------------------------------- #
# Scenario 5 — E inside band => HOLD, no order (REQ-VR-001-R2 / R5)
# --------------------------------------------------------------------------- #
def test_e_inside_band_holds() -> None:
    decision = rebalance_decision(
        Decimal("1020.00"),
        Decimal("1000.00"),
        Decimal("0.05"),
        Decimal("0.05"),
    )
    assert decision.side is DecisionSide.HOLD
    assert decision.target_amount == Decimal("0.00")


def test_hold_decision_yields_no_order() -> None:
    hold = rebalance_decision(
        Decimal("1020.00"),
        Decimal("1000.00"),
        Decimal("0.05"),
        Decimal("0.05"),
    )
    order = order_from_decision(
        hold,
        Decimal("50.00"),
        Decimal("10.00"),
        Decimal("1000.00"),
        ticker="TQQQ",
        account_seq="0001",
    )
    assert order is None


@pytest.mark.parametrize(
    "e",
    [Decimal("950.00"), Decimal("1050.00"), Decimal("1000.00")],
)
def test_band_edges_inclusive_hold(e: Decimal) -> None:
    # Both edges are inclusive: E == lower or E == upper still HOLDs.
    decision = rebalance_decision(
        e,
        Decimal("1000.00"),
        Decimal("0.05"),
        Decimal("0.05"),
    )
    assert decision.side is DecisionSide.HOLD


# --------------------------------------------------------------------------- #
# Scenario 6 — Order clamp: BUY exceeds pool, allow_fractional=False floors qty
# (REQ-VR-001-R3)
# --------------------------------------------------------------------------- #
def test_buy_clamped_to_pool_and_floored() -> None:
    buy = Decision(side=DecisionSide.BUY, target_amount=Decimal("100.00"))
    order = order_from_decision(
        buy,
        Decimal("50.00"),
        Decimal("10.00"),  # holdings
        Decimal("80.00"),  # pool (buying power)
        allow_fractional=False,
        ticker="TQQQ",
        account_seq="0001",
    )
    assert order is not None
    assert order.side is Side.BUY
    assert order.qty == Decimal("1.00")  # floor(80/50) = floor(1.6) = 1
    assert order.limit_price == Decimal("50.00")
    assert order.ticker == "TQQQ"
    assert order.account_seq == "0001"
    assert order.order_type is OrderType.RESERVED_LIMIT
    # Cash committed never exceeds the pool.
    assert order.qty * order.limit_price <= Decimal("80.00")


def test_buy_fractional_allowed_keeps_clamped_fraction() -> None:
    buy = Decision(side=DecisionSide.BUY, target_amount=Decimal("100.00"))
    order = order_from_decision(
        buy,
        Decimal("50.00"),
        Decimal("10.00"),
        Decimal("80.00"),
        allow_fractional=True,
        ticker="TQQQ",
        account_seq="0001",
    )
    assert order is not None
    assert order.qty == Decimal("1.60")  # clamped to pool, not floored
    assert order.qty * order.limit_price == Decimal("80.00")


def test_sell_clamped_to_holdings() -> None:
    sell = Decision(side=DecisionSide.SELL, target_amount=Decimal("1000.00"))
    order = order_from_decision(
        sell,
        Decimal("50.00"),
        Decimal("3.00"),  # holdings -> caps qty
        Decimal("0.00"),  # pool irrelevant for SELL
        allow_fractional=False,
        ticker="TQQQ",
        account_seq="0001",
    )
    assert order is not None
    assert order.side is Side.SELL
    # raw qty 1000/50 = 20, clamped to holdings 3.
    assert order.qty == Decimal("3.00")
    assert order.qty <= Decimal("3.00")


# --------------------------------------------------------------------------- #
# Scenario 7 — Edge case: qty rounds to 0 => None (REQ-VR-001-R3 / R5)
# --------------------------------------------------------------------------- #
def test_buy_qty_floors_to_zero_returns_none() -> None:
    buy = Decision(side=DecisionSide.BUY, target_amount=Decimal("20.00"))
    order = order_from_decision(
        buy,
        Decimal("50.00"),
        Decimal("0.00"),
        Decimal("1000.00"),
        allow_fractional=False,
        ticker="TQQQ",
        account_seq="0001",
    )
    # 20/50 = 0.4 -> floor 0 -> None (never a dust order).
    assert order is None


def test_sell_floors_to_zero_holdings_returns_none() -> None:
    sell = Decision(side=DecisionSide.SELL, target_amount=Decimal("1000.00"))
    order = order_from_decision(
        sell,
        Decimal("50.00"),
        Decimal("0.40"),  # holdings floor to 0
        Decimal("0.00"),
        allow_fractional=False,
        ticker="TQQQ",
        account_seq="0001",
    )
    assert order is None


def test_zero_target_amount_returns_none() -> None:
    buy = Decision(side=DecisionSide.BUY, target_amount=Decimal("0.00"))
    order = order_from_decision(
        buy,
        Decimal("50.00"),
        Decimal("10.00"),
        Decimal("1000.00"),
        ticker="TQQQ",
        account_seq="0001",
    )
    assert order is None


# --------------------------------------------------------------------------- #
# Scenario 8 — VRStrategy conforms and wires the pipeline (REQ-VR-001-R4)
# --------------------------------------------------------------------------- #
def _accepts_strategy(strategy: Strategy) -> Strategy:
    """Typed sink: mypy --strict verifies VRStrategy satisfies Strategy."""
    return strategy


def test_vrstrategy_attributes() -> None:
    strategy = VRStrategy()
    assert strategy.cadence == "cycle"
    assert strategy.ns == "vr"


def test_vrstrategy_conforms_to_protocol() -> None:
    strategy = VRStrategy()
    assert isinstance(strategy, Strategy)
    assert _accepts_strategy(strategy) is strategy


def test_vrstrategy_plan_orders_sells_when_e_above_band(
    example_config_path: Path,
) -> None:
    cfg = Config.load(example_config_path)
    # TQQQ default_band = 0.15 -> upper edge ~ V*1.15. Keep pool tiny so the line
    # stays near V_n=1000; qty=12 @ 100 -> E = 1200, above the upper band edge.
    market = Market(
        ticker="TQQQ",
        current_price=Decimal("100.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )
    state = State(
        ns="vr",
        data={
            "V_n": Decimal("1000.00"),
            "pool": Decimal("0.00"),
            "qty": Decimal("12.00"),
        },
    )
    result = VRStrategy().plan_orders(market, state, cfg)
    orders = result.orders
    # The advanced line stays near 1020 (skill correction only); E (1200) is
    # above V*(1.15), so a SELL order is produced.
    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert orders[0].order_type is OrderType.RESERVED_LIMIT
    assert orders[0].ticker == "TQQQ"
    assert orders[0].account_seq == "0001"


def test_vrstrategy_plan_orders_holds_returns_empty(
    example_config_path: Path,
) -> None:
    cfg = Config.load(example_config_path)
    market = Market(
        ticker="TQQQ",
        current_price=Decimal("10.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )
    # qty=100 @ 10 -> E = 1000, V_n grows via next_value but stays near E so HOLD.
    state = State(
        ns="vr",
        data={
            "V_n": Decimal("1000.00"),
            "pool": Decimal("0.00"),
            "qty": Decimal("100.00"),
        },
    )
    result = VRStrategy().plan_orders(market, state, cfg)
    assert result.orders == ()


def test_vrstrategy_plan_orders_is_pure(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    market = Market(
        ticker="TQQQ",
        current_price=Decimal("60.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )
    data = {
        "V_n": Decimal("1000.00"),
        "pool": Decimal("100000.00"),
        "qty": Decimal("100.00"),
    }
    state = State(ns="vr", data=data)
    VRStrategy().plan_orders(market, state, cfg)
    # plan_orders does not mutate its inputs.
    assert state.data == data
    assert data == {
        "V_n": Decimal("1000.00"),
        "pool": Decimal("100000.00"),
        "qty": Decimal("100.00"),
    }


# --------------------------------------------------------------------------- #
# SPEC-STRATEGY-001 R3 — VRStrategy surfaces the recomputed V_n through the
# enriched contract; single-cycle orders are unchanged (AC-8, AC-9).
# --------------------------------------------------------------------------- #
def _vr_seed_state() -> State:
    return State(
        ns="vr",
        data={
            "V_n": Decimal("1000.00"),
            "pool": Decimal("0.00"),
            "qty": Decimal("12.00"),
        },
    )


def _vr_market() -> Market:
    return Market(
        ticker="TQQQ",
        current_price=Decimal("100.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )


def test_vrstrategy_plan_orders_returns_plan_result(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    result = VRStrategy().plan_orders(_vr_market(), _vr_seed_state(), cfg)
    assert isinstance(result, PlanResult)


def test_vrstrategy_surfaces_recomputed_v_n_single_pass(example_config_path: Path) -> None:
    # AC-8: the delta is {"V_n": V2} where V2 == next_value(...) for THIS cycle —
    # the SAME value used to compute the rebalance decision (single-pass).
    cfg = Config.load(example_config_path)
    market = _vr_market()
    state = _vr_seed_state()

    result = VRStrategy().plan_orders(market, state, cfg)

    # Recompute V2 independently from the same inputs the strategy used.
    vr = cfg.strategies.vr
    e = state.data["qty"] * market.current_price
    expected_v2 = next_value(
        state.data["V_n"],
        state.data["pool"],
        e,
        vr.g,
        vr.flow,
        use_skill=vr.use_skill,
        r=vr.r,
    )
    assert set(result.state_delta) == {"V_n"}
    v2 = result.state_delta["V_n"]
    assert v2 == expected_v2
    assert isinstance(v2, Decimal)
    assert _is_two_place_decimal(v2)
    # It advanced past the seed (this is the value the engine will carry forward).
    assert v2 != Decimal("1000.00")


def test_vrstrategy_single_cycle_orders_unchanged_by_v_n_channel(
    example_config_path: Path,
) -> None:
    # AC-9: surfacing V_n does not alter the order(s) VR plans for one cycle. The
    # order tuple is identical to the order built directly from the same pipeline.
    cfg = Config.load(example_config_path)
    market = _vr_market()
    state = _vr_seed_state()

    result = VRStrategy().plan_orders(market, state, cfg)

    # Rebuild the expected order through the unchanged pipeline pieces.
    vr = cfg.strategies.vr
    e = state.data["qty"] * market.current_price
    v = next_value(
        state.data["V_n"], state.data["pool"], e, vr.g, vr.flow, use_skill=vr.use_skill, r=vr.r
    )
    from ballast.core.instrument import InstrumentRegistry
    from ballast.core.vr import order_from_decision, rebalance_decision

    base_band = InstrumentRegistry.from_config(cfg).resolve_band(vr.ticker, vr.band)
    min_band = vr.min_band if vr.min_band is not None else base_band
    max_band = vr.max_band if vr.max_band is not None else base_band
    decision = rebalance_decision(e, v, min_band, max_band, vr.target_mode)
    expected = order_from_decision(
        decision,
        market.current_price,
        state.data["qty"],
        state.data["pool"],
        allow_fractional=cfg.common.allow_fractional,
        ticker=vr.ticker,
        account_seq=vr.account_seq,
    )
    expected_orders = (expected,) if expected is not None else ()
    assert result.orders == expected_orders


def test_vrstrategy_hold_still_surfaces_v_n(example_config_path: Path) -> None:
    # Even when no order is planned (HOLD), the V_n channel still advances.
    cfg = Config.load(example_config_path)
    market = Market(
        ticker="TQQQ",
        current_price=Decimal("10.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )
    state = State(
        ns="vr",
        data={"V_n": Decimal("1000.00"), "pool": Decimal("0.00"), "qty": Decimal("100.00")},
    )
    result = VRStrategy().plan_orders(market, state, cfg)
    assert result.orders == ()
    assert "V_n" in result.state_delta
    assert isinstance(result.state_delta["V_n"], Decimal)


# --------------------------------------------------------------------------- #
# Purity / constraint check: no float, no clock, no IO inside vr.py
# (REQ-VR-001-R5)
# --------------------------------------------------------------------------- #
def test_vr_module_has_no_forbidden_imports() -> None:
    import ast

    import ballast.core.vr as vr_module

    source = Path(vr_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    # No IO / clock / float-math modules may be imported in the pure core.
    forbidden_modules = {"math", "datetime", "os", "socket", "time", "random", "pathlib"}
    leaked = imported & forbidden_modules
    assert not leaked, f"vr.py must not import IO/clock/float-math modules: {leaked}"

    # A math.sqrt(...) call (float) must never appear; Decimal.sqrt is required.
    # (Prose mentions in docstrings are fine; only an actual call has a paren.)
    assert "math.sqrt(" not in source


# --------------------------------------------------------------------------- #
# Property-based invariants (hypothesis)
# --------------------------------------------------------------------------- #
_MONEY = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("100000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_BAND = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("0.50"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_FLOW = st.decimals(
    min_value=Decimal("-1000"),
    max_value=Decimal("1000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_GRADIENT = st.integers(min_value=1, max_value=60)


@given(v=_MONEY, min_band=_BAND, max_band=_BAND, mode=st.sampled_from(["center", "edge"]))
def test_property_hold_inside_band_yields_no_order(
    v: Decimal,
    min_band: Decimal,
    max_band: Decimal,
    mode: Literal["center", "edge"],
) -> None:
    # E strictly inside the band must HOLD and never produce an order.
    e = v  # the line itself is always inside the band
    decision = rebalance_decision(e, v, min_band, max_band, target_mode=mode)
    assert decision.side is DecisionSide.HOLD
    assert decision.target_amount == Decimal("0.00")
    order = order_from_decision(
        decision,
        Decimal("50.00"),
        Decimal("10.00"),
        Decimal("10000.00"),
        ticker="TQQQ",
        account_seq="0001",
    )
    assert order is None


@given(
    v1=_MONEY,
    pool=_MONEY,
    e=_MONEY,
    g=_GRADIENT,
    flow_lo=_FLOW,
    flow_hi=_FLOW,
    use_skill=st.booleans(),
)
def test_property_flow_sign_moves_v2_same_direction(
    v1: Decimal,
    pool: Decimal,
    e: Decimal,
    g: int,
    flow_lo: Decimal,
    flow_hi: Decimal,
    use_skill: bool,
) -> None:
    assume(flow_lo <= flow_hi)
    low = next_value(v1, pool, e, g, flow_lo, use_skill=use_skill)
    high = next_value(v1, pool, e, g, flow_hi, use_skill=use_skill)
    # A larger (more positive) flow never decreases V2.
    assert low <= high


@given(
    v1=_MONEY,
    pool=_MONEY,
    e=_MONEY,
    g=_GRADIENT,
    flow=_FLOW,
    use_skill=st.booleans(),
)
def test_property_next_value_is_two_place_decimal(
    v1: Decimal,
    pool: Decimal,
    e: Decimal,
    g: int,
    flow: Decimal,
    use_skill: bool,
) -> None:
    v2 = next_value(v1, pool, e, g, flow, use_skill=use_skill)
    assert _is_two_place_decimal(v2)


@given(
    v=_MONEY,
    e=_MONEY,
    min_band=_BAND,
    max_band=_BAND,
    price=st.decimals(
        min_value=Decimal("1"),
        max_value=Decimal("1000"),
        allow_nan=False,
        allow_infinity=False,
        places=2,
    ),
    holdings=_MONEY,
    pool=_MONEY,
    allow_fractional=st.booleans(),
    mode=st.sampled_from(["center", "edge"]),
)
def test_property_clamps_respect_pool_and_holdings(
    v: Decimal,
    e: Decimal,
    min_band: Decimal,
    max_band: Decimal,
    price: Decimal,
    holdings: Decimal,
    pool: Decimal,
    allow_fractional: bool,
    mode: Literal["center", "edge"],
) -> None:
    decision = rebalance_decision(e, v, min_band, max_band, target_mode=mode)
    order = order_from_decision(
        decision,
        price,
        holdings,
        pool,
        allow_fractional=allow_fractional,
        ticker="TQQQ",
        account_seq="0001",
    )
    if order is None:
        return
    if order.side is Side.BUY:
        assert order.limit_price is not None
        # BUY never commits more cash than the pool.
        assert order.qty * order.limit_price <= pool
    else:
        # SELL never exceeds available holdings.
        assert order.qty <= holdings
    # Every emitted order carries a strictly positive, 2-place quantity.
    assert order.qty > Decimal("0")
    assert _is_two_place_decimal(order.qty)
