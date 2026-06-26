"""Tests for the instrument registry and guardrails (REQ-CORE-001-R3 / R4).

@TEST:SPEC-CORE-001
"""

from __future__ import annotations

import warnings
from decimal import Decimal
from pathlib import Path

import pytest

from ballast.core import (
    Config,
    InstrumentMeta,
    InstrumentRegistry,
    UnknownTickerError,
    UnresolvedParameterError,
    validate_instrument,
)


@pytest.fixture
def registry(example_config_path: Path) -> InstrumentRegistry:
    cfg = Config.load(example_config_path)
    return InstrumentRegistry.from_config(cfg)


# --------------------------------------------------------------------------- #
# resolve(ticker) -> InstrumentMeta
# --------------------------------------------------------------------------- #
def test_resolve_returns_instrument_meta(registry: InstrumentRegistry) -> None:
    meta = registry.resolve("TQQQ")
    assert isinstance(meta, InstrumentMeta)
    assert meta.ticker == "TQQQ"
    assert meta.leverage == 3
    assert meta.underlying == "NDX"
    assert meta.default_target_pct == Decimal("0.50")
    assert meta.default_band == Decimal("0.15")


# --------------------------------------------------------------------------- #
# Scenario 1 — explicit over default (R3)
# --------------------------------------------------------------------------- #
def test_resolve_target_pct_explicit_wins(registry: InstrumentRegistry) -> None:
    # strategies.mab explicitly sets target_pct 0.45 for SOXL
    value = registry.resolve_target_pct(ticker="SOXL", explicit=Decimal("0.45"))
    assert value == Decimal("0.45")
    assert type(value) is Decimal


def test_resolve_band_falls_back_to_default(registry: InstrumentRegistry) -> None:
    # mab provides no explicit band -> instrument default 0.20 applies
    value = registry.resolve_band(ticker="SOXL", explicit=None)
    assert value == Decimal("0.20")
    assert type(value) is Decimal


def test_resolve_target_pct_falls_back_to_default(registry: InstrumentRegistry) -> None:
    value = registry.resolve_target_pct(ticker="TQQQ", explicit=None)
    assert value == Decimal("0.50")


# --------------------------------------------------------------------------- #
# Scenario 2 — missing target/band after the chain raises (R3 / R4)
# --------------------------------------------------------------------------- #
def _registry_without_defaults(tmp_path: Path) -> InstrumentRegistry:
    yaml_text = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true
execution:
  broker: toss
  dry_run: true
  max_position_pct: "0.95"
instruments:
  TQQQ:
    leverage: 3
    underlying: NDX
strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
  mab:
    account_seq: "0002"
    ticker: TQQQ
"""
    path = tmp_path / "no_defaults.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return InstrumentRegistry.from_config(Config.load(path))


def test_resolve_target_pct_unresolved_raises(tmp_path: Path) -> None:
    registry = _registry_without_defaults(tmp_path)
    with pytest.raises(UnresolvedParameterError) as exc_info:
        registry.resolve_target_pct(ticker="TQQQ", explicit=None)
    message = str(exc_info.value)
    assert "TQQQ" in message
    assert "target_pct" in message


def test_resolve_band_unresolved_raises(tmp_path: Path) -> None:
    registry = _registry_without_defaults(tmp_path)
    with pytest.raises(UnresolvedParameterError) as exc_info:
        registry.resolve_band(ticker="TQQQ", explicit=None)
    assert "TQQQ" in str(exc_info.value)
    assert "band" in str(exc_info.value)


def test_unresolved_raises_regardless_of_strict(tmp_path: Path) -> None:
    # Unresolved is a hard data error, independent of strict_instrument.
    registry = _registry_without_defaults(tmp_path)
    with pytest.raises(UnresolvedParameterError):
        registry.resolve_target_pct(ticker="TQQQ", explicit=None)


def test_resolve_never_returns_none(registry: InstrumentRegistry) -> None:
    # Either a Decimal or an exception, never None.
    assert registry.resolve_target_pct(ticker="TQQQ", explicit=None) is not None
    assert registry.resolve_band(ticker="TQQQ", explicit=None) is not None


# --------------------------------------------------------------------------- #
# Scenario 5 — unknown ticker is rejected (R3 / R5)
# --------------------------------------------------------------------------- #
def test_resolve_unknown_ticker_raises(registry: InstrumentRegistry) -> None:
    with pytest.raises(UnknownTickerError) as exc_info:
        registry.resolve("SPY")
    assert "SPY" in str(exc_info.value)


def test_unknown_ticker_distinct_from_unresolved(registry: InstrumentRegistry) -> None:
    # The two error classes must be distinguishable.
    assert not issubclass(UnknownTickerError, UnresolvedParameterError)
    assert not issubclass(UnresolvedParameterError, UnknownTickerError)


def test_resolve_target_pct_unknown_ticker_raises(registry: InstrumentRegistry) -> None:
    with pytest.raises(UnknownTickerError) as exc_info:
        registry.resolve_target_pct(ticker="SPY", explicit=None)
    assert "SPY" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Scenario 3 — leverage < 2 blocks when strict, warns when not (R4)
# --------------------------------------------------------------------------- #
_LEVERAGE_MESSAGE = "Laoer strategies assume leveraged ETFs; 1x / single-stock breaks behavior"


def test_validate_low_leverage_blocks_when_strict() -> None:
    meta = InstrumentMeta(
        ticker="AAPL",
        leverage=1,
        underlying="AAPL",
        default_target_pct=Decimal("0.50"),
        default_band=Decimal("0.10"),
    )
    with pytest.raises(ValueError, match="leveraged ETFs"):
        validate_instrument(meta, strict=True)


def test_validate_low_leverage_warns_when_not_strict() -> None:
    meta = InstrumentMeta(
        ticker="SPY",
        leverage=1,
        underlying="SPX",  # index underlying so only leverage triggers
        default_target_pct=Decimal("0.50"),
        default_band=Decimal("0.10"),
    )
    with pytest.warns(UserWarning, match="leveraged ETFs"):
        # Returns None (does not raise); the warning is the only effect.
        validate_instrument(meta, strict=False)


def test_validate_low_leverage_message_matches_spec() -> None:
    meta = InstrumentMeta(
        ticker="SPY",
        leverage=1,
        underlying="SPX",
        default_target_pct=Decimal("0.50"),
        default_band=Decimal("0.10"),
    )
    with pytest.warns(UserWarning) as record:
        validate_instrument(meta, strict=False)
    assert any(_LEVERAGE_MESSAGE in str(w.message) for w in record)


# --------------------------------------------------------------------------- #
# Scenario 6 — non-index underlying blocks under strict (R4)
# --------------------------------------------------------------------------- #
def test_validate_non_index_underlying_blocks_when_strict() -> None:
    meta = InstrumentMeta(
        ticker="TSLL",
        leverage=2,
        underlying="TSLA",  # single stock, not an index
        default_target_pct=Decimal("0.50"),
        default_band=Decimal("0.10"),
    )
    with pytest.raises(ValueError, match="single/theme"):
        validate_instrument(meta, strict=True)


def test_validate_non_index_underlying_warns_when_not_strict() -> None:
    meta = InstrumentMeta(
        ticker="TSLL",
        leverage=2,
        underlying="TSLA",
        default_target_pct=Decimal("0.50"),
        default_band=Decimal("0.10"),
    )
    with pytest.warns(UserWarning, match="single/theme"):
        # Returns None (does not raise); the warning is the only effect.
        validate_instrument(meta, strict=False)


def test_validate_valid_instrument_passes_silently() -> None:
    meta = InstrumentMeta(
        ticker="TQQQ",
        leverage=3,
        underlying="NDX",
        default_target_pct=Decimal("0.50"),
        default_band=Decimal("0.15"),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # No warning is raised (would become an error) and no exception: passes.
        validate_instrument(meta, strict=True)


def test_validate_unresolved_defaults_raise_regardless_of_strict() -> None:
    # If the meta itself carries no defaults, validate must hard-error even
    # when strict=False (this is a data error, not a guardrail).
    meta = InstrumentMeta(
        ticker="TQQQ",
        leverage=3,
        underlying="NDX",
        default_target_pct=None,
        default_band=None,
    )
    with pytest.raises(UnresolvedParameterError):
        validate_instrument(meta, strict=False)
    with pytest.raises(UnresolvedParameterError):
        validate_instrument(meta, strict=True)
