"""Shared fixtures for the core domain test suite.

The canonical example Config (TQQQ / SOXL / QLD) from ``spec.md`` is reused
across the registry, resolution-chain, and config-loading tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# @TEST:SPEC-CORE-001 — the reference Config from spec.md (TQQQ / SOXL / QLD).
EXAMPLE_CONFIG_YAML = """\
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
    default_target_pct: "0.50"
    default_band: "0.15"
  SOXL:
    leverage: 3
    underlying: SOX
    default_target_pct: "0.40"
    default_band: "0.20"
  QLD:
    leverage: 2
    underlying: NDX
    default_target_pct: "0.60"
    default_band: "0.10"

strategies:
  vr:
    account_seq: "0001"
    ticker: TQQQ
  mab:
    account_seq: "0002"
    ticker: SOXL
    target_pct: "0.45"
"""


@pytest.fixture
def example_config_yaml() -> str:
    """Return the canonical example config document as a YAML string."""
    return EXAMPLE_CONFIG_YAML


@pytest.fixture
def example_config_path(tmp_path: Path) -> Path:
    """Write the example config to a temp file and return its path."""
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE_CONFIG_YAML, encoding="utf-8")
    return path
