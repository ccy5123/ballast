"""Characterization tests for SPEC-STRATEGY-001 (DDD PRESERVE phase).

These tests pin the CURRENT observable behavior of the parts that MUST stay
INVARIANT across the SPEC-STRATEGY-001 fix, so the DDD ANALYZE-PRESERVE-IMPROVE
cycle has a green safety net that survives the change unchanged:

* the full MAB ``run_backtest`` result (equity curve / holdings / cash / fills /
  trades / realized P&L / tax / fingerprint), which is 100% fill-derived and must
  be byte-for-byte identical after the contract change (FD5, AC-13);
* the MAB seed-exhausted quarter-sell, which (because its priced LOC limit equals
  the bar close) must fill at the close and apply to the ledger IDENTICALLY to the
  pre-fix price-less LOC (FD1, AC-4);
* a VR single-cycle order produced from a given seed ``V_n`` (FD6, AC-9).

The golden values below were captured against the PRE-FIX engine; they are the
contract these behaviors must continue to honor after the IMPROVE phase. The
intentionally-CHANGED behaviors (quarter-sell now priced, multi-cycle ``V_n`` now
advances) are asserted in the cluster suites, not here.

@TEST:SPEC-STRATEGY-001
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ballast.backtest.engine import run_backtest
from ballast.backtest.types import BacktestResult, OHLCBar
from ballast.core.config import Config
from ballast.core.mab import MABStrategy
from ballast.core.models import Market, OrderType, Side, State
from ballast.core.vr import VRStrategy

from .conftest import bar

# A SOXL-only example Config (mirrors the core spec instrument defaults).
_CONFIG_YAML = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true
execution:
  broker: toss
  dry_run: true
  max_position_pct: "0.95"
instruments:
  SOXL:
    leverage: 3
    underlying: SOX
    default_target_pct: "0.40"
    default_band: "0.20"
strategies:
  vr:
    account_seq: "0001"
    ticker: SOXL
  mab:
    account_seq: "0002"
    ticker: SOXL
    target_pct: "0.45"
    seed: "8000.00"
    n_splits: 40
"""

# A config whose tiny ``n_splits`` drives ``round_idx`` past the seed so the
# backtest reaches the quarter-sell path (the priced-LOC equivalence target).
_QUARTER_SELL_YAML = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true
execution:
  broker: toss
  dry_run: true
  max_position_pct: "0.95"
instruments:
  SOXL:
    leverage: 3
    underlying: SOX
    default_target_pct: "0.40"
    default_band: "0.20"
strategies:
  vr:
    account_seq: "0001"
    ticker: SOXL
  mab:
    account_seq: "0002"
    ticker: SOXL
    target_pct: "9.99"
    seed: "8000.00"
    n_splits: 2
"""


def _config(tmp_path: object, yaml_text: str = _CONFIG_YAML) -> Config:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return Config.load(path)


def _ascending_series() -> list[OHLCBar]:
    """A synthetic ascending close series across three calendar months."""
    days = [
        date(2024, 1, 2),
        date(2024, 1, 15),
        date(2024, 2, 1),
        date(2024, 2, 20),
        date(2024, 3, 4),
        date(2024, 3, 25),
    ]
    closes = ["50.00", "55.00", "60.00", "66.00", "72.00", "80.00"]
    return [bar(d, c, c, c, c) for d, c in zip(days, closes, strict=True)]


def _mab_start_state() -> State:
    return State(
        ns="mab",
        data={
            "avg_price": Decimal("50.00"),
            "holdings": Decimal("40.00"),
            "seed_remaining": Decimal("8000.00"),
            "round_idx": Decimal("0"),
        },
    )


def _run_mab(tmp_path: object) -> BacktestResult:
    return run_backtest(
        MABStrategy(),
        _ascending_series(),
        _config(tmp_path),
        start_state=_mab_start_state(),
    )


# --------------------------------------------------------------------------- #
# INVARIANT — MAB full backtest result is fill-derived and must NOT change
# (FD5, AC-13). Golden snapshot captured against the pre-fix engine; the contract
# change carries an EMPTY MAB delta, so every value below stays identical.
# --------------------------------------------------------------------------- #
def test_characterize_mab_backtest_result_is_stable(tmp_path: object) -> None:
    result = _run_mab(tmp_path)

    curve_usd = [p.equity_usd for p in result.equity_curve]
    assert curve_usd == [
        Decimal("10000.00"),
        Decimal("10215.00"),
        Decimal("10435.00"),
        Decimal("10699.00"),
        Decimal("10963.00"),
        Decimal("11315.00"),
    ]
    holdings = [p.holdings for p in result.equity_curve]
    assert holdings == [
        Decimal("43.00"),
        Decimal("44.00"),
        Decimal("44.00"),
        Decimal("44.00"),
        Decimal("44.00"),
        Decimal("0.00"),
    ]
    cash = [p.cash for p in result.equity_curve]
    assert cash == [
        Decimal("7850.00"),
        Decimal("7795.00"),
        Decimal("7795.00"),
        Decimal("7795.00"),
        Decimal("7795.00"),
        Decimal("11315.00"),
    ]
    assert len(result.fills) == 4
    assert len(result.trades) == 1
    assert result.trades[0].side is Side.SELL
    assert result.trades[0].qty == Decimal("44.00")
    assert result.trades[0].realized_gain_usd == Decimal("1315.16")
    assert result.realized_pnl_usd == Decimal("1315.16")
    assert result.total_tax_usd == Decimal("0.00")
    assert result.start_capital_usd == Decimal("10000.00")


def test_characterize_mab_backtest_is_deterministic(tmp_path: object) -> None:
    first = _run_mab(tmp_path)
    second = _run_mab(tmp_path)
    assert first.equity_curve == second.equity_curve
    assert first.fills == second.fills
    assert first.trades == second.trades
    assert first.realized_pnl_usd == second.realized_pnl_usd
    assert first.total_tax_usd == second.total_tax_usd
    assert first.fingerprint == second.fingerprint


# --------------------------------------------------------------------------- #
# INVARIANT — the MAB quarter-sell, run through the backtest, fills at the close
# and applies to the ledger identically pre- and post-fix (FD1, AC-4). Pre-fix
# the quarter-sell LOC carries ``limit_price=None``; post-fix it carries
# ``quantize_money(close)``. Because limit == close, the fills are identical.
# --------------------------------------------------------------------------- #
def test_characterize_mab_quarter_sell_fills_at_close(tmp_path: object) -> None:
    series = [bar(date(2024, 1, i + 2), "50.00", "50.00", "50.00", "50.00") for i in range(6)]
    result = run_backtest(
        MABStrategy(),
        series,
        _config(tmp_path, _QUARTER_SELL_YAML),
        start_state=_mab_start_state(),
    )

    # The seed exhausts after the first cycle's buys; subsequent triggers are the
    # quarter-sell path. Each quarter-sell SELL must fill at the bar close (50.00).
    quarter_sells = [
        f for f in result.fills if f.order.side is Side.SELL and f.order.order_type is OrderType.LOC
    ]
    assert len(quarter_sells) == 4
    assert all(f.fill_price == Decimal("50.00") for f in quarter_sells)
    # floor(holdings / 4) each cycle, applied to the engine ledger.
    assert [f.qty for f in quarter_sells] == [
        Decimal("39.00"),
        Decimal("29.00"),
        Decimal("22.00"),
        Decimal("16.00"),
    ]
    # Terminal ledger state captured pre-fix; must be byte-for-byte identical.
    assert [p.holdings for p in result.equity_curve] == [
        Decimal("116.00"),
        Decimal("156.00"),
        Decimal("117.00"),
        Decimal("88.00"),
        Decimal("66.00"),
        Decimal("50.00"),
    ]
    assert [p.cash for p in result.equity_curve] == [
        Decimal("4200.00"),
        Decimal("2200.00"),
        Decimal("4150.00"),
        Decimal("5600.00"),
        Decimal("6700.00"),
        Decimal("7500.00"),
    ]


# --------------------------------------------------------------------------- #
# INVARIANT — VR single-cycle order from a given seed V_n is unchanged
# (FD6, AC-9). The R3 channel surfaces V_n alongside the SAME order tuple.
# --------------------------------------------------------------------------- #
def _vr_config() -> Config:
    return Config.model_validate(
        {
            "common": {"allow_fractional": False, "round_digits": 2, "strict_instrument": True},
            "execution": {"broker": "toss", "dry_run": True, "max_position_pct": "0.95"},
            "instruments": {
                "TQQQ": {
                    "leverage": 3,
                    "underlying": "NDX",
                    "default_target_pct": "0.50",
                    "default_band": "0.15",
                }
            },
            "strategies": {
                "vr": {"account_seq": "0001", "ticker": "TQQQ"},
                "mab": {"account_seq": "0002", "ticker": "TQQQ"},
            },
        }
    )


def test_characterize_vr_single_cycle_order_from_seed() -> None:
    cfg = _vr_config()
    market = Market(
        ticker="TQQQ",
        current_price=Decimal("100.00"),
        fx_rate=Decimal("1300.00"),
        is_open=True,
        is_holiday=False,
    )
    state = State(
        ns="vr",
        data={
            "V_n": Decimal("1000.00"),
            "pool": Decimal("0.00"),
            "qty": Decimal("12.00"),
        },
    )
    # E = 12 * 100 = 1200; the advanced line is above the band -> a single SELL.
    plan = VRStrategy().plan_orders(market, state, cfg)
    orders = plan.orders
    assert len(orders) == 1
    order = orders[0]
    assert order.side is Side.SELL
    assert order.order_type is OrderType.RESERVED_LIMIT
    assert order.ticker == "TQQQ"
    assert order.account_seq == "0001"
    assert order.limit_price == Decimal("100.00")
    assert order.qty == Decimal("1.00")  # floor((1200 - 1031.62) / 100) = floor(1.68)
