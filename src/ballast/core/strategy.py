"""The Strategy protocol (REQ-CORE-001-R2).

A pure structural contract that the VR and MAB engines satisfy in later SPECs.
It prescribes no IO and ships no concrete implementation — only the shape.

@CODE:SPEC-CORE-001
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from ballast.core.config import Config
from ballast.core.models import Market, Order, State


@runtime_checkable
class Strategy(Protocol):
    """Structural contract for a rebalancing strategy.

    Conforming objects expose a ``cadence`` and namespace ``ns`` and turn a
    market snapshot plus persisted state into a list of concrete orders. The
    method is pure: time and price arrive via ``market``, never the wall clock.
    """

    cadence: Literal["daily", "cycle"]
    ns: str

    def plan_orders(self, market: Market, state: State, cfg: Config) -> list[Order]:
        """Return the orders this strategy wants to place for the snapshot."""
        ...
