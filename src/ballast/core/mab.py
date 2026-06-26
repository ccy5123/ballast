"""MAB (무한매수법 / Infinite Buying) strategy core (REQ-MAB-001-R1..R5).

Two pure building blocks plus one :class:`Strategy`-conforming class:

* :func:`mab_daily_orders` — the day's two-point LOC buys plus the LOC
  profit-take (when ``holdings > 0``).
* :func:`mab_on_seed_exhausted` — the seed-exhausted quarter-sell (LOC).
* :class:`MABStrategy` — wire the two over ``Market``/``State``/``Config``.

This module is PURE: no network, no filesystem, no clock (``datetime.now()`` is
forbidden). Time and prices arrive via the :class:`~ballast.core.models.Market`
snapshot; persisted state via :class:`~ballast.core.models.State`; knobs via
:class:`~ballast.core.config.Config`. Money/quantity are :class:`~decimal.Decimal`
throughout. Share quantities are discretized toward zero (``ROUND_FLOOR``) before
the zero-check so no dust orders survive, and ``limit_price``s are quantized to two
places at the ``Order`` boundary via ``quantize_money`` (``Order.__post_init__``).

It REUSES the SPEC-CORE-001 domain types and the :class:`Strategy` protocol and
mirrors ``vr.py``'s construction (clamp → floor toward zero → zero-check →
quantize); it redefines none of them and does not import ``vr.py``. MAB stamps
``OrderType.LOC`` on every order.

@CODE:SPEC-MAB-001
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from ballast.core.config import Config
from ballast.core.instrument import InstrumentRegistry
from ballast.core.models import Market, Order, OrderType, Side, State, quantize_money
from ballast.core.strategy import PlanResult

# Halftime phase rule selector. SPEC-MAB-001 [T9]; default ``standard``,
# extensible later without an interface break.
HalftimeRule = Literal["standard"]

# MAB version variant. SPEC-MAB-001 [T9]; later versions add target step-down /
# quarter-stop-loss. Provisional; default ``v2.2``.
MABVersion = Literal["v1.0", "v1.1", "v2.0", "v2.1", "v2.2"]

# State-store keys MABStrategy reads from ``State.data`` (namespace ``mab``).
_STATE_AVG_PRICE = "avg_price"
_STATE_HOLDINGS = "holdings"
_STATE_SEED_REMAINING = "seed_remaining"
_STATE_ROUND_IDX = "round_idx"

_ONE = Decimal("1")
_FOUR = Decimal("4")
_ZERO = Decimal("0")


def _floor_shares(value: Decimal) -> Decimal:
    """Discretize a share count toward zero (``ROUND_FLOOR``).

    Flooring (never rounding up) keeps the seed-remaining cap intact through the
    ``Order``'s own 2-place quantization, so cash committed never exceeds the cap
    and the quarter-sell never oversells. Mirrors ``vr.py``'s ``order_from_decision``.
    """
    return value.quantize(_ONE, rounding=ROUND_FLOOR)


def _loc_buy_leg(
    price: Decimal,
    budget_portion: Decimal,
    *,
    ticker: str,
    account_seq: str,
) -> Order | None:
    """Build one LOC BUY leg, or ``None`` when its quantity floors to zero.

    ``qty = floor(budget_portion / price)``; a quantity that rounds to ``0`` (the
    budget cannot afford one share at this price) yields ``None`` so no zero/dust
    order is emitted. The ``limit_price`` is quantized at the ``Order`` boundary.
    """
    qty = _floor_shares(budget_portion / price)
    if qty <= _ZERO:
        return None
    return Order(
        side=Side.BUY,
        ticker=ticker,
        qty=qty,
        limit_price=price,
        order_type=OrderType.LOC,
        account_seq=account_seq,
    )


def mab_daily_orders(
    avg_price: Decimal,
    holdings: Decimal,
    seed: Decimal,
    seed_remaining: Decimal,
    round_idx: int,
    n_splits: int,
    target_pct: Decimal,
    alpha: Decimal,
    split_ratio: Decimal,
    *,
    halftime_rule: HalftimeRule = "standard",
    version: MABVersion = "v2.2",
    ticker: str,
    account_seq: str,
) -> list[Order]:
    """Return the MAB LOC orders for one trading day (REQ-MAB-001-R1..R3).

    The day's effective budget is ``budget = min(seed / n_splits, seed_remaining)``
    (the seed-remaining cap, R3), split into two LOC BUY legs by ``split_ratio``:

    * **Leg A (near-average)** at ``avg_price`` with budget ``budget * split_ratio``,
      ``qty_a = floor(budget_a / avg_price)``.
    * **Leg B (step-up)** at ``avg_price * (1 + alpha)`` with budget
      ``budget * (1 - split_ratio)``, ``qty_b = floor(budget_b / price_b)`` [T10].

    Halftime rule (R2, ``halftime_rule``, PROVISIONAL [T9]): 전반전
    (``round_idx <= n_splits // 2``) places both legs; 후반전
    (``round_idx > n_splits // 2``) reduces aggressiveness to leg A only. The
    split point uses integer floor division so it is deterministic for odd
    ``n_splits``.

    When ``holdings > 0`` an LOC SELL profit-take of the entire ``holdings`` at
    ``avg_price * (1 + target_pct)`` is appended (R1, phase-independent); resolved
    ``target_pct`` is never hardcoded [T10]. ``seed_remaining <= 0`` emits no buys;
    any leg flooring to ``0`` is dropped; ``holdings <= 0`` emits no sell (R3).
    Every order carries ``OrderType.LOC``. ``version`` is accepted for forward
    compatibility [T9]; the ``v2.2`` default does not alter these primary mechanics.
    """
    orders: list[Order] = []

    # R3: no buys once the cycle's buying cash is exhausted.
    if seed_remaining > _ZERO:
        budget = min(seed / n_splits, seed_remaining)

        # Leg A (near-average) is always a candidate in both phases.
        leg_a = _loc_buy_leg(
            avg_price,
            budget * split_ratio,
            ticker=ticker,
            account_seq=account_seq,
        )
        if leg_a is not None:
            orders.append(leg_a)

        # Leg B (step-up) only in 전반전 (R2 [T9], halftime_rule="standard").
        if round_idx <= n_splits // 2:
            leg_b = _loc_buy_leg(
                avg_price * (_ONE + alpha),
                budget * (_ONE - split_ratio),
                ticker=ticker,
                account_seq=account_seq,
            )
            if leg_b is not None:
                orders.append(leg_b)

    # R1: profit-take SELL of the whole position, independent of phase (R3 guard).
    if holdings > _ZERO:
        orders.append(
            Order(
                side=Side.SELL,
                ticker=ticker,
                qty=holdings,
                limit_price=avg_price * (_ONE + target_pct),
                order_type=OrderType.LOC,
                account_seq=account_seq,
            )
        )

    return orders


def mab_on_seed_exhausted(
    holdings: Decimal,
    ref_price: Decimal,
    *,
    version: MABVersion = "v2.2",
    ticker: str,
    account_seq: str,
) -> Order:
    """Return the seed-exhausted quarter-sell as a single priced LOC SELL (R4 / R1).

    When the seed is exhausted (after ``n_splits`` buys) the strategy quarter-sells
    ``floor(holdings / 4)`` to re-secure the seed before the cycle repeats. The
    quantity is floored toward zero (never oversold beyond the quarter) and carries
    ``OrderType.LOC``. The v2.x quarter-stop-loss is a provisional variant [T9];
    the ``v2.2`` default is the seed-exhausted quarter-sell defined here.

    SPEC-STRATEGY-001 (R1, FD1): the LOC carries a deterministic, INJECTED limit
    price — ``quantize_money(ref_price)``, the cycle's current/close price supplied
    via ``Market.current_price`` — never ``None``. Because the limit equals the bar
    close, the backtest still fills it at the close (identical ledger effect), while
    a priced ``LIMIT + CLS`` is a valid live order (Toss has no MOC, so the prior
    price-less LOC surfaced as ``FAILED``). ``ref_price`` is injected, so the
    function stays pure (no clock, no network, no ``float``).
    """
    return Order(
        side=Side.SELL,
        ticker=ticker,
        qty=_floor_shares(holdings / _FOUR),
        limit_price=quantize_money(ref_price),
        order_type=OrderType.LOC,
        account_seq=account_seq,
    )


class MABStrategy:
    """MAB strategy wiring conforming to the CORE-001 Strategy protocol (R5).

    ``plan_orders`` reads ``avg_price``/``holdings``/``seed_remaining``/``round_idx``
    from ``state`` and ``seed``/``n_splits``/``alpha``/``split_ratio``/
    ``halftime_rule``/``version``/``account_seq``/``ticker`` from ``cfg``, resolves
    ``target_pct`` via the registry chain [T10], then wires the daily orders. When
    the seed is exhausted (``round_idx > n_splits``) it returns the single
    :func:`mab_on_seed_exhausted` quarter-sell instead. It is pure: it reads only
    its arguments and mutates none of them. MAB emits the ``LOC`` order type only.
    """

    cadence: Literal["daily", "cycle"] = "daily"
    ns: str = "mab"

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        """Wire the MAB daily or seed-exhausted path for one snapshot.

        Returns the day's orders plus an EMPTY state delta: MAB has no
        strategy-evolved internal state (``avg_price``/``holdings``/
        ``seed_remaining``/``round_idx`` are derived by the engine from fills), so
        it conforms to the enriched contract without inventing state
        (REQ-STRATEGY-001-R2/R4, FD5). The seed-exhausted quarter-sell is priced
        at the injected ``market.current_price`` (REQ-STRATEGY-001-R1, FD1).
        """
        mab = cfg.strategies.mab

        avg_price = state.data.get(_STATE_AVG_PRICE, _ZERO)
        holdings = state.data.get(_STATE_HOLDINGS, _ZERO)
        seed_remaining = state.data.get(_STATE_SEED_REMAINING, _ZERO)
        round_idx = int(state.data.get(_STATE_ROUND_IDX, _ZERO))

        # target_pct: explicit > instrument default > error (CORE-001 chain, [T10]).
        registry = InstrumentRegistry.from_config(cfg)
        target_pct = registry.resolve_target_pct(mab.ticker, mab.target_pct)

        # Seed exhausted after n_splits buys -> the quarter-sell path. The LOC is
        # priced at the injected current/close price (FD1); MAB's delta is empty.
        if round_idx > mab.n_splits:
            return PlanResult(
                orders=(
                    mab_on_seed_exhausted(
                        holdings,
                        market.current_price,
                        version=mab.version,
                        ticker=mab.ticker,
                        account_seq=mab.account_seq,
                    ),
                )
            )

        orders = mab_daily_orders(
            avg_price,
            holdings,
            mab.seed,
            seed_remaining,
            round_idx,
            mab.n_splits,
            target_pct,
            mab.alpha,
            mab.split_ratio,
            halftime_rule=mab.halftime_rule,
            version=mab.version,
            ticker=mab.ticker,
            account_seq=mab.account_seq,
        )
        return PlanResult(orders=tuple(orders))
