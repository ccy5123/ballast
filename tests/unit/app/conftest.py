"""Shared fixtures for the Runner / app test suite (REQ-RUNNER-001).

@TEST:SPEC-RUNNER-001

Everything is in-memory and deterministic: the Runner is exercised ONLY against
the STATE-001 ``InMemoryStateStore`` and the ORDER-001 ``RecordingBrokerOrderPort``
(or a mock ``BrokerOrderPort``) — no real Toss network, no real DB, no
credentials. Time / market / lease ``owner`` / ``ttl`` are injected for
determinism. A configurable ``_FakeStrategy`` returns a fixed ``PlanResult`` so
the Runner's coordination can be asserted independently of VR/MAB logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from ballast.app import RunnerConfig
from ballast.core.config import Config
from ballast.core.models import Market, Order, OrderType, Side, State
from ballast.core.strategy import PlanResult
from ballast.orders.recording import RecordingBrokerOrderPort
from ballast.state.memory import InMemoryStateStore

# A minimal valid strategy-domain Config (the operational RunnerConfig is distinct
# and complementary; this stays the source of truth for max_position_pct etc.).
_EXAMPLE_CONFIG_YAML = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true

execution:
  broker: toss
  dry_run: true
  max_position_pct: "0.95"

instruments:
  QLD:
    leverage: 2
    underlying: NDX
    default_target_pct: "0.60"
    default_band: "0.10"

strategies:
  vr:
    account_seq: "0001"
    ticker: QLD
  mab:
    account_seq: "0002"
    ticker: QLD
"""


class _FakeStrategy:
    """A configurable, structurally-conforming ``Strategy`` (returns a fixed plan).

    Records every ``plan_orders`` call so a test can assert it was invoked exactly
    once and that ``market`` is the only source of "now"/price (the method reads no
    wall clock).
    """

    def __init__(
        self,
        ns: str,
        *,
        orders: tuple[Order, ...] = (),
        state_delta: Mapping[str, Decimal] | None = None,
        cadence: Literal["daily", "cycle"] = "cycle",
    ) -> None:
        self.ns = ns
        self.cadence: Literal["daily", "cycle"] = cadence
        self._orders = orders
        self._state_delta: Mapping[str, Decimal] = state_delta if state_delta is not None else {}
        self.calls: list[tuple[Market, State, Config]] = []

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        self.calls.append((market, state, cfg))
        return PlanResult(orders=self._orders, state_delta=dict(self._state_delta))


def _buy_order(ticker: str = "QLD", account_seq: str = "0001", qty: str = "3") -> Order:
    """A reserved-limit BUY (maps to LIMIT + DAY, carries a limit_price)."""
    return Order(
        side=Side.BUY,
        ticker=ticker,
        qty=Decimal(qty),
        limit_price=Decimal("80.00"),
        order_type=OrderType.RESERVED_LIMIT,
        account_seq=account_seq,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """The strategy-domain Config (source of truth for max_position_pct)."""
    path = tmp_path / "config.yaml"
    path.write_text(_EXAMPLE_CONFIG_YAML, encoding="utf-8")
    return Config.load(path)


@pytest.fixture
def store() -> InMemoryStateStore:
    """A fresh, network-free, credential-free in-memory State Store."""
    return InMemoryStateStore()


@pytest.fixture
def port() -> RecordingBrokerOrderPort:
    """A network-free, idempotent recording BrokerOrderPort (no real Toss)."""
    return RecordingBrokerOrderPort()


@pytest.fixture
def market() -> Market:
    """An injected open-market snapshot (the only source of now/price/hours)."""
    return Market(
        ticker="QLD",
        current_price=Decimal("80.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )


@pytest.fixture
def runner_config() -> RunnerConfig:
    """A live (dry_run=False) operational config seeded into the snapshot."""
    return RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000.00"),
        allocation={"vr": Decimal("0.6"), "mab": Decimal("0.4")},
        dry_run=False,
        kill_switch=False,
    )
