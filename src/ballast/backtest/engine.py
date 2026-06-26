"""Deterministic replay engine (REQ-BACKTEST-001-R1).

``run_backtest`` iterates OHLC bars in ascending ``date`` order and, for each bar,
builds a CORE-001 :class:`Market` snapshot (``current_price = close``,
``fx_rate = fx_usdkrw``) and a FRESH immutable :class:`State` from the engine's own
bookkeeping, exposing exactly the keys the chosen strategy reads:

* VR (``ns == "vr"``): ``V_n`` / ``pool`` / ``qty``;
* MAB (``ns == "mab"``): ``avg_price`` / ``holdings`` / ``seed_remaining`` / ``round_idx``.

The trigger cadence is read from ``strategy.cadence``: ``"daily"`` fires every bar,
``"cycle"`` once per ``cycle_length`` slice (monthly, or every-N-bars for an ``int``).
On a trigger the engine calls ``strategy.plan_orders``, routes each order through the
close-based :func:`simulate_fill`, applies the fills to its bookkeeping, and accrues
per-year realized gains; on every bar it records one mark-to-market
:class:`EquityPoint` in BOTH USD and KRW. At each calendar-year boundary it applies
:meth:`CostModel.annual_tax` (deduction resets per year). The engine is pure toward
its inputs: it never mutates ``Config`` or the passed-in ``State``.

This layer MAY use pandas (a ``DataFrame`` is normalized to ``OHLCBar`` at the
boundary); the inner loop only ever sees the typed sequence.

@CODE:SPEC-BACKTEST-001
"""

from __future__ import annotations

import hashlib
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from ballast.backtest.costs import CostModel
from ballast.backtest.fills import simulate_fill
from ballast.backtest.types import (
    BacktestResult,
    EquityPoint,
    Fill,
    OHLCBar,
    Trade,
)
from ballast.core.config import Config
from ballast.core.models import Market, Order, Side, State, quantize_money
from ballast.core.strategy import Strategy

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

_ZERO = Decimal("0")

# A shared default cost model. CostModel is frozen and immutable, so a single
# module-level instance is interchangeable with a fresh one and avoids a mutable
# call in argument defaults (ruff B008).
_DEFAULT_COSTS = CostModel()


@dataclass(slots=True)
class _Ledger:
    """Mutable engine bookkeeping (the only stateful part; the core stays pure)."""

    cash_usd: Decimal
    holdings: Decimal
    avg_price: Decimal
    seed_remaining: Decimal
    round_idx: Decimal
    v_n: Decimal
    realized_pnl_usd: Decimal = _ZERO
    total_tax_usd: Decimal = _ZERO


def run_backtest(
    strategy: Strategy,
    bars: Sequence[OHLCBar] | pd.DataFrame,
    cfg: Config,
    *,
    start_state: State,
    cycle_length: Literal["monthly"] | int = "monthly",
    index: Sequence[tuple[date, Decimal]] | None = None,
    costs: CostModel = _DEFAULT_COSTS,
) -> BacktestResult:
    """Replay ``bars`` through ``strategy`` and return a deterministic result."""
    series = _normalize_bars(bars)
    ledger = _seed_ledger(start_state)
    start_capital = ledger.cash_usd + ledger.holdings * (series[0].close if series else _ZERO)

    fills: list[Fill] = []
    trades: list[Trade] = []
    curve: list[EquityPoint] = []
    year_gains: dict[int, Decimal] = {}
    year_fx: dict[int, Decimal] = {}

    for bar_index, bar in enumerate(series):
        if _is_trigger(strategy.cadence, cycle_length, series, bar_index):
            market = Market(
                ticker=_strategy_ticker(strategy, cfg),
                current_price=bar.close,
                fx_rate=bar.fx_usdkrw,
                is_open=True,
                is_holiday=False,
            )
            state = _build_state(strategy.ns, ledger)
            orders = strategy.plan_orders(market, state, cfg)
            for order in orders:
                _execute(order, bar, costs, ledger, fills, trades, year_gains)
        # Track the latest FX seen in each calendar year (for the deduction).
        year_fx[bar.date.year] = bar.fx_usdkrw
        curve.append(_mark_to_market(bar, ledger))

    _apply_annual_tax(costs, ledger, year_gains, year_fx, curve)

    return BacktestResult(
        equity_curve=tuple(curve),
        fills=tuple(fills),
        trades=tuple(trades),
        realized_pnl_usd=quantize_money(ledger.realized_pnl_usd),
        total_tax_usd=quantize_money(ledger.total_tax_usd),
        start_capital_usd=quantize_money(start_capital),
        fingerprint=_fingerprint(series, start_state, cycle_length, costs),
    )


def _seed_ledger(start_state: State) -> _Ledger:
    """Build the mutable ledger from the initial ``State`` (never mutate it)."""
    data = start_state.data
    cash = data.get("pool", data.get("seed_remaining", _ZERO))
    return _Ledger(
        cash_usd=cash,
        holdings=data.get("qty", data.get("holdings", _ZERO)),
        avg_price=data.get("avg_price", _ZERO),
        seed_remaining=data.get("seed_remaining", cash),
        round_idx=data.get("round_idx", _ZERO),
        v_n=data.get("V_n", _ZERO),
    )


def _build_state(ns: str, ledger: _Ledger) -> State:
    """Construct a FRESH immutable ``State`` exposing the namespace's keys."""
    if ns == "mab":
        data = {
            "avg_price": ledger.avg_price,
            "holdings": ledger.holdings,
            "seed_remaining": ledger.seed_remaining,
            "round_idx": ledger.round_idx,
        }
    else:  # VR (and trivial vr-namespaced stubs)
        data = {
            "V_n": ledger.v_n,
            "pool": ledger.cash_usd,
            "qty": ledger.holdings,
        }
    return State(ns=ns, data=data)


def _execute(
    order: Order,
    bar: OHLCBar,
    costs: CostModel,
    ledger: _Ledger,
    fills: list[Fill],
    trades: list[Trade],
    year_gains: dict[int, Decimal],
) -> None:
    """Fill one order (if it fills) and apply it to the ledger."""
    fill = simulate_fill(order, bar, slippage_bps=costs.slippage_bps)
    if fill is None:
        return  # a miss leaves holdings/cash untouched (Unwanted)
    fill = replace(fill, commission=costs.commission_per_trade)
    fills.append(fill)
    if order.side is Side.BUY:
        _apply_buy(fill, ledger)
    else:
        _apply_sell(fill, costs, ledger, trades, year_gains)


def _apply_buy(fill: Fill, ledger: _Ledger) -> None:
    """A BUY: volume-weight ``avg_price``, grow holdings, debit cash and seed.

    A fill always carries a positive ``qty`` (strategies floor to whole shares and
    never emit zero/dust orders), so ``new_holdings`` is always positive and the
    volume-weighted ``avg_price`` is always well-defined.
    """
    debit = fill.fill_price * fill.qty + fill.commission
    new_holdings = ledger.holdings + fill.qty
    weighted = ledger.avg_price * ledger.holdings + fill.fill_price * fill.qty
    ledger.avg_price = quantize_money(weighted / new_holdings)
    ledger.holdings = quantize_money(new_holdings)
    ledger.cash_usd = quantize_money(ledger.cash_usd - debit)
    ledger.seed_remaining = quantize_money(max(_ZERO, ledger.seed_remaining - debit))
    ledger.round_idx = ledger.round_idx + Decimal("1")


def _apply_sell(
    fill: Fill,
    costs: CostModel,
    ledger: _Ledger,
    trades: list[Trade],
    year_gains: dict[int, Decimal],
) -> None:
    """A SELL: realize a gain, reduce holdings, credit cash, accrue the year gain."""
    proceeds = fill.fill_price * fill.qty
    gain = costs.realized_gain_usd(
        sell_price=fill.fill_price,
        avg_cost=ledger.avg_price,
        qty=fill.qty,
        commission=fill.commission,
    )
    ledger.holdings = quantize_money(ledger.holdings - fill.qty)
    if ledger.holdings <= _ZERO:
        ledger.avg_price = _ZERO
    ledger.cash_usd = quantize_money(ledger.cash_usd + proceeds - fill.commission)
    ledger.realized_pnl_usd = quantize_money(ledger.realized_pnl_usd + gain)
    year = fill.date.year
    year_gains[year] = year_gains.get(year, _ZERO) + gain
    trades.append(
        Trade(
            side=Side.SELL,
            qty=fill.qty,
            realized_gain_usd=gain,
            tax_year=year,
        )
    )


def _apply_annual_tax(
    costs: CostModel,
    ledger: _Ledger,
    year_gains: dict[int, Decimal],
    year_fx: dict[int, Decimal],
    curve: list[EquityPoint],
) -> None:
    """Apply per-calendar-year tax to the ledger and the final curve point."""
    total_tax = _ZERO
    for year, gain in year_gains.items():
        fx = year_fx[year]
        total_tax += costs.annual_tax(gain, fx)
    total_tax = quantize_money(total_tax)
    ledger.total_tax_usd = total_tax
    if total_tax == _ZERO or not curve:
        return
    # Tax reduces the realized cash; reflect it on the terminal equity point only.
    ledger.cash_usd = quantize_money(ledger.cash_usd - total_tax)
    last = curve[-1]
    last_fx = year_fx[last.date.year]
    new_usd = quantize_money(last.equity_usd - total_tax)
    curve[-1] = EquityPoint(
        date=last.date,
        equity_usd=new_usd,
        equity_krw=quantize_money(new_usd * last_fx),
        holdings=last.holdings,
        cash=quantize_money(last.cash - total_tax),
    )


def _mark_to_market(bar: OHLCBar, ledger: _Ledger) -> EquityPoint:
    """Record a USD + KRW equity sample for ``bar``."""
    equity_usd = quantize_money(ledger.cash_usd + ledger.holdings * bar.close)
    return EquityPoint(
        date=bar.date,
        equity_usd=equity_usd,
        equity_krw=quantize_money(equity_usd * bar.fx_usdkrw),
        holdings=ledger.holdings,
        cash=ledger.cash_usd,
    )


def _is_trigger(
    cadence: str,
    cycle_length: Literal["monthly"] | int,
    series: Sequence[OHLCBar],
    bar_index: int,
) -> bool:
    """Decide whether the strategy triggers on ``series[bar_index]``."""
    if cadence == "daily":
        return True
    # cadence == "cycle": once per cycle slice.
    if isinstance(cycle_length, int):
        return bar_index % cycle_length == 0
    # "monthly": first bar overall, then on each (year, month) change.
    if bar_index == 0:
        return True
    prev = series[bar_index - 1].date
    cur = series[bar_index].date
    return (cur.year, cur.month) != (prev.year, prev.month)


def _strategy_ticker(strategy: Strategy, cfg: Config) -> str:
    """Resolve the ticker for the ``Market`` snapshot from the Config when present."""
    if cfg is None:
        return "TEST"
    block = cfg.strategies.mab if strategy.ns == "mab" else cfg.strategies.vr
    return block.ticker


def _normalize_bars(bars: Sequence[OHLCBar] | pd.DataFrame) -> list[OHLCBar]:
    """Normalize the abstract input to an ascending ``list[OHLCBar]``.

    A pandas ``DataFrame`` (the fixed schema) is converted to typed ``OHLCBar`` at
    this single boundary so pandas types never leak into the strict-checked loop.
    """
    if isinstance(bars, Sequence):
        ordered = sorted(bars, key=lambda b: b.date)
        return list(ordered)
    return _bars_from_dataframe(bars)


def _bars_from_dataframe(df: pd.DataFrame) -> list[OHLCBar]:
    """Convert a fixed-schema pandas ``DataFrame`` to ``list[OHLCBar]``.

    Done at this single boundary so pandas/numpy types never leak into the
    strict-checked inner loop: every value is read by string column name and
    funnelled through ``Decimal(str(...))`` before reaching a typed ``OHLCBar``.
    """
    out: list[OHLCBar] = []
    records: list[dict[Hashable, Any]] = df.to_dict(orient="records")
    for row in records:
        adj = row.get("adj_close")
        out.append(
            OHLCBar(
                date=row["date"],
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                adj_close=None if adj is None else Decimal(str(adj)),
                fx_usdkrw=Decimal(str(row["fx_usdkrw"])),
            )
        )
    out.sort(key=lambda b: b.date)
    return out


def _fingerprint(
    series: Sequence[OHLCBar],
    start_state: State,
    cycle_length: Literal["monthly"] | int,
    costs: CostModel,
) -> str:
    """A stable hash of the inputs for reproducibility checks."""
    parts = [
        str(cycle_length),
        f"{costs.commission_per_trade}|{costs.tax_rate}|"
        f"{costs.annual_deduction_krw}|{costs.slippage_bps}",
        f"{start_state.ns}:" + ",".join(f"{k}={v}" for k, v in sorted(start_state.data.items())),
    ]
    for b in series:
        parts.append(
            f"{b.date.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.adj_close}|{b.fx_usdkrw}"
        )
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest
