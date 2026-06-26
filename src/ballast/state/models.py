"""Immutable persisted DTOs for the State Store (REQ-STATE-001-R1/R2/R4/R5).

@CODE:SPEC-STATE-001

These frozen DTOs are the durable shapes the store backends read and write and
the pure reconciliation core threads through. Money/quantity fields are
:class:`~decimal.Decimal` (never ``float``); a bare ``float`` is rejected at
construction (FD7, AC-19) so precision never silently leaks onto the money path,
and every money value is normalized to 2 dp via
:func:`ballast.core.models.quantize_money`. ``Decimal`` is serialized losslessly
as a canonical decimal **string** (never a binary float) by the store backends.

``ts`` is an **opaque injected stamp** (a plain ``str``): the layer pins no
concrete datetime type — the caller injects whatever string stamp it wants (an
ISO timestamp, an epoch string, a cycle key), and the store round-trips it as
TEXT. The ``OrderState`` lifecycle enum reuses the SPEC-ORDER-001
``SubmissionStatus`` values verbatim (no fork) and extends them with the
reconciliation terminals ``PARTIAL`` / ``FILLED`` / ``CANCELED`` / ``EXPIRED``.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator

from ballast.core.models import Side, quantize_money
from ballast.orders.models import SubmissionStatus


class ConcurrencyError(Exception):
    """Raised when a mutating call's ``expected_version`` does not match (FD3/FD6).

    Optimistic concurrency guard: a stale or second writer cannot clobber a newer
    write. The message names the namespace and the version mismatch only — never a
    secret or connection string.
    """


class WriterLeaseHeldError(Exception):
    """Raised when a second concurrent writer tries to acquire a held lease (FD6).

    The single-writer lease is owned by exactly one process; a second
    ``acquire_writer_lease`` from a different owner fails with this error so a
    stray second process cannot become a second writer.
    """


def _reject_float(value: Any) -> Any:
    """Reject bare ``float`` money inputs before Decimal coercion.

    A string (e.g. ``"80.00"``) or a ``Decimal`` passes through; a bare ``float``
    (e.g. ``80.5``) is rejected so precision never silently leaks onto the money
    path. ``bool`` is an ``int`` subclass in Python and is not affected here.
    Mirrors CORE / ORDER / ADAPTER ``_reject_float``.
    """
    if isinstance(value, float):
        raise ValueError("money/quantity must be a string or Decimal, not a float")
    return value


def _normalize_money(value: Any) -> Any:
    """Normalize an accepted money value to 2 dp (after float rejection)."""
    if isinstance(value, Decimal):
        return quantize_money(value)
    if isinstance(value, str):
        return quantize_money(Decimal(value))
    return value


def _normalize_decimal_map(value: Any) -> Any:
    """Reject floats and 2-dp-normalize every value of a ``Decimal`` map.

    Applied to ``StrategyStateRecord.data`` so each persisted strategy-state value
    is a 2-dp ``Decimal`` (never a bare ``float``).
    """
    if isinstance(value, Mapping):
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            _reject_float(raw)
            if isinstance(raw, Decimal):
                out[key] = quantize_money(raw)
            elif isinstance(raw, str):
                out[key] = quantize_money(Decimal(raw))
            else:
                raise ValueError("strategy-state values must be Decimal or decimal strings")
        return out
    return value


# A money/quantity value: never a bare float, always normalized to 2 dp.
Money = Annotated[Decimal, BeforeValidator(_normalize_money), BeforeValidator(_reject_float)]

_Frozen = ConfigDict(frozen=True, extra="forbid")


class OrderState(StrEnum):
    """The order/idempotency-ledger lifecycle status (FD4).

    The SPEC-ORDER-001 ``SubmissionStatus`` values verbatim (no fork) extended
    with the reconciliation terminals. ``_assert_submission_status_parity`` (run at
    import) guarantees the shared values never silently drift from
    ``SubmissionStatus``.
    """

    # SPEC-ORDER-001 SubmissionStatus values (verbatim, no fork).
    RECORDED = "RECORDED"  # ORDER-001 dry-run preview
    SUBMITTED = "SUBMITTED"  # ORDER-001 live, accepted
    DUPLICATE = "DUPLICATE"  # ORDER-001 idempotent dedup
    BLOCKED = "BLOCKED"  # ORDER-001 guard-blocked
    FAILED = "FAILED"  # ORDER-001 port failure
    # Reconciliation terminals (this SPEC).
    PARTIAL = "PARTIAL"  # filled_qty < ordered_qty
    FILLED = "FILLED"  # fully filled (terminal)
    CANCELED = "CANCELED"  # canceled at broker (terminal)
    EXPIRED = "EXPIRED"  # expired unfilled (terminal)


def _assert_submission_status_parity() -> None:
    """Fail fast at import if ``OrderState`` drifts from ``SubmissionStatus``."""
    for status in SubmissionStatus:
        if OrderState(status.value).value != status.value:  # pragma: no cover - invariant
            raise RuntimeError("OrderState must reuse every SubmissionStatus value verbatim")


_assert_submission_status_parity()

# Terminal statuses that a monotonic ledger lifecycle must never regress (FD4).
TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED}
)


class StrategyStateRecord(BaseModel):
    """Per-namespace durable strategy-state record (FD2).

    The durable form of ``ballast.core.models.State.data`` and the backtest
    ``_Ledger``: a ``Decimal``-valued map (VR ``V_n`` / ``pool`` / ``qty``; MAB
    ``avg_price`` / ``holdings`` / ``seed_remaining`` / ``round_idx``) plus a
    monotonically increasing ``version``.
    """

    model_config = _Frozen

    ns: str
    data: Annotated[Mapping[str, Decimal], BeforeValidator(_normalize_decimal_map)] = {}
    version: int = 0

    @field_validator("data", mode="after")
    @classmethod
    def _freeze_data(cls, value: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
        """Wrap the validated map in a read-only view (immutable snapshot).

        A loaded record's ``data`` cannot be mutated in place, so it is never a
        live alias of the store's internal state.
        """
        return MappingProxyType(dict(value))


class OrderLedgerRecord(BaseModel):
    """A durable order/idempotency-ledger record keyed by ``client_order_id`` (FD4).

    The durable, cross-process form of the ORDER-001 in-memory ledger and the
    ADAPTER-002 ``client_order_id -> orderId`` map. ``status`` advances along the
    monotonic lifecycle and never regresses a terminal state.
    """

    model_config = _Frozen

    client_order_id: str
    account_seq: str
    broker_order_id: str | None
    status: OrderState
    ordered_qty: Money
    filled_qty: Money
    ts: str


class ConfigSnapshotRecord(BaseModel):
    """A generic, durable worker<->dashboard config snapshot (FD5).

    Deliberately minimal and untyped here: the typed Config model is the Runner
    SPEC's concern. Holds an opaque JSON-serializable mapping (money values as
    decimal strings) plus a ``version``.
    """

    model_config = _Frozen

    data: Mapping[str, Any] = {}
    version: int = 0


class FillRecord(BaseModel):
    """An executed fill injected into the pure reconciliation core (FD7).

    Sourced by the caller from the ADAPTER-001 read port and passed into
    :func:`ballast.state.reconcile.reconcile`. ``commission`` defaults to ``0`` and
    feeds the engine-identical buy/sell math.
    """

    model_config = _Frozen

    fill_id: str
    client_order_id: str | None
    broker_order_id: str | None
    side: Side
    qty: Money
    price: Money
    commission: Money = Decimal("0")
    ts: str


class ReconMutation(BaseModel):
    """One inspectable change produced by reconciliation (FD7).

    ``kind`` is ``"ledger_status"`` (an order status transition) or
    ``"strategy_state"`` (a position/avg-cost update). The caller persists these
    via the store atomically (R3); they are never applied by the pure core.
    """

    model_config = _Frozen

    kind: str
    target: str
    status: OrderState | None = None
    note: str | None = None


class StateSnapshot(BaseModel):
    """The current persisted snapshot fed into reconciliation (FD7).

    ``applied_fills`` is the idempotency watermark (``fill_id -> applied qty``) that
    makes the pure ``reconcile`` converge on re-run without any store access.
    """

    model_config = _Frozen

    strategy: Mapping[str, StrategyStateRecord] = {}
    orders: Mapping[str, OrderLedgerRecord] = {}
    applied_fills: Mapping[str, Decimal] = {}


class ReconResult(BaseModel):
    """The result of one :func:`reconcile` call: new snapshot + inspectable mutations."""

    model_config = _Frozen

    snapshot: StateSnapshot
    mutations: tuple[ReconMutation, ...] = ()


class Lease(BaseModel):
    """A single-writer lease handle (FD6).

    ``owner`` is the sole writer; ``token`` matches a release to its acquire;
    ``ttl`` is an opaque, injected value (the store reads no clock).
    """

    model_config = _Frozen

    owner: str
    ttl: int | None = None
    token: str = ""


def decimal_map_to_strings(data: Mapping[str, Decimal]) -> dict[str, str]:
    """Serialize a ``Decimal`` map to canonical decimal **strings** (never floats)."""
    return {key: str(value) for key, value in data.items()}


def decimal_map_from_strings(data: Mapping[str, str]) -> dict[str, Decimal]:
    """Deserialize a canonical-decimal-string map back to ``Decimal`` losslessly."""
    return {key: Decimal(value) for key, value in data.items()}
