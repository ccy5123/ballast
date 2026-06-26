"""Tests for the MAB (무한매수법 / Infinite Buying) strategy core (REQ-MAB-001-R1..R5).

Mirrors ``src/ballast/core/mab.py``. Covers all ten acceptance scenarios
(``acceptance.md``) plus ``hypothesis`` property tests for financial invariants:
buy cash never exceeds ``seed_remaining``, no zero/dust orders, profit-take iff
``holdings > 0``, every order is LOC, 2-place ``Decimal`` quantization, 후반전
emits at most leg A among buys, and quarter-sell ``== floor(holdings / 4)``.

@TEST:SPEC-MAB-001
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

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
from ballast.core.mab import (
    MABStrategy,
    mab_daily_orders,
    mab_on_seed_exhausted,
)

# --------------------------------------------------------------------------- #
# Decimal helpers for tests
# --------------------------------------------------------------------------- #
TWO_PLACES = Decimal("0.01")


def _is_two_place_decimal(value: Decimal) -> bool:
    """True iff ``value`` is a Decimal with exactly two fractional digits."""
    return isinstance(value, Decimal) and value.as_tuple().exponent == -2


def _floor1(value: Decimal) -> Decimal:
    """Floor toward zero to a whole share (mirrors the production policy)."""
    return value.quantize(Decimal(1), rounding=ROUND_FLOOR)


def _buys(orders: list[Order]) -> list[Order]:
    return [o for o in orders if o.side is Side.BUY]


def _sells(orders: list[Order]) -> list[Order]:
    return [o for o in orders if o.side is Side.SELL]


# Standing SOXL-like example parameters (acceptance.md).
def _daily(**overrides: object) -> list[Order]:
    """Call ``mab_daily_orders`` with the standing example defaults."""
    kwargs: dict[str, object] = {
        "avg_price": Decimal("50.00"),
        "holdings": Decimal("30.00"),
        "seed": Decimal("8000.00"),
        "seed_remaining": Decimal("8000.00"),
        "round_idx": 5,
        "n_splits": 40,
        "target_pct": Decimal("0.20"),
        "alpha": Decimal("0.05"),
        "split_ratio": Decimal("0.50"),
        "halftime_rule": "standard",
        "version": "v2.2",
        "ticker": "SOXL",
        "account_seq": "0002",
    }
    kwargs.update(overrides)
    return mab_daily_orders(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Scenario 1 — Mid-game day: two LOC buys + LOC profit-take (REQ-MAB-001-R1)
# --------------------------------------------------------------------------- #
def test_mid_game_two_buys_and_profit_take() -> None:
    orders = _daily()
    assert len(orders) == 3

    leg_a, leg_b, sell = orders[0], orders[1], orders[2]

    # Leg A (near-average): floor(100 / 50) = 2 @ 50.00.
    assert leg_a.side is Side.BUY
    assert leg_a.qty == Decimal("2.00")
    assert leg_a.limit_price == Decimal("50.00")
    assert leg_a.order_type is OrderType.LOC

    # Leg B (step-up): floor(100 / 52.50) = 1 @ 52.50.
    assert leg_b.side is Side.BUY
    assert leg_b.qty == Decimal("1.00")
    assert leg_b.limit_price == Decimal("52.50")
    assert leg_b.order_type is OrderType.LOC

    # Profit-take SELL of holdings at avg * (1 + target_pct) = 60.00.
    assert sell.side is Side.SELL
    assert sell.qty == Decimal("30.00")
    assert sell.limit_price == Decimal("60.00")
    assert sell.order_type is OrderType.LOC

    assert all(o.ticker == "SOXL" and o.account_seq == "0002" for o in orders)


# --------------------------------------------------------------------------- #
# Scenario 2 — Profit-take price = avg_price * (1 + target_pct) (REQ-MAB-001-R1)
# --------------------------------------------------------------------------- #
def test_profit_take_price_is_avg_times_one_plus_target_soxl() -> None:
    orders = _daily()
    sell = _sells(orders)[0]
    assert sell.limit_price == Decimal("60.00")  # 50 * 1.20


def test_profit_take_price_tqqq_variant() -> None:
    # TQQQ-like: avg 100, target 0.10 -> 110.00.
    orders = _daily(
        avg_price=Decimal("100.00"),
        target_pct=Decimal("0.10"),
        ticker="TQQQ",
    )
    sell = _sells(orders)[0]
    assert sell.limit_price == Decimal("110.00")


# --------------------------------------------------------------------------- #
# Scenario 3 — Second buy price = avg_price * (1 + alpha) (REQ-MAB-001-R1)
# --------------------------------------------------------------------------- #
def test_second_buy_price_is_avg_times_one_plus_alpha() -> None:
    orders = _daily(holdings=Decimal("0.00"))  # buys only, simpler list
    leg_a, leg_b = orders[0], orders[1]
    assert leg_a.limit_price == Decimal("50.00")
    assert leg_b.limit_price == Decimal("52.50")  # 50 * 1.05


def test_alpha_sensitivity_moves_only_leg_b() -> None:
    orders = _daily(holdings=Decimal("0.00"), alpha=Decimal("0.10"))
    leg_a, leg_b = orders[0], orders[1]
    assert leg_a.limit_price == Decimal("50.00")
    assert leg_b.limit_price == Decimal("55.00")  # 50 * 1.10


# --------------------------------------------------------------------------- #
# Scenario 4 — seed_remaining below the per-round budget => buy capped
# (REQ-MAB-001-R3)
# --------------------------------------------------------------------------- #
def test_seed_remaining_caps_budget() -> None:
    orders = _daily(
        seed_remaining=Decimal("120.00"),
        holdings=Decimal("0.00"),
    )
    buys = _buys(orders)
    assert len(buys) == 2
    # budget = min(200, 120) = 120; 60/60 split.
    assert buys[0].qty == Decimal("1.00")  # floor(60 / 50)
    assert buys[0].limit_price == Decimal("50.00")
    assert buys[1].qty == Decimal("1.00")  # floor(60 / 52.50)
    assert buys[1].limit_price == Decimal("52.50")
    # Total cash committed never exceeds seed_remaining.
    cash = sum((o.qty * o.limit_price for o in buys), Decimal("0"))
    assert cash == Decimal("102.50")
    assert cash <= Decimal("120.00")
    # No profit-take because holdings == 0.
    assert _sells(orders) == []


# --------------------------------------------------------------------------- #
# Scenario 5 — seed_remaining <= 0 => no buys (sell may remain) (REQ-MAB-001-R3)
# --------------------------------------------------------------------------- #
def test_seed_remaining_zero_emits_no_buys_but_keeps_sell() -> None:
    orders = _daily(seed_remaining=Decimal("0.00"))
    assert _buys(orders) == []
    sells = _sells(orders)
    assert len(sells) == 1
    assert sells[0].qty == Decimal("30.00")
    assert sells[0].limit_price == Decimal("60.00")
    assert sells[0].order_type is OrderType.LOC
    assert len(orders) == 1


def test_seed_remaining_negative_emits_no_buys() -> None:
    orders = _daily(seed_remaining=Decimal("-50.00"), holdings=Decimal("0.00"))
    assert orders == []


# --------------------------------------------------------------------------- #
# Scenario 6 — Seed exhausted => quarter-sell = floor(holdings / 4), LOC
# (REQ-MAB-001-R4); SPEC-STRATEGY-001 R1: now carries a deterministic injected
# (close) limit price (AC-2), never None.
# --------------------------------------------------------------------------- #
def test_seed_exhausted_quarter_sell() -> None:
    order = mab_on_seed_exhausted(
        Decimal("40.00"),
        Decimal("12.34"),  # injected reference (close) price
        version="v2.2",
        ticker="SOXL",
        account_seq="0002",
    )
    assert order.side is Side.SELL
    assert order.order_type is OrderType.LOC
    assert order.qty == Decimal("10.00")  # floor(40 / 4)
    assert order.ticker == "SOXL"
    assert order.account_seq == "0002"
    # SPEC-STRATEGY-001 fix (R1, FD1): the quarter-sell is a PRICED LOC at the
    # injected reference, quantized to 2 dp — not the prior None-priced LOC.
    assert order.limit_price == Decimal("12.34")
    assert order.limit_price is not None
    assert isinstance(order.limit_price, Decimal)


def test_seed_exhausted_quarter_sell_floors_toward_zero() -> None:
    order = mab_on_seed_exhausted(
        Decimal("41.00"),
        Decimal("12.34"),
        ticker="SOXL",
        account_seq="0002",
    )
    assert order.qty == Decimal("10.00")  # floor(41 / 4) = floor(10.25)
    assert order.limit_price == Decimal("12.34")


def test_seed_exhausted_quarter_sell_quantizes_ref_price() -> None:
    # AC-2 / AC-15: a ref_price with >2 dp is normalized via quantize_money;
    # the surviving limit_price is a 2-place Decimal.
    order = mab_on_seed_exhausted(
        Decimal("40.00"),
        Decimal("12.345"),  # 3 dp -> ROUND_HALF_UP -> 12.35
        ticker="SOXL",
        account_seq="0002",
    )
    assert order.limit_price == Decimal("12.35")
    assert order.limit_price is not None
    assert order.limit_price.as_tuple().exponent == -2


# --------------------------------------------------------------------------- #
# Scenario 7 — Edge: holdings = 0 => no profit-take (REQ-MAB-001-R1 / R3)
# --------------------------------------------------------------------------- #
def test_holdings_zero_emits_no_profit_take() -> None:
    orders = _daily(holdings=Decimal("0.00"))
    assert _sells(orders) == []
    buys = _buys(orders)
    assert len(buys) == 2
    assert len(orders) == 2


# --------------------------------------------------------------------------- #
# Scenario 8 — 후반전 halftime rule: leg B dropped (REQ-MAB-001-R2 [T9])
# --------------------------------------------------------------------------- #
def test_second_half_drops_leg_b() -> None:
    orders = _daily(round_idx=25)  # 25 > 40 // 2 == 20
    buys = _buys(orders)
    assert len(buys) == 1
    assert buys[0].limit_price == Decimal("50.00")  # only leg A
    # Profit-take is phase-independent.
    sells = _sells(orders)
    assert len(sells) == 1
    assert sells[0].qty == Decimal("30.00")
    assert sells[0].limit_price == Decimal("60.00")
    assert len(orders) == 2


def test_halftime_boundary_last_first_half_keeps_both_legs() -> None:
    orders = _daily(round_idx=20, holdings=Decimal("0.00"))  # == 40 // 2
    assert len(_buys(orders)) == 2  # last 전반전 round -> both legs


def test_halftime_boundary_first_second_half_drops_leg_b() -> None:
    orders = _daily(round_idx=21, holdings=Decimal("0.00"))  # first 후반전
    assert len(_buys(orders)) == 1


def test_odd_n_splits_uses_floor_division() -> None:
    # n_splits = 41 -> 41 // 2 == 20; round_idx 20 is last 전반전, 21 first 후반전.
    both = _daily(round_idx=20, n_splits=41, holdings=Decimal("0.00"))
    only_a = _daily(round_idx=21, n_splits=41, holdings=Decimal("0.00"))
    assert len(_buys(both)) == 2
    assert len(_buys(only_a)) == 1


# --------------------------------------------------------------------------- #
# Scenario 9 — Dust suppression: a buy leg's qty rounds to 0 => dropped
# (REQ-MAB-001-R3)
# --------------------------------------------------------------------------- #
def test_dust_both_legs_dropped_empty_list() -> None:
    orders = _daily(
        seed=Decimal("400.00"),  # budget = 10; legs 5/5
        seed_remaining=Decimal("400.00"),
        holdings=Decimal("0.00"),
    )
    # qty_a = floor(5 / 50) = 0, qty_b = floor(5 / 52.50) = 0 -> empty list.
    assert orders == []


def test_partial_dust_drops_only_leg_b() -> None:
    # split_ratio favors leg A so leg A funds a share but leg B starves.
    # budget = 60; budget_a = 60 * 0.9 = 54 -> floor(54/50) = 1;
    # budget_b = 60 * 0.1 = 6 -> floor(6/52.50) = 0 -> dropped.
    orders = _daily(
        seed=Decimal("2400.00"),  # budget = 60
        seed_remaining=Decimal("2400.00"),
        split_ratio=Decimal("0.90"),
        holdings=Decimal("0.00"),
    )
    buys = _buys(orders)
    assert len(buys) == 1
    assert buys[0].limit_price == Decimal("50.00")  # leg A survived
    assert buys[0].qty == Decimal("1.00")


# --------------------------------------------------------------------------- #
# Scenario 10 — MABStrategy conforms and wires both paths (REQ-MAB-001-R5)
# --------------------------------------------------------------------------- #
def _accepts_strategy(strategy: Strategy) -> Strategy:
    """Typed sink: mypy --strict verifies MABStrategy satisfies Strategy."""
    return strategy


def test_mabstrategy_attributes() -> None:
    strategy = MABStrategy()
    assert strategy.cadence == "daily"
    assert strategy.ns == "mab"


def test_mabstrategy_conforms_to_protocol() -> None:
    strategy = MABStrategy()
    assert isinstance(strategy, Strategy)
    assert _accepts_strategy(strategy) is strategy


def _mab_market() -> Market:
    return Market(
        ticker="SOXL",
        current_price=Decimal("50.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )


def test_mabstrategy_plan_orders_daily_path(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    # mab config: seed default 0 must be overridden via state-only values? No --
    # seed comes from cfg; the example mab block leaves seed at its default 0,
    # so plan_orders must still wire a valid (possibly empty) order list.
    state = State(
        ns="mab",
        data={
            "avg_price": Decimal("50.00"),
            "holdings": Decimal("30.00"),
            "seed_remaining": Decimal("8000.00"),
            "round_idx": Decimal("5"),
        },
    )
    result = MABStrategy().plan_orders(_mab_market(), state, cfg)
    orders = list(result.orders)
    # seed defaults to 0 -> budget 0 -> no buys; holdings>0 -> one profit-take.
    # target_pct resolved via registry: explicit mab override 0.45 -> 50*1.45.
    assert all(o.order_type is OrderType.LOC for o in orders)
    sells = _sells(orders)
    assert len(sells) == 1
    assert sells[0].limit_price == Decimal("72.50")  # 50 * 1.45 (explicit)
    # AC-10: MAB conforms with an EMPTY state delta (no evolving internal state).
    assert dict(result.state_delta) == {}


def test_mabstrategy_plan_orders_seed_exhausted_path(
    example_config_path: Path,
) -> None:
    cfg = Config.load(example_config_path)
    # round_idx > n_splits (default 40) triggers the quarter-sell path.
    state = State(
        ns="mab",
        data={
            "avg_price": Decimal("50.00"),
            "holdings": Decimal("40.00"),
            "seed_remaining": Decimal("0.00"),
            "round_idx": Decimal("41"),
        },
    )
    result = MABStrategy().plan_orders(_mab_market(), state, cfg)
    orders = result.orders
    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert orders[0].order_type is OrderType.LOC
    assert orders[0].qty == Decimal("10.00")  # floor(40 / 4)
    # AC-3 (R1): the quarter-sell is priced at the injected market.current_price.
    assert orders[0].limit_price == Decimal("50.00")  # _mab_market() close
    # AC-10: MAB still conforms with an empty delta on the seed-exhausted path.
    assert dict(result.state_delta) == {}


def test_mabstrategy_plan_orders_is_pure(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    data = {
        "avg_price": Decimal("50.00"),
        "holdings": Decimal("30.00"),
        "seed_remaining": Decimal("8000.00"),
        "round_idx": Decimal("5"),
    }
    state = State(ns="mab", data=dict(data))
    MABStrategy().plan_orders(_mab_market(), state, cfg)
    # plan_orders does not mutate its inputs.
    assert state.data == data


# --------------------------------------------------------------------------- #
# SPEC-STRATEGY-001 — MABStrategy conformance (AC-3, AC-10, AC-16)
# --------------------------------------------------------------------------- #
def test_mabstrategy_returns_plan_result(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    state = State(
        ns="mab",
        data={
            "avg_price": Decimal("50.00"),
            "holdings": Decimal("30.00"),
            "seed_remaining": Decimal("8000.00"),
            "round_idx": Decimal("5"),
        },
    )
    result = MABStrategy().plan_orders(_mab_market(), state, cfg)
    assert isinstance(result, PlanResult)


def test_mabstrategy_passes_market_price_into_quarter_sell(
    example_config_path: Path,
) -> None:
    # AC-3: on the seed-exhausted path MABStrategy prices the quarter-sell at the
    # injected market.current_price (here 12.34), and surfaces an empty delta.
    cfg = Config.load(example_config_path)
    market = Market(
        ticker="SOXL",
        current_price=Decimal("12.34"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )
    state = State(
        ns="mab",
        data={
            "avg_price": Decimal("50.00"),
            "holdings": Decimal("17.00"),
            "seed_remaining": Decimal("0.00"),
            "round_idx": Decimal("41"),  # > n_splits (40) -> quarter-sell
        },
    )
    snapshot = dict(state.data)
    result = MABStrategy().plan_orders(market, state, cfg)
    assert len(result.orders) == 1
    order = result.orders[0]
    assert order.side is Side.SELL
    assert order.order_type is OrderType.LOC
    assert order.qty == Decimal("4.00")  # floor(17 / 4)
    assert order.limit_price == Decimal("12.34")  # injected close, not None
    assert dict(result.state_delta) == {}
    # AC-16: plan_orders mutated neither the state nor (implicitly) the cfg.
    assert state.data == snapshot


def test_mabstrategy_empty_delta_on_daily_path(example_config_path: Path) -> None:
    # AC-10: the daily (non-exhausted) path also conforms with an empty delta.
    cfg = Config.load(example_config_path)
    state = State(
        ns="mab",
        data={
            "avg_price": Decimal("50.00"),
            "holdings": Decimal("30.00"),
            "seed_remaining": Decimal("8000.00"),
            "round_idx": Decimal("5"),
        },
    )
    result = MABStrategy().plan_orders(_mab_market(), state, cfg)
    assert dict(result.state_delta) == {}


# --------------------------------------------------------------------------- #
# Purity / constraint check: no float, no clock, no IO inside mab.py
# (REQ-MAB-001-R5)
# --------------------------------------------------------------------------- #
def test_mab_module_has_no_forbidden_imports() -> None:
    import ast

    import ballast.core.mab as mab_module

    source = Path(mab_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    forbidden_modules = {"math", "datetime", "os", "socket", "time", "random", "pathlib"}
    leaked = imported & forbidden_modules
    assert not leaked, f"mab.py must not import IO/clock/float-math modules: {leaked}"


# --------------------------------------------------------------------------- #
# Property-based invariants (hypothesis)
# --------------------------------------------------------------------------- #
_PRICE = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("1000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_HOLDINGS = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_SEED = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_SEED_REMAINING = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_RATIO = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("0.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_ALPHA = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("1.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_TARGET = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_N_SPLITS = st.integers(min_value=2, max_value=200)
_ROUND_IDX = st.integers(min_value=1, max_value=200)


@st.composite
def _daily_inputs(draw: st.DrawFn) -> dict[str, object]:
    n_splits = draw(_N_SPLITS)
    return {
        "avg_price": draw(_PRICE),
        "holdings": draw(_HOLDINGS),
        "seed": draw(_SEED),
        "seed_remaining": draw(_SEED_REMAINING),
        "round_idx": draw(st.integers(min_value=1, max_value=n_splits)),
        "n_splits": n_splits,
        "target_pct": draw(_TARGET),
        "alpha": draw(_ALPHA),
        "split_ratio": draw(_RATIO),
        "ticker": "SOXL",
        "account_seq": "0002",
    }


@given(_daily_inputs())
def test_property_buy_cash_never_exceeds_seed_remaining(
    inputs: dict[str, object],
) -> None:
    orders = mab_daily_orders(**inputs)  # type: ignore[arg-type]
    seed_remaining = inputs["seed_remaining"]
    assert isinstance(seed_remaining, Decimal)
    seed = inputs["seed"]
    n_splits = inputs["n_splits"]
    assert isinstance(seed, Decimal)
    assert isinstance(n_splits, int)
    per_round = seed / n_splits
    cash = sum(
        (o.qty * o.limit_price for o in _buys(orders) if o.limit_price is not None),
        Decimal("0"),
    )
    assert cash <= seed_remaining
    assert cash <= per_round + TWO_PLACES  # within a quantization step of the cap


@given(_daily_inputs())
def test_property_no_order_has_zero_qty(inputs: dict[str, object]) -> None:
    orders = mab_daily_orders(**inputs)  # type: ignore[arg-type]
    assert all(o.qty > Decimal("0") for o in orders)


@given(_daily_inputs())
def test_property_profit_take_iff_holdings_positive(
    inputs: dict[str, object],
) -> None:
    orders = mab_daily_orders(**inputs)  # type: ignore[arg-type]
    holdings = inputs["holdings"]
    assert isinstance(holdings, Decimal)
    has_sell = len(_sells(orders)) > 0
    assert has_sell == (holdings > Decimal("0"))


@given(_daily_inputs())
def test_property_every_order_is_loc(inputs: dict[str, object]) -> None:
    orders = mab_daily_orders(**inputs)  # type: ignore[arg-type]
    assert all(o.order_type is OrderType.LOC for o in orders)


@given(_daily_inputs())
def test_property_orders_are_two_place_decimals(
    inputs: dict[str, object],
) -> None:
    orders = mab_daily_orders(**inputs)  # type: ignore[arg-type]
    for o in orders:
        assert _is_two_place_decimal(o.qty)
        assert o.limit_price is not None
        assert _is_two_place_decimal(o.limit_price)


@given(_daily_inputs())
def test_property_second_half_emits_at_most_leg_a(
    inputs: dict[str, object],
) -> None:
    n_splits = inputs["n_splits"]
    assert isinstance(n_splits, int)
    inputs["round_idx"] = n_splits  # last round -> deep 후반전 (for n_splits >= 2)
    orders = mab_daily_orders(**inputs)  # type: ignore[arg-type]
    avg_price = inputs["avg_price"]
    assert isinstance(avg_price, Decimal)
    expected_a_price = avg_price.quantize(TWO_PLACES)
    for buy in _buys(orders):
        # In 후반전 only the near-average leg A (at avg_price) may appear.
        assert buy.limit_price == expected_a_price


@given(_HOLDINGS, _PRICE)
def test_property_quarter_sell_is_floor_holdings_over_four(
    holdings: Decimal,
    ref_price: Decimal,
) -> None:
    order = mab_on_seed_exhausted(holdings, ref_price, ticker="SOXL", account_seq="0002")
    assert order.qty == _floor1(holdings / Decimal(4))
    assert order.order_type is OrderType.LOC
    assert order.side is Side.SELL
    # SPEC-STRATEGY-001 (R1): always a priced LOC at the quantized reference.
    assert order.limit_price is not None
    assert order.limit_price == ref_price.quantize(TWO_PLACES)
    assert _is_two_place_decimal(order.limit_price)
