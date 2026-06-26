"""The in-memory State Store (REQ-STATE-001-R5, FD8).

@CODE:SPEC-STATE-001

``InMemoryStateStore`` structurally satisfies :class:`StateStorePort` with no DB,
no network, and no credentials, and is the sole vehicle for unit tests. It honors
the same single-writer-lease, optimistic-concurrency, apply-delta, atomic, and
read-your-writes semantics as a persistent backend (a shared port-conformance
suite asserts in-memory <-> SQLite parity).

Atomicity: every mutation runs under a re-entrant lock and replaces the record
reference in one assignment, so a read observes either the old or the new record,
never a torn one. A failed mutation (a version mismatch) leaves the prior record
untouched (the version check happens before any write).
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Mapping
from decimal import Decimal

from ballast.core.models import quantize_money
from ballast.state._shared import known_keys_for, status_allows_transition
from ballast.state.models import (
    ConcurrencyError,
    ConfigSnapshotRecord,
    Lease,
    OrderLedgerRecord,
    OrderState,
    StrategyStateRecord,
    WriterLeaseHeldError,
)


class InMemoryStateStore:
    """A network-free, credential-free :class:`StateStorePort` implementation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._strategy: dict[str, StrategyStateRecord] = {}
        self._orders: dict[str, OrderLedgerRecord] = {}
        self._config = ConfigSnapshotRecord(data={}, version=0)
        self._lease: Lease | None = None

    # --- strategy state (R1) ---------------------------------------------------------

    def load_strategy_state(self, ns: str) -> StrategyStateRecord:
        with self._lock:
            existing = self._strategy.get(ns)
            if existing is not None:
                return existing
            return StrategyStateRecord(ns=ns, data={}, version=0)

    def apply_strategy_delta(
        self, ns: str, delta: Mapping[str, Decimal], *, expected_version: int
    ) -> int:
        with self._lock:
            current = self.load_strategy_state(ns)
            if current.version != expected_version:
                raise ConcurrencyError(
                    f"stale write to ns={ns!r}: expected_version={expected_version} "
                    f"but current version is {current.version}"
                )
            merged = dict(current.data)
            known = known_keys_for(ns)
            for key, value in delta.items():
                if key not in known:
                    continue  # unknown key ignored (forward-compatible, FD3)
                if isinstance(value, float):
                    raise ValueError("money/quantity must be a Decimal, not a float")
                merged[key] = quantize_money(value)
            new_version = current.version + 1
            # Atomic swap: build the new record fully, then replace in one assignment.
            self._strategy[ns] = StrategyStateRecord(ns=ns, data=merged, version=new_version)
            return new_version

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
        with self._lock:
            prior = self._orders.get(client_order_id)
            if prior is not None and not status_allows_transition(prior.status, status):
                return  # never regress a terminal status (monotonic, FD4)
            self._orders[client_order_id] = OrderLedgerRecord(
                client_order_id=client_order_id,
                account_seq=account_seq,
                broker_order_id=broker_order_id,
                status=status,
                ordered_qty=ordered_qty,
                filled_qty=filled_qty,
                ts=ts,
            )

    def load_order(self, client_order_id: str) -> OrderLedgerRecord | None:
        with self._lock:
            return self._orders.get(client_order_id)

    def resolve_order_id(self, client_order_id: str) -> str | None:
        with self._lock:
            record = self._orders.get(client_order_id)
            return record.broker_order_id if record is not None else None

    def list_open_orders(self, account_seq: str) -> tuple[OrderLedgerRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._orders.values()
                if record.account_seq == account_seq and record.status not in _TERMINAL
            )

    # --- config snapshot (R5) --------------------------------------------------------

    def get_config(self) -> ConfigSnapshotRecord:
        with self._lock:
            return self._config

    def set_config(self, snapshot: Mapping[str, object], *, expected_version: int) -> int:
        with self._lock:
            if self._config.version != expected_version:
                raise ConcurrencyError(
                    f"stale config write: expected_version={expected_version} "
                    f"but current version is {self._config.version}"
                )
            new_version = self._config.version + 1
            self._config = ConfigSnapshotRecord(data=dict(snapshot), version=new_version)
            return new_version

    # --- single-writer lease (R3) ----------------------------------------------------

    def acquire_writer_lease(self, owner: str, *, ttl: int | None) -> Lease:
        with self._lock:
            held = self._lease
            if held is not None and held.owner != owner:
                raise WriterLeaseHeldError(f"writer lease already held by {held.owner!r}")
            lease = Lease(owner=owner, ttl=ttl, token=secrets.token_hex(8))
            self._lease = lease
            return lease

    def release_writer_lease(self, lease: Lease) -> None:
        with self._lock:
            held = self._lease
            if held is not None and held.token == lease.token:
                self._lease = None


# Terminal statuses excluded from ``list_open_orders``.
_TERMINAL: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.FAILED}
)

__all__ = ["InMemoryStateStore"]
