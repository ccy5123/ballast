"""End-to-end acceptance check for the example Config (Definition of Done).

The reference Config (TQQQ / SOXL / QLD) must load, validate, and resolve
target_pct/band end-to-end, and the guardrails must agree with the config's
``strict_instrument`` flag.

@TEST:SPEC-CORE-001
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ballast.core import (
    Config,
    InstrumentRegistry,
    validate_instrument,
)


def test_example_config_resolves_end_to_end(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    registry = InstrumentRegistry.from_config(cfg)

    # VR on TQQQ: no explicit overrides -> instrument defaults apply.
    vr = cfg.strategies.vr
    assert registry.resolve_target_pct(vr.ticker, vr.target_pct) == Decimal("0.50")
    assert registry.resolve_band(vr.ticker, vr.band) == Decimal("0.15")

    # MAB on SOXL: explicit target_pct overrides; band falls back to default.
    mab = cfg.strategies.mab
    assert registry.resolve_target_pct(mab.ticker, mab.target_pct) == Decimal("0.45")
    assert registry.resolve_band(mab.ticker, mab.band) == Decimal("0.20")


def test_example_instruments_pass_strict_guardrails(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    registry = InstrumentRegistry.from_config(cfg)
    strict = cfg.common.strict_instrument
    # Every instrument in the reference config is a leveraged index ETF, so it
    # must pass the guardrails even under strict=True (no raise, no warning).
    for ticker in cfg.instruments:
        meta = registry.resolve(ticker)
        validate_instrument(meta, strict=strict)
