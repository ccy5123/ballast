"""Tests for the pydantic Config schema (REQ-CORE-001-R5).

@TEST:SPEC-CORE-001
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ballast.core import Config


def test_config_loads_valid_example(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    assert set(cfg.instruments) == {"TQQQ", "SOXL", "QLD"}
    assert cfg.common.round_digits == 2
    assert cfg.common.strict_instrument is True
    assert cfg.common.allow_fractional is False
    assert cfg.execution.broker == "toss"
    assert cfg.execution.dry_run is True


def test_config_money_fields_are_decimal(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    assert type(cfg.execution.max_position_pct) is Decimal
    assert cfg.execution.max_position_pct == Decimal("0.95")
    tqqq = cfg.instruments["TQQQ"]
    assert type(tqqq.default_target_pct) is Decimal
    assert tqqq.default_target_pct == Decimal("0.50")
    assert tqqq.default_band == Decimal("0.15")


def test_config_strategies_parsed(example_config_path: Path) -> None:
    cfg = Config.load(example_config_path)
    assert cfg.strategies.vr.account_seq == "0001"
    assert cfg.strategies.vr.ticker == "TQQQ"
    assert cfg.strategies.vr.target_pct is None  # omitted -> default applies
    assert cfg.strategies.mab.account_seq == "0002"
    assert cfg.strategies.mab.ticker == "SOXL"
    assert cfg.strategies.mab.target_pct == Decimal("0.45")  # explicit override


# --------------------------------------------------------------------------- #
# Scenario 7 — invalid Config surfaces a clear validation error (R5)
# --------------------------------------------------------------------------- #
def test_config_rejects_float_money_field(tmp_path: Path) -> None:
    bad_yaml = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true
execution:
  broker: toss
  dry_run: true
  max_position_pct: 0.95
instruments:
  TQQQ:
    leverage: 3
    underlying: NDX
    default_target_pct: "0.50"
    default_band: "0.15"
strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
  mab:
    account_seq: "0002"
    ticker: TQQQ
"""
    path = tmp_path / "bad.yaml"
    path.write_text(bad_yaml, encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        Config.load(path)
    # The offending field path must be named.
    assert "max_position_pct" in str(exc_info.value)


def test_config_rejects_unknown_extra_key(tmp_path: Path) -> None:
    bad_yaml = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true
execution:
  broker: toss
  dry_run: true
  max_position_pct: "0.95"
  surprise_key: 1
instruments:
  TQQQ:
    leverage: 3
    underlying: NDX
    default_target_pct: "0.50"
    default_band: "0.15"
strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
  mab:
    account_seq: "0002"
    ticker: TQQQ
"""
    path = tmp_path / "bad.yaml"
    path.write_text(bad_yaml, encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        Config.load(path)
    assert "surprise_key" in str(exc_info.value)


def test_config_rejects_missing_required_block(tmp_path: Path) -> None:
    bad_yaml = """\
common:
  allow_fractional: false
  round_digits: 2
  strict_instrument: true
instruments:
  TQQQ:
    leverage: 3
    underlying: NDX
    default_target_pct: "0.50"
    default_band: "0.15"
strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
  mab:
    account_seq: "0002"
    ticker: TQQQ
"""
    path = tmp_path / "bad.yaml"
    path.write_text(bad_yaml, encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        Config.load(path)
    assert "execution" in str(exc_info.value)


def test_config_load_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "does-not-exist.yaml")


def test_config_load_accepts_string_path(example_config_path: Path) -> None:
    cfg = Config.load(str(example_config_path))
    assert "TQQQ" in cfg.instruments
