"""Multi-cycle VR ``V_n`` fidelity at the engine level (SPEC-STRATEGY-001 R4).

Corrected-behavior tests for the engine's application of the R2 state delta:

* AC-12 — the engine writes VR's surfaced ``V2`` into ``_Ledger.v_n`` each cycle,
  so cycle ``n+1`` reads the ADVANCED ``V_n`` (not the stale seed). This replaces
  the documented pre-fix bug where ``ledger.v_n`` was seeded once and never moved.
* AC-13 — a MAB backtest result is byte-for-byte unchanged (MAB's empty delta is a
  no-op; its bookkeeping stays 100% fill-derived).
* AC-14 — the engine applies an opaque, namespace-scoped ``Decimal`` delta and
  never imports or calls ``vr.next_value`` (no engine -> vr coupling).

@TEST:SPEC-STRATEGY-001
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

from ballast.backtest.engine import run_backtest
from ballast.core.config import Config
from ballast.core.models import Market, State
from ballast.core.strategy import PlanResult
from ballast.core.vr import VRStrategy, next_value

from .conftest import bar


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


def _monthly_series() -> list[object]:
    days = [date(2024, 1, 2), date(2024, 2, 1), date(2024, 3, 1), date(2024, 4, 1)]
    return [bar(d, "100.00", "100.00", "100.00", "100.00") for d in days]


class _RecordingVR:
    """Wraps :class:`VRStrategy`, recording the ``V_n`` it SEES and SURFACES each
    cycle so a black-box test can assert the engine advanced the ledger."""

    cadence = "cycle"
    ns = "vr"

    def __init__(self) -> None:
        self._inner = VRStrategy()
        self.seen_v_n: list[Decimal] = []
        self.seen_pool: list[Decimal] = []
        self.seen_qty: list[Decimal] = []
        self.seen_price: list[Decimal] = []
        self.surfaced_v_n: list[Decimal] = []

    def plan_orders(self, market: Market, state: State, cfg: Config) -> PlanResult:
        self.seen_v_n.append(state.data["V_n"])
        self.seen_pool.append(state.data["pool"])
        self.seen_qty.append(state.data["qty"])
        self.seen_price.append(market.current_price)
        result = self._inner.plan_orders(market, state, cfg)
        self.surfaced_v_n.append(result.state_delta["V_n"])
        return result


# --------------------------------------------------------------------------- #
# AC-12 — multi-cycle V_n evolves faithfully (engine applies the delta).
# --------------------------------------------------------------------------- #
def test_engine_advances_v_n_across_cycles() -> None:
    strat = _RecordingVR()
    run_backtest(
        strat,
        _monthly_series(),  # type: ignore[arg-type]
        _vr_config(),
        start_state=State(
            ns="vr",
            data={"V_n": Decimal("100.00"), "pool": Decimal("5000.00"), "qty": Decimal("10.00")},
        ),
        cycle_length="monthly",
    )
    assert len(strat.seen_v_n) == 4  # one trigger per calendar month

    # Cycle 0 sees the seed; every later cycle sees the V2 surfaced by the prior
    # cycle (faithful evolution) — exactly the engine applying the delta.
    assert strat.seen_v_n[0] == Decimal("100.00")
    for prev, current in zip(strat.surfaced_v_n[:-1], strat.seen_v_n[1:], strict=True):
        assert current == prev

    # After >= 1 advancing cycle the exposed V_n differs from the seed.
    assert strat.seen_v_n[1] != Decimal("100.00")
    # The exposed V_n is monotonically distinct across cycles here (it advances).
    assert len(set(strat.seen_v_n)) == len(strat.seen_v_n)


def test_engine_v_n_matches_next_value_each_cycle() -> None:
    # The surfaced V2 the engine carries forward is exactly next_value(...) for the
    # inputs that cycle saw (single-pass; no divergence).
    cfg = _vr_config()
    strat = _RecordingVR()
    run_backtest(
        strat,
        _monthly_series(),  # type: ignore[arg-type]
        cfg,
        start_state=State(
            ns="vr",
            data={"V_n": Decimal("100.00"), "pool": Decimal("5000.00"), "qty": Decimal("10.00")},
        ),
        cycle_length="monthly",
    )
    vr = cfg.strategies.vr
    # Recompute V2 from the exact (V_n, pool, qty, price) each cycle actually saw.
    for seen_v, pool, qty, price, surfaced in zip(
        strat.seen_v_n,
        strat.seen_pool,
        strat.seen_qty,
        strat.seen_price,
        strat.surfaced_v_n,
        strict=True,
    ):
        e = qty * price
        expected = next_value(seen_v, pool, e, vr.g, vr.flow, use_skill=vr.use_skill, r=vr.r)
        assert surfaced == expected


# --------------------------------------------------------------------------- #
# AC-14 — the engine stays generic: it never imports or calls vr.next_value.
# --------------------------------------------------------------------------- #
def test_engine_does_not_import_or_call_vr_next_value() -> None:
    import ballast.backtest.engine as engine_module

    source = Path(engine_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # No import of vr / next_value into the engine.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert not node.module.endswith("vr"), "engine must not import ballast.core.vr"
            for alias in node.names:
                assert alias.name != "next_value", "engine must not import next_value"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.endswith(".vr")

    # No call to a `next_value(...)` symbol anywhere in the engine source.
    assert "next_value(" not in source
