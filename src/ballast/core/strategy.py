"""The Strategy protocol and its result type (REQ-CORE-001-R2, REQ-STRATEGY-001-R2).

A pure structural contract that the VR and MAB engines satisfy in later SPECs.
It prescribes no IO and ships no concrete implementation — only the shape.

SPEC-STRATEGY-001 (R2) enriches the contract so a strategy can surface its
recomputed, persist-worthy state to the driver alongside its orders, as a
namespace-scoped ``Decimal`` state delta. ``plan_orders`` now returns a small
immutable :class:`PlanResult` carrying both the orders and that delta. The delta
is opaque to the engine (it applies known keys to its ledger and ignores the
rest), so the engine stays generic and never reaches into strategy internals.

@CODE:SPEC-CORE-001
@CODE:SPEC-STRATEGY-001
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from ballast.core.config import Config
from ballast.core.models import Market, Order, State


@dataclass(frozen=True, slots=True)
class PlanResult:
    """The result of one :meth:`Strategy.plan_orders` call (REQ-STRATEGY-001-R2).

    ``orders`` are the concrete orders the strategy wants to place for the
    snapshot. ``state_delta`` is a namespace-scoped mapping of recomputed,
    persist-worthy state keys to ``Decimal`` values that the driver should carry
    into the next cycle (VR surfaces at least ``{"V_n": V2}``; a strategy with no
    evolving internal state, such as MAB, surfaces an empty delta). The delta
    carries ``Decimal`` values only — no ``float`` ever reaches the money path.
    """

    orders: tuple[Order, ...] = ()
    state_delta: Mapping[str, Decimal] = field(default_factory=dict)


@runtime_checkable
class Strategy(Protocol):
    """Structural contract for a rebalancing strategy.

    Conforming objects expose a ``cadence`` and namespace ``ns`` and turn a
    market snapshot plus persisted state into a :class:`PlanResult` — the orders
    to place plus a namespace-scoped state delta. The method is pure: time and
    price arrive via ``market``, never the wall clock.
    """

    cadence: Literal["daily", "cycle"]
    ns: str

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        """Return the orders plus state delta this strategy wants for the snapshot."""
        ...
