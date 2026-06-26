"""Immutable backtest-only types (REQ-BACKTEST-001 — types).

New types introduced by SPEC-BACKTEST-001 for the replay/cost/metric layer. They
mirror the CORE-001 ``frozen``/``slots`` dataclass style and quantize every money
or quantity field to two decimal places (``ROUND_HALF_UP``) at construction via the
single source of rounding, ``ballast.core.models.quantize_money``. Re-quantizing an
already-rounded value is idempotent.

This module REUSES the CORE-001 :class:`~ballast.core.models.Order` and
:class:`~ballast.core.models.Side`; it redefines neither. No ``float`` appears on
any money field here (the curve/ratio float math lives only in ``metrics.py``).

@CODE:SPEC-BACKTEST-001
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ballast.core.models import Order, Side, quantize_money


@dataclass(frozen=True, slots=True)
class OHLCBar:
    """One trading day of OHLC data plus the aligned USD->KRW rate.

    ``adj_close`` is an optional total-return / adjusted close used by baselines.
    All money fields are quantized to two places at construction.
    """

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal | None
    fx_usdkrw: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "open", quantize_money(self.open))
        object.__setattr__(self, "high", quantize_money(self.high))
        object.__setattr__(self, "low", quantize_money(self.low))
        object.__setattr__(self, "close", quantize_money(self.close))
        object.__setattr__(self, "fx_usdkrw", quantize_money(self.fx_usdkrw))
        if self.adj_close is not None:
            object.__setattr__(self, "adj_close", quantize_money(self.adj_close))


@dataclass(frozen=True, slots=True)
class Fill:
    """A realized execution of a CORE-001 :class:`Order` on a single bar."""

    order: Order
    fill_price: Decimal
    qty: Decimal
    date: date
    commission: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_price", quantize_money(self.fill_price))
        object.__setattr__(self, "qty", quantize_money(self.qty))
        object.__setattr__(self, "commission", quantize_money(self.commission))


@dataclass(frozen=True, slots=True)
class Trade:
    """A realizing SELL recorded on the ledger, tagged by its tax year."""

    side: Side
    qty: Decimal
    realized_gain_usd: Decimal
    tax_year: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "qty", quantize_money(self.qty))
        object.__setattr__(self, "realized_gain_usd", quantize_money(self.realized_gain_usd))


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One mark-to-market curve sample in BOTH USD and KRW."""

    date: date
    equity_usd: Decimal
    equity_krw: Decimal
    holdings: Decimal
    cash: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "equity_usd", quantize_money(self.equity_usd))
        object.__setattr__(self, "equity_krw", quantize_money(self.equity_krw))
        object.__setattr__(self, "holdings", quantize_money(self.holdings))
        object.__setattr__(self, "cash", quantize_money(self.cash))


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The deterministic output of one :func:`run_backtest` call.

    Ledgers are stored as ordered tuples for hashable determinism; ``fingerprint``
    summarizes the inputs so a re-run with identical inputs is verifiably identical.
    """

    equity_curve: tuple[EquityPoint, ...]
    fills: tuple[Fill, ...]
    trades: tuple[Trade, ...]
    realized_pnl_usd: Decimal
    total_tax_usd: Decimal
    start_capital_usd: Decimal
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "realized_pnl_usd", quantize_money(self.realized_pnl_usd))
        object.__setattr__(self, "total_tax_usd", quantize_money(self.total_tax_usd))
        object.__setattr__(self, "start_capital_usd", quantize_money(self.start_capital_usd))


@dataclass(frozen=True, slots=True)
class EquityCurve:
    """A simple labelled baseline curve: ordered ``(date, value_usd)`` points."""

    leverage: int
    points: tuple[tuple[date, Decimal], ...] = field(default_factory=tuple)
