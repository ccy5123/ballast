"""Shared fixtures for the State Store suite (REQ-STATE-001-R5, FD8).

@TEST:SPEC-STATE-001

Parametrizes the shared port-conformance suite over BOTH ``InMemoryStateStore``
and ``SqliteStateStore`` (over a ``tmp_path`` temp file, WAL) so the two backends
are asserted behaviorally identical (in-memory <-> SQLite parity). No live cloud
DB is ever contacted; SQLite uses a temp file only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ballast.state.memory import InMemoryStateStore
from ballast.state.ports import StateStorePort
from ballast.state.sqlite import SqliteStateStore

# A factory that (re)opens a store over the same backing — used to simulate a
# worker restart (a fresh store instance over the same durable backing).
StoreFactory = Callable[[], StateStorePort]


@pytest.fixture(params=["memory", "sqlite"])
def store_factory(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StoreFactory]:
    """Yield a factory that opens a fresh store over the SAME backing each call.

    For ``memory`` the backing is a shared in-process dict store (one instance);
    "restart" is modeled as re-reading the same live instance. For ``sqlite`` the
    backing is a temp file; "restart" opens a brand-new connection over that file,
    proving durability across reopen.
    """
    if request.param == "memory":
        store = InMemoryStateStore()

        def factory() -> StateStorePort:
            return store

        yield factory
    else:
        db_path = tmp_path / "state.db"

        def factory() -> StateStorePort:
            return SqliteStateStore(db_path)

        yield factory


@pytest.fixture
def store(store_factory: StoreFactory) -> StateStorePort:
    """A single store instance for non-restart conformance assertions."""
    return store_factory()
