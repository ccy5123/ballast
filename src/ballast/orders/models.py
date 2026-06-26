"""Broker-neutral Order Manager DTOs and enums (REQ-ORDER-001-R1).

@CODE:SPEC-ORDER-001

These immutable DTOs are the broker-agnostic order shapes the manager threads
through its guards and ledger. Money/quantity fields are :class:`~decimal.Decimal`
(never ``float``); a bare ``float`` is rejected at construction (FD6, AC-17) so
precision never silently leaks onto the money path, and every money value is
normalized to 2 dp via :func:`ballast.core.models.quantize_money`.

They are deliberately distinct from the CORE ``Order`` and the ADAPTER
``OrderRecord``; the only bridge from CORE to here is
:func:`ballast.orders.mapping.order_to_intent` (FD1).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

from ballast.core.models import Side, quantize_money


def _reject_float(value: Any) -> Any:
    """Reject bare ``float`` money inputs before Decimal coercion.

    A string (e.g. ``"80.00"``) or a ``Decimal`` passes through; a bare ``float``
    (e.g. ``80.5``) is rejected so precision never silently leaks onto the money
    path. ``bool`` is an ``int`` subclass in Python and is not affected here.
    Mirrors CORE ``Config._reject_float`` and ADAPTER ``models._reject_float``.
    """
    if isinstance(value, float):
        raise ValueError("money/quantity must be a string or Decimal, not a float")
    return value


def _normalize_money(value: Any) -> Any:
    """Normalize an accepted money value to 2 dp (after float rejection)."""
    if isinstance(value, Decimal):
        return quantize_money(value)
    return value


# A money/quantity value: never a bare float, always normalized to 2 dp.
Money = Annotated[Decimal, BeforeValidator(_normalize_money), BeforeValidator(_reject_float)]
# An optional money/quantity value (``None`` allowed, e.g. a price-less LOC).
OptionalMoney = Annotated[
    Decimal | None, BeforeValidator(_normalize_money), BeforeValidator(_reject_float)
]

_Frozen = ConfigDict(frozen=True, extra="forbid")


class Tif(StrEnum):
    """Time-in-force for a broker-neutral :class:`OrderIntent` (FD1)."""

    DAY = "DAY"
    CLS = "CLS"


class OrderKind(StrEnum):
    """Order kind for a broker-neutral :class:`OrderIntent` (FD1)."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class SubmissionStatus(StrEnum):
    """Lifecycle status of a submission attempt (FD6)."""

    RECORDED = "RECORDED"  # dry-run preview only
    SUBMITTED = "SUBMITTED"  # live, accepted by the port
    DUPLICATE = "DUPLICATE"  # idempotent dedup hit
    BLOCKED = "BLOCKED"  # a guard blocked it (kill-switch / no headroom)
    FAILED = "FAILED"  # the port raised or returned a failure


class OrderIntent(BaseModel):
    """A broker-neutral, immutable order intent (FD1).

    Carries the deterministic ``client_order_id`` (the idempotency key) plus the
    mapped ``kind``/``tif``/``limit_price``. ``limit_price`` is ``None`` for a
    MARKET order and may be ``None`` for a CLS (Limit-On-Close) LIMIT.
    """

    model_config = _Frozen

    client_order_id: str
    account_seq: str
    side: Side
    ticker: str
    qty: Money
    kind: OrderKind
    tif: Tif
    limit_price: OptionalMoney = None


class SubmissionResult(BaseModel):
    """The immutable outcome of recording or submitting an :class:`OrderIntent` (FD6)."""

    model_config = _Frozen

    client_order_id: str
    status: SubmissionStatus
    broker_order_id: str | None = None
    reason: str | None = None


class OrderPlan(BaseModel):
    """An inspectable dry-run / live preview: the intents and their results (FD4)."""

    model_config = _Frozen

    intents: tuple[OrderIntent, ...]
    results: tuple[SubmissionResult, ...]
