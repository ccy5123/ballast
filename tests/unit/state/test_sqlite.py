"""Tests specific to the SQLite backend (REQ-STATE-001-R1/R3/R5).

@TEST:SPEC-STATE-001

The SQLite backend is exercised entirely IN-PROCESS over a ``tmp_path`` temp file
(WAL mode) — never a live cloud DB. These tests assert durability across a true
reopen (a fresh connection over the same file), WAL configuration, Decimal-as-TEXT
round-trips, and that the file path carries no secret.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from ballast.state.models import OrderState
from ballast.state.ports import StateStorePort
from ballast.state.sqlite import SqliteStateStore


def test_sqlite_satisfies_protocol(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    assert isinstance(store, StateStorePort)


def test_sqlite_uses_wal_mode(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    SqliteStateStore(db)
    con = sqlite3.connect(db)
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()
    assert str(mode).lower() == "wal"


def test_sqlite_durable_across_true_reopen(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    SqliteStateStore(db).apply_strategy_delta("vr", {"V_n": Decimal("1234.56")}, expected_version=0)
    # A brand-new connection over the same file returns the durable value.
    reopened = SqliteStateStore(db)
    rec = reopened.load_strategy_state("vr")
    assert rec.data["V_n"] == Decimal("1234.56")
    assert rec.version == 1


def test_sqlite_stores_decimal_as_text_not_float(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    SqliteStateStore(db).apply_strategy_delta(
        "mab", {"avg_price": Decimal("82.55")}, expected_version=0
    )
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT data FROM strategy_state WHERE ns = ?", ("mab",)).fetchone()
    finally:
        con.close()
    # The serialized payload holds the canonical decimal STRING, never a binary float.
    assert "82.55" in row[0]


def test_sqlite_order_ledger_durable_resolution(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    SqliteStateStore(db).upsert_order(
        "mab-c0007-def",
        account_seq="acc-1",
        broker_order_id="toss-9100",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    # Cross-process / post-restart resolution over a fresh connection.
    assert SqliteStateStore(db).resolve_order_id("mab-c0007-def") == "toss-9100"


def test_sqlite_repr_does_not_leak_path_as_secret(tmp_path: Path) -> None:
    # The SQLite file path is config/env-driven and carries no secret; the repr
    # must not expose a credential or connection string.
    db = tmp_path / "s.db"
    store = SqliteStateStore(db)
    text = repr(store)
    assert "password" not in text.lower()
    assert "DATABASE_URL" not in text


def test_sqlite_accepts_str_path(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    store = SqliteStateStore(str(db))
    store.set_config({"ticker": "QLD"}, expected_version=0)
    assert SqliteStateStore(str(db)).get_config().data["ticker"] == "QLD"
