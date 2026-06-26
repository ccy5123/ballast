"""Shared fixtures and stub strategies for the backtest test suite.

@TEST:SPEC-BACKTEST-001
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from ballast.backtest.types import OHLCBar
from ballast.core.config import Config
from ballast.core.models import Market, Order, OrderType, Side, State
from ballast.core.strategy import PlanResult

_TICKER = "TQQQ"
_ACCT = "0001"


def bar(
    d: date,
    o: str,
    h: str,
    low: str,
    c: str,
    *,
    adj: str | None = None,
    fx: str = "1300.00",
) -> OHLCBar:
    """Build an :class:`OHLCBar` from string literals (Decimal money)."""
    return OHLCBar(
        date=d,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        adj_close=None if adj is None else Decimal(adj),
        fx_usdkrw=Decimal(fx),
    )


class NoopStrategy:
    """A conforming strategy that never places an order (flat-curve tests)."""

    def __init__(self, *, cadence: Literal["daily", "cycle"] = "daily", ns: str = "vr") -> None:
        self.cadence: Literal["daily", "cycle"] = cadence
        self.ns = ns

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        return PlanResult()


class OneShotBuyStrategy:
    """Buys ``qty`` shares (LOC at ``limit``) on the first trigger only, then idles."""

    def __init__(
        self,
        *,
        qty: str = "10.00",
        limit: str = "100.00",
        cadence: Literal["daily", "cycle"] = "daily",
        ns: str = "vr",
        ticker: str = _TICKER,
    ) -> None:
        self.cadence: Literal["daily", "cycle"] = cadence
        self.ns = ns
        self._qty = Decimal(qty)
        self._limit = Decimal(limit)
        self._ticker = ticker
        self.calls = 0

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        self.calls += 1
        if self.calls > 1:
            return PlanResult()
        return PlanResult(
            orders=(
                Order(
                    side=Side.BUY,
                    ticker=self._ticker,
                    qty=self._qty,
                    limit_price=self._limit,
                    order_type=OrderType.LOC,
                    account_seq=_ACCT,
                ),
            )
        )


class CountingStrategy:
    """Counts how many times it is triggered; never trades.

    Also records the state-keys the engine synthesized on each trigger so a test
    can assert the correct VR vs MAB ``State`` shape was built.
    """

    def __init__(self, *, cadence: Literal["daily", "cycle"], ns: str) -> None:
        self.cadence: Literal["daily", "cycle"] = cadence
        self.ns = ns
        self.calls = 0
        self.seen_state_keys: list[frozenset[str]] = []

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        self.calls += 1
        self.seen_state_keys.append(frozenset(state.data))
        return PlanResult()
