"""Public surface of the ballast State Store + Reconciliation (P-state).

@CODE:SPEC-STATE-001

The persistence foundation for 24/7 autonomous operation: a backend-agnostic
``StateStorePort`` Protocol, frozen Decimal-money persisted DTOs, the in-memory
store (the unit-test vehicle), the recommended SQLite-on-volume backend, and the
PURE, idempotent reconciliation core. The pure core (``reconcile``) reads no clock
and does no IO; only the store backends touch IO. Money is ``Decimal`` (2 dp);
secrets / DB credentials are env-only and never logged.
"""

from __future__ import annotations

from ballast.state.memory import InMemoryStateStore
from ballast.state.models import (
    ConcurrencyError,
    ConfigSnapshotRecord,
    FillRecord,
    Lease,
    OrderLedgerRecord,
    OrderState,
    ReconMutation,
    ReconResult,
    StateSnapshot,
    StrategyStateRecord,
    WriterLeaseHeldError,
    decimal_map_from_strings,
    decimal_map_to_strings,
)
from ballast.state.ports import StateStorePort
from ballast.state.reconcile import reconcile
from ballast.state.sqlite import SqliteStateStore

__all__ = [
    "ConcurrencyError",
    "ConfigSnapshotRecord",
    "FillRecord",
    "InMemoryStateStore",
    "Lease",
    "OrderLedgerRecord",
    "OrderState",
    "ReconMutation",
    "ReconResult",
    "SqliteStateStore",
    "StateSnapshot",
    "StateStorePort",
    "StrategyStateRecord",
    "WriterLeaseHeldError",
    "decimal_map_from_strings",
    "decimal_map_to_strings",
    "reconcile",
]
