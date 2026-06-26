"""The SQLite State Store backend (REQ-STATE-001-R1/R2/R3/R5).

@CODE:SPEC-STATE-001

``SqliteStateStore`` satisfies the SAME :class:`StateStorePort` as the in-memory
store, over the stdlib :mod:`sqlite3` (zero new third-party deps) in WAL mode. It
is the RECOMMENDED persistent backend for the single-worker-on-volume topology:
durable across a true reopen (restart-safe), atomic per-mutation transactions,
optimistic ``version`` concurrency, and the same monotonic order lifecycle and
single-writer lease as the in-memory store.

Money is stored as a canonical decimal **string** (TEXT), never a binary float;
the strategy-state and config maps are stored as JSON of decimal-string values.
The database file path is config/env-driven and carries no secret; nothing is
logged and the ``repr`` exposes no credential.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

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
    decimal_map_from_strings,
    decimal_map_to_strings,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_state (
    ns      TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS order_ledger (
    client_order_id TEXT PRIMARY KEY,
    account_seq     TEXT NOT NULL,
    broker_order_id TEXT,
    status          TEXT NOT NULL,
    ordered_qty     TEXT NOT NULL,
    filled_qty      TEXT NOT NULL,
    ts              TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_snapshot (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    data    TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS writer_lease (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    ttl   INTEGER
);
"""

_TERMINAL: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.FAILED}
)


class SqliteStateStore:
    """A durable :class:`StateStorePort` over stdlib ``sqlite3`` (WAL mode)."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(self._path, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.executescript(_SCHEMA)
        self._con.commit()

    def __repr__(self) -> str:
        # The file path is not a secret, but keep the repr minimal and credential-free.
        return f"{type(self).__name__}(path=<sqlite-file>)"

    # --- strategy state (R1) ---------------------------------------------------------

    def load_strategy_state(self, ns: str) -> StrategyStateRecord:
        with self._lock:
            row = self._con.execute(
                "SELECT data, version FROM strategy_state WHERE ns = ?", (ns,)
            ).fetchone()
        if row is None:
            return StrategyStateRecord(ns=ns, data={}, version=0)
        data = decimal_map_from_strings(json.loads(row[0]))
        return StrategyStateRecord(ns=ns, data=data, version=int(row[1]))

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
                    continue
                if isinstance(value, float):
                    raise ValueError("money/quantity must be a Decimal, not a float")
                merged[key] = quantize_money(value)
            new_version = current.version + 1
            payload = json.dumps(decimal_map_to_strings(merged))
            with self._con:  # atomic transaction per mutation
                self._con.execute(
                    "INSERT INTO strategy_state (ns, data, version) VALUES (?, ?, ?) "
                    "ON CONFLICT(ns) DO UPDATE SET "
                    " data = excluded.data, version = excluded.version",
                    (ns, payload, new_version),
                )
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
            prior = self.load_order(client_order_id)
            if prior is not None and not status_allows_transition(prior.status, status):
                return  # never regress a terminal status (monotonic, FD4)
            # Normalize money to 2 dp via the DTO, then persist as decimal strings.
            record = OrderLedgerRecord(
                client_order_id=client_order_id,
                account_seq=account_seq,
                broker_order_id=broker_order_id,
                status=status,
                ordered_qty=ordered_qty,
                filled_qty=filled_qty,
                ts=ts,
            )
            with self._con:
                self._con.execute(
                    "INSERT INTO order_ledger "
                    "(client_order_id, account_seq, broker_order_id, status, "
                    " ordered_qty, filled_qty, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(client_order_id) DO UPDATE SET "
                    " account_seq = excluded.account_seq, "
                    " broker_order_id = excluded.broker_order_id, "
                    " status = excluded.status, "
                    " ordered_qty = excluded.ordered_qty, "
                    " filled_qty = excluded.filled_qty, "
                    " ts = excluded.ts",
                    (
                        record.client_order_id,
                        record.account_seq,
                        record.broker_order_id,
                        record.status.value,
                        str(record.ordered_qty),
                        str(record.filled_qty),
                        record.ts,
                    ),
                )

    def load_order(self, client_order_id: str) -> OrderLedgerRecord | None:
        with self._lock:
            row = self._con.execute(
                "SELECT client_order_id, account_seq, broker_order_id, status, "
                " ordered_qty, filled_qty, ts FROM order_ledger WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return _row_to_order(row) if row is not None else None

    def resolve_order_id(self, client_order_id: str) -> str | None:
        with self._lock:
            row = self._con.execute(
                "SELECT broker_order_id FROM order_ledger WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return None if row is None else row[0]

    def list_open_orders(self, account_seq: str) -> tuple[OrderLedgerRecord, ...]:
        terminals = tuple(state.value for state in _TERMINAL)
        placeholders = ", ".join("?" for _ in terminals)
        with self._lock:
            rows = self._con.execute(
                "SELECT client_order_id, account_seq, broker_order_id, status, "
                " ordered_qty, filled_qty, ts FROM order_ledger "
                f"WHERE account_seq = ? AND status NOT IN ({placeholders})",
                (account_seq, *terminals),
            ).fetchall()
        return tuple(_row_to_order(row) for row in rows)

    # --- config snapshot (R5) --------------------------------------------------------

    def get_config(self) -> ConfigSnapshotRecord:
        with self._lock:
            row = self._con.execute(
                "SELECT data, version FROM config_snapshot WHERE id = 1"
            ).fetchone()
        if row is None:
            return ConfigSnapshotRecord(data={}, version=0)
        return ConfigSnapshotRecord(data=json.loads(row[0]), version=int(row[1]))

    def set_config(self, snapshot: Mapping[str, object], *, expected_version: int) -> int:
        with self._lock:
            current = self.get_config()
            if current.version != expected_version:
                raise ConcurrencyError(
                    f"stale config write: expected_version={expected_version} "
                    f"but current version is {current.version}"
                )
            new_version = current.version + 1
            payload = json.dumps(dict(snapshot))
            with self._con:
                self._con.execute(
                    "INSERT INTO config_snapshot (id, data, version) VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    " data = excluded.data, version = excluded.version",
                    (payload, new_version),
                )
            return new_version

    # --- single-writer lease (R3) ----------------------------------------------------

    def acquire_writer_lease(self, owner: str, *, ttl: int | None) -> Lease:
        with self._lock:
            row = self._con.execute("SELECT owner, token FROM writer_lease WHERE id = 1").fetchone()
            if row is not None and row[0] != owner:
                raise WriterLeaseHeldError(f"writer lease already held by {row[0]!r}")
            token = _token()
            with self._con:
                self._con.execute(
                    "INSERT INTO writer_lease (id, owner, token, ttl) VALUES (1, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET owner = excluded.owner, "
                    " token = excluded.token, ttl = excluded.ttl",
                    (owner, token, ttl),
                )
            return Lease(owner=owner, ttl=ttl, token=token)

    def release_writer_lease(self, lease: Lease) -> None:
        with self._lock, self._con:
            self._con.execute("DELETE FROM writer_lease WHERE id = 1 AND token = ?", (lease.token,))


def _row_to_order(row: tuple[object, ...]) -> OrderLedgerRecord:
    """Map an ``order_ledger`` row back to an :class:`OrderLedgerRecord`."""
    return OrderLedgerRecord(
        client_order_id=str(row[0]),
        account_seq=str(row[1]),
        broker_order_id=None if row[2] is None else str(row[2]),
        status=OrderState(str(row[3])),
        ordered_qty=Decimal(str(row[4])),
        filled_qty=Decimal(str(row[5])),
        ts=str(row[6]),
    )


def _token() -> str:
    """A short, unguessable lease token (matches a release to its acquire)."""
    import secrets

    return secrets.token_hex(8)
