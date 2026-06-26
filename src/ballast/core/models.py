"""Immutable domain types for the ballast core (REQ-CORE-001-R1).

All monetary and quantity values are :class:`~decimal.Decimal` (never ``float``)
and are normalized to ``DEFAULT_ROUND_DIGITS`` decimal places at construction via
``ROUND_HALF_UP``. Quantization is idempotent: re-normalizing an already-rounded
value leaves it unchanged.

This module is pure: it imports only the standard library and depends on nothing
else in the package.

@CODE:SPEC-CORE-001
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

# The single source of truth for money/quantity precision.
DEFAULT_ROUND_DIGITS = 2


def quantize_money(value: Decimal, digits: int = DEFAULT_ROUND_DIGITS) -> Decimal:
    """Quantize ``value`` to ``digits`` decimal places using ``ROUND_HALF_UP``.

    Quantization is idempotent and always returns a :class:`~decimal.Decimal`.
    """
    quantum = Decimal(1).scaleb(-digits)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


class Side(StrEnum):
    """Order direction for a concrete :class:`Order`."""

    BUY = "BUY"
    SELL = "SELL"


class DecisionSide(StrEnum):
    """Direction of an abstract :class:`Decision` (HOLD has no order)."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderType(StrEnum):
    """Supported order types at the broker boundary."""

    RESERVED_LIMIT = "reserved_limit"
    LOC = "LOC"
    MARKET = "market"


@dataclass(frozen=True, slots=True)
class InstrumentMeta:
    """Static metadata for a tradable instrument.

    ``default_target_pct`` / ``default_band`` are optional instrument-level
    defaults consumed by the registry resolution chain (REQ-CORE-001-R3).
    """

    ticker: str
    leverage: int
    underlying: str
    default_target_pct: Decimal | None
    default_band: Decimal | None


@dataclass(frozen=True, slots=True)
class Order:
    """A concrete, broker-ready order.

    ``qty`` and ``limit_price`` are normalized to 2 decimal places. A ``None``
    ``limit_price`` is valid for market orders.
    """

    side: Side
    ticker: str
    qty: Decimal
    limit_price: Decimal | None
    order_type: OrderType
    account_seq: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "qty", quantize_money(self.qty))
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", quantize_money(self.limit_price))


@dataclass(frozen=True, slots=True)
class Decision:
    """An abstract rebalance/trade intent that precedes an :class:`Order`."""

    side: DecisionSide
    target_amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_amount", quantize_money(self.target_amount))


@dataclass(frozen=True, slots=True)
class Market:
    """Read-only market snapshot for a ticker.

    The only legitimate source of "now"/price for the pure core: time and price
    are injected here rather than read from the wall clock or network.
    """

    ticker: str
    current_price: Decimal
    fx_rate: Decimal
    is_open: bool
    is_holiday: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_price", quantize_money(self.current_price))
        object.__setattr__(self, "fx_rate", quantize_money(self.fx_rate))


@dataclass(frozen=True, slots=True)
class State:
    """Per-strategy persisted-state container, addressed by namespace ``ns``."""

    ns: str
    data: dict[str, Decimal] = field(default_factory=dict)
