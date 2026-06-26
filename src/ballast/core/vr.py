"""VR (Value Rebalancing) strategy core (REQ-VR-001-R1..R5).

Four pure building blocks plus one :class:`Strategy`-conforming class:

* :func:`next_value` — advance the target value line ``V`` (skill / basic forms).
* :func:`rebalance_decision` — band test ``E`` against ``V`` (SELL / BUY / HOLD).
* :func:`order_from_decision` — turn a decision into a ``reserved_limit`` order.
* :class:`VRStrategy` — wire the three together over ``Market``/``State``/``Config``.

This module is PURE: no network, no filesystem, no clock (``datetime.now()`` is
forbidden). Time and prices arrive via the :class:`~ballast.core.models.Market`
snapshot; persisted state via :class:`~ballast.core.models.State`; knobs via
:class:`~ballast.core.config.Config`. Money/quantity are :class:`~decimal.Decimal`
throughout and quantized to two places via ``quantize_money`` — including the
``sqrt(G)`` computation, which uses ``Decimal.sqrt`` under a high-precision
context (never ``math.sqrt``, which returns ``float``).

It REUSES the SPEC-CORE-001 domain types and the :class:`Strategy` protocol; it
redefines none of them.

@CODE:SPEC-VR-001
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Literal

from ballast.core.config import Config
from ballast.core.instrument import InstrumentRegistry
from ballast.core.models import (
    Decision,
    DecisionSide,
    Market,
    Order,
    OrderType,
    Side,
    State,
    quantize_money,
)
from ballast.core.strategy import PlanResult

# Magnitude of the target_amount: rebalance back to the line (``center``) or to
# the nearest band edge (``edge``). SPEC-VR-001 [T4]; default ``center``.
TargetMode = Literal["center", "edge"]

# Working precision for the Decimal sqrt; ample headroom over the 2-place result.
_SQRT_PRECISION = 50

# State-store keys VRStrategy reads from ``State.data`` (namespace ``vr``).
_STATE_VALUE_LINE = "V_n"
_STATE_POOL = "pool"
_STATE_QTY = "qty"

_ONE = Decimal("1")
_TWO = Decimal("2")
_ZERO = Decimal("0")


def _dec_sqrt(g: int) -> Decimal:
    """Return ``sqrt(g)`` as a high-precision :class:`~decimal.Decimal`.

    Uses ``Decimal.sqrt`` under a local context (never ``math.sqrt``), so the
    money path never touches ``float``. ``g`` is an ``int`` and ``Decimal(g)`` is
    exact going in.
    """
    with localcontext() as ctx:
        ctx.prec = _SQRT_PRECISION
        return Decimal(g).sqrt()


def next_value(
    v1: Decimal,
    pool: Decimal,
    e: Decimal,
    g: int,
    flow: Decimal,
    *,
    use_skill: bool = True,
    r: Decimal = Decimal("0"),
) -> Decimal:
    """Compute next cycle's target value line ``V2`` (REQ-VR-001-R1).

    The skill formula (default, ``use_skill=True``) is primary::

        V2 = V1 + pool/G + (E - V1) / (2 * sqrt(G)) +- flow

    ``pool/G`` schedules cash into the line over ~``G`` cycles; the skill
    correction ``(E - V1) / (2*sqrt(G))`` raises the line as price rises (so the
    next rebalance sells more) and suppresses it on a crash (so the next
    rebalance buys less, preserving cash), with ``sqrt(G)`` damping its
    magnitude. ``flow`` is the signed deposit/withdrawal (accumulate +, withdraw
    -, hold 0) and moves ``V2`` in the same direction.

    When ``use_skill=False`` the basic, PROVISIONAL [T3] form is used instead::

        V2 = V1 * (1 + r) + pool/G +- flow            # PROVISIONAL [T3]

    The skill-correction term (and thus ``E``) is absent; a separate growth-rate
    ``r`` governs the climb. The exact placement of ``r`` may change when [T3] is
    resolved; the skill formula remains the primary path.

    ``sqrt(G)`` is computed with :class:`~decimal.Decimal` precision and the final
    ``V2`` is quantized to two places (``ROUND_HALF_UP``).
    """
    cash_schedule = pool / g
    if use_skill:
        skill_correction = (e - v1) / (_TWO * _dec_sqrt(g))
        v2 = v1 + cash_schedule + skill_correction + flow
    else:
        v2 = v1 * (_ONE + r) + cash_schedule + flow
    return quantize_money(v2)


def rebalance_decision(
    e: Decimal,
    v: Decimal,
    min_band: Decimal,
    max_band: Decimal,
    target_mode: TargetMode = "center",
) -> Decision:
    """Band-test ``E`` against the line ``V`` and return a CORE-001 Decision (R2).

    * ``E > V*(1 + max_band)`` -> SELL (Event-driven): the attack asset rose out
      of the band.
    * ``E < V*(1 - min_band)`` -> BUY (Event-driven): it fell out of the band.
    * otherwise -> HOLD with ``target_amount = 0`` (State-driven).

    ``target_mode`` sets the magnitude of ``Decision.target_amount`` [T4]:

    * ``center`` — rebalance back to the line ``V``:
      SELL ``E - V``; BUY ``V - E``.
    * ``edge`` — rebalance back to the nearest band edge:
      SELL ``E - V*(1 + max_band)``; BUY ``V*(1 - min_band) - E``.

    Band widths are leverage-dependent [T1] and arrive as arguments resolved
    through the CORE-001 registry chain (a 3x instrument carries a different band
    than a 2x instrument); this function hardcodes no band numbers.
    """
    upper = v * (_ONE + max_band)
    lower = v * (_ONE - min_band)

    if e > upper:
        amount = (e - v) if target_mode == "center" else (e - upper)
        return Decision(side=DecisionSide.SELL, target_amount=amount)
    if e < lower:
        amount = (v - e) if target_mode == "center" else (lower - e)
        return Decision(side=DecisionSide.BUY, target_amount=amount)
    return Decision(side=DecisionSide.HOLD, target_amount=_ZERO)


def order_from_decision(
    decision: Decision,
    price: Decimal,
    holdings: Decimal,
    pool: Decimal,
    *,
    allow_fractional: bool = False,
    ticker: str,
    account_seq: str,
) -> Order | None:
    """Convert a non-HOLD Decision into a ``reserved_limit`` order (R3).

    Steps, in a fixed, independently testable order: convert
    ``target_amount`` to ``qty = target_amount / price``; clamp a BUY to buying
    power (``qty * price <= pool``) and a SELL to sellable quantity
    (``qty <= holdings``); floor to whole shares when ``allow_fractional`` is
    ``False``; and return ``None`` when the quantity rounds to ``0`` (never a
    zero/dust order).

    A HOLD decision (or ``target_amount == 0``) yields ``None``. VR emits the
    ``reserved_limit`` order type only — never ``LOC`` or ``market``.
    """
    if decision.side is DecisionSide.HOLD or decision.target_amount == _ZERO:
        return None

    side = Side.BUY if decision.side is DecisionSide.BUY else Side.SELL

    qty = decision.target_amount / price
    # Clamp BUY to buying power (qty * price <= pool); SELL to sellable holdings.
    qty = min(qty, pool / price) if side is Side.BUY else min(qty, holdings)

    # Discretize toward zero: whole shares (ROUND_FLOOR) or 2-place fractional.
    # Flooring (never rounding up) keeps the clamp invariants intact through the
    # Order's own 2-place quantization, so cash committed never exceeds the pool
    # and qty never exceeds holdings.
    quantum = _ONE if not allow_fractional else Decimal("0.01")
    qty = qty.quantize(quantum, rounding=ROUND_FLOOR)

    # A quantity that rounds to 0 is never emitted (no zero/dust order).
    if qty <= _ZERO:
        return None

    return Order(
        side=side,
        ticker=ticker,
        qty=qty,
        limit_price=price,
        order_type=OrderType.RESERVED_LIMIT,
        account_seq=account_seq,
    )


class VRStrategy:
    """VR strategy wiring conforming to the CORE-001 Strategy protocol (R4).

    ``plan_orders`` reads ``V_n``/``pool``/``qty`` from ``state`` and the
    band/``G``/``flow``/``use_skill``/``target_mode``/``account_seq``/``ticker``
    knobs from ``cfg``, computes ``E = qty * current_price``, then chains
    :func:`next_value` -> :func:`rebalance_decision` -> :func:`order_from_decision`.
    It is pure: it reads only its arguments and mutates none of them.
    """

    cadence: Literal["daily", "cycle"] = "cycle"
    ns: str = "vr"

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        """Wire the VR pipeline for one snapshot.

        Returns the order(s) it plans (``()`` on HOLD or a None order) plus the
        recomputed value line surfaced as ``{"V_n": V2}`` — the SAME ``V2`` used
        to make this cycle's rebalance decision (single-pass; no second
        ``next_value`` evaluation), so the engine can advance ``V_n`` across
        cycles (REQ-STRATEGY-001-R3).
        """
        vr = cfg.strategies.vr

        v1 = state.data.get(_STATE_VALUE_LINE, _ZERO)
        pool = state.data.get(_STATE_POOL, _ZERO)
        held_qty = state.data.get(_STATE_QTY, _ZERO)

        price = market.current_price
        e = held_qty * price

        v = next_value(
            v1,
            pool,
            e,
            vr.g,
            vr.flow,
            use_skill=vr.use_skill,
            r=vr.r,
        )

        # Band: explicit > instrument default > error (CORE-001 chain, [T1]).
        registry = InstrumentRegistry.from_config(cfg)
        base_band = registry.resolve_band(vr.ticker, vr.band)
        min_band = vr.min_band if vr.min_band is not None else base_band
        max_band = vr.max_band if vr.max_band is not None else base_band

        decision = rebalance_decision(e, v, min_band, max_band, vr.target_mode)

        order = order_from_decision(
            decision,
            price,
            held_qty,
            pool,
            allow_fractional=cfg.common.allow_fractional,
            ticker=vr.ticker,
            account_seq=vr.account_seq,
        )
        orders: tuple[Order, ...] = (order,) if order is not None else ()
        # Surface the recomputed value line for the engine to carry forward. ``v``
        # is the exact ``V2`` used for the decision above (already 2-place
        # quantized by ``next_value``); no second evaluation can diverge (FD4).
        return PlanResult(orders=orders, state_delta={_STATE_VALUE_LINE: v})
