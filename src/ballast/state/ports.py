"""The backend-agnostic ``StateStorePort`` Protocol (REQ-STATE-001-R1/R2/R3/R5).

@CODE:SPEC-STATE-001

A ``runtime_checkable typing.Protocol`` prescribing durable, namespaced reads and
writes for three record families (strategy state, order ledger, config snapshot)
plus the single-writer/lease primitives (FD1). It prescribes no concrete transport:
the in-memory, SQLite, and (future) Postgres impls all satisfy it structurally.
Mirrors the SPEC-ADAPTER-001 / SPEC-ORDER-001 port style.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ballast.state.models import (
    ConfigSnapshotRecord,
    Lease,
    OrderLedgerRecord,
    OrderState,
    StrategyStateRecord,
)


@runtime_checkable
class StateStorePort(Protocol):
    """Durable, namespaced state persistence with single-writer semantics (FD1)."""

    # --- strategy state (R1) ---------------------------------------------------------

    def load_strategy_state(self, ns: str) -> StrategyStateRecord:
        """Return the durable strategy-state record for ``ns`` (empty at version 0)."""
        ...

    def apply_strategy_delta(
        self, ns: str, delta: Mapping[str, Decimal], *, expected_version: int
    ) -> int:
        """Merge ``delta`` into ``ns`` (known keys only) and return the new version."""
        ...

    # --- order / idempotency ledger (R2) ---------------------------------------------

    def upsert_order(
        self,
        client_order_id: str,
        *,
        account_seq: str,
        broker_order_id: str | None,
        status: OrderState,
        ordered_qty: Decimal,
        filled_qty: Decimal,
        ts: str,
    ) -> None:
        """Durably upsert an order ledger record (monotonic, non-regressing status)."""
        ...

    def load_order(self, client_order_id: str) -> OrderLedgerRecord | None:
        """Return the ledger record for ``client_order_id``, or ``None`` if unknown."""
        ...

    def resolve_order_id(self, client_order_id: str) -> str | None:
        """Return the persisted ``broker_order_id`` (cross-process cancel hook), or ``None``."""
        ...

    def list_open_orders(self, account_seq: str) -> tuple[OrderLedgerRecord, ...]:
        """Return the open (non-terminal) orders scoped to ``account_seq``."""
        ...

    # --- config snapshot (R5) --------------------------------------------------------

    def get_config(self) -> ConfigSnapshotRecord:
        """Return the durable generic config snapshot (empty at version 0)."""
        ...

    def set_config(self, snapshot: Mapping[str, object], *, expected_version: int) -> int:
        """Persist the generic config snapshot and return the new version."""
        ...

    # --- single-writer lease (R3) ----------------------------------------------------

    def acquire_writer_lease(self, owner: str, *, ttl: int | None) -> Lease:
        """Grant the sole-writer lease to ``owner`` (a second writer is rejected)."""
        ...

    def release_writer_lease(self, lease: Lease) -> None:
        """Release a previously acquired writer lease."""
        ...
