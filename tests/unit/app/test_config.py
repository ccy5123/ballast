"""Tests for the typed operational ``RunnerConfig`` lens (REQ-RUNNER-001-R1).

@TEST:SPEC-RUNNER-001

Covers FD1 (operational fields, Decimal money, frozen + extra="forbid", no fork),
FD2 (lossless ``to_snapshot`` / ``from_snapshot`` round-trip with money as decimal
strings), and the Decimal policy (a bare ``float`` is rejected on the money path).
Maps to acceptance.md AC-1 / AC-2 / AC-3.

Note: FD1 normalizes BOTH ``account_capital`` and the ``allocation`` ratios to
2 dp via ``quantize_money`` (binding "Fixed Definition"); acceptance.md AC-2's
inline example shows ``"0.6"`` illustratively, but the binding 2-dp rule yields
``"0.60"``. The round-trip is lossless either way.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ballast.app import RunnerConfig

# --- AC-1 — operational fields with Decimal money ------------------------------------


def test_runner_config_carries_operational_fields() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000.00"),
        allocation={"vr": Decimal("0.6"), "mab": Decimal("0.4")},
        dry_run=True,
        kill_switch=False,
    )
    assert cfg.ticker == "QLD"
    assert cfg.account_capital == Decimal("10000.00")
    assert cfg.dry_run is True
    assert cfg.kill_switch is False


def test_account_capital_is_decimal_normalized_to_2dp() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000"),
        allocation={"vr": Decimal("1")},
        dry_run=True,
        kill_switch=False,
    )
    assert cfg.account_capital == Decimal("10000.00")
    assert isinstance(cfg.account_capital, Decimal)


def test_allocation_ratios_are_decimal_values() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000.00"),
        allocation={"vr": Decimal("0.6"), "mab": Decimal("0.4")},
        dry_run=True,
        kill_switch=False,
    )
    assert all(isinstance(ratio, Decimal) for ratio in cfg.allocation.values())
    assert cfg.allocation["vr"] == Decimal("0.60")
    assert cfg.allocation["mab"] == Decimal("0.40")


def test_runner_config_is_frozen() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000.00"),
        allocation={"vr": Decimal("1")},
        dry_run=True,
        kill_switch=False,
    )
    with pytest.raises(ValidationError):
        cfg.dry_run = False


def test_runner_config_rejects_unknown_keys() -> None:
    # extra="forbid": an unknown operational key fails loudly (no silent drift).
    with pytest.raises(ValidationError):
        RunnerConfig(
            ticker="QLD",
            account_capital=Decimal("10000.00"),
            allocation={"vr": Decimal("1")},
            dry_run=True,
            kill_switch=False,
            account_seq="0001",  # type: ignore[call-arg]  # belongs in core Config, not here
        )


# --- AC-2 — lossless round-trip through the snapshot ---------------------------------


def test_to_snapshot_emits_generic_shape_with_decimal_strings() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000.00"),
        allocation={"vr": Decimal("0.6"), "mab": Decimal("0.4")},
        dry_run=True,
        kill_switch=False,
    )
    snapshot = cfg.to_snapshot()
    assert snapshot == {
        "ticker": "QLD",
        "account_capital": "10000.00",
        "allocation": {"vr": "0.60", "mab": "0.40"},
        "dry_run": True,
        "kill_switch": False,
    }


def test_to_snapshot_money_is_never_a_binary_float() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000.00"),
        allocation={"vr": Decimal("0.6"), "mab": Decimal("0.4")},
        dry_run=False,
        kill_switch=True,
    )
    snapshot = cfg.to_snapshot()
    assert isinstance(snapshot["account_capital"], str)
    allocation = snapshot["allocation"]
    assert all(isinstance(ratio, str) for ratio in allocation.values())


def test_from_snapshot_is_exact_inverse_of_to_snapshot() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=Decimal("10000.00"),
        allocation={"vr": Decimal("0.6"), "mab": Decimal("0.4")},
        dry_run=True,
        kill_switch=False,
    )
    restored = RunnerConfig.from_snapshot(cfg.to_snapshot())
    assert restored == cfg
    assert restored.account_capital == Decimal("10000.00")
    assert restored.allocation["vr"] == Decimal("0.60")


def test_from_snapshot_recovers_decimal_money_exactly() -> None:
    # A snapshot persisted by a dashboard (decimal strings) reconstructs exactly.
    snapshot = {
        "ticker": "SOXL",
        "account_capital": "25000.50",
        "allocation": {"vr": "0.70", "mab": "0.30"},
        "dry_run": False,
        "kill_switch": True,
    }
    cfg = RunnerConfig.from_snapshot(snapshot)
    assert cfg.account_capital == Decimal("25000.50")
    assert cfg.allocation == {"vr": Decimal("0.70"), "mab": Decimal("0.30")}
    assert cfg.dry_run is False
    assert cfg.kill_switch is True
    # And it round-trips back to the same canonical snapshot.
    assert cfg.to_snapshot() == snapshot


# --- AC-3 — a bare float on the money path is rejected -------------------------------


def test_float_account_capital_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RunnerConfig(
            ticker="QLD",
            account_capital=10000.5,  # type: ignore[arg-type]  # bare float on money path
            allocation={"vr": Decimal("1")},
            dry_run=True,
            kill_switch=False,
        )


def test_float_allocation_ratio_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RunnerConfig(
            ticker="QLD",
            account_capital=Decimal("10000.00"),
            allocation={"vr": 0.6},  # type: ignore[dict-item]  # bare float ratio
            dry_run=True,
            kill_switch=False,
        )


def test_string_money_values_are_accepted_and_normalized() -> None:
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital="10000",  # type: ignore[arg-type]  # decimal string accepted
        allocation={"vr": "0.6", "mab": "0.4"},  # type: ignore[dict-item]
        dry_run=True,
        kill_switch=False,
    )
    assert cfg.account_capital == Decimal("10000.00")
    assert cfg.allocation == {"vr": Decimal("0.60"), "mab": Decimal("0.40")}


def test_non_decimal_non_string_allocation_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RunnerConfig(
            ticker="QLD",
            account_capital=Decimal("10000.00"),
            allocation={"vr": object()},  # type: ignore[dict-item]
            dry_run=True,
            kill_switch=False,
        )


def test_int_account_capital_passes_through_to_decimal() -> None:
    # An int (not float/str/Decimal) passes the money normalizer through to
    # pydantic, which coerces it to a Decimal (int is exact on the money path).
    cfg = RunnerConfig(
        ticker="QLD",
        account_capital=10000,  # type: ignore[arg-type]  # int passthrough -> Decimal
        allocation={"vr": Decimal("1")},
        dry_run=True,
        kill_switch=False,
    )
    assert cfg.account_capital == Decimal("10000")


def test_non_mapping_allocation_is_rejected() -> None:
    # A non-Mapping allocation passes the normalizer through; pydantic then
    # rejects it (it is not a valid Mapping[str, Decimal]).
    with pytest.raises(ValidationError):
        RunnerConfig(
            ticker="QLD",
            account_capital=Decimal("10000.00"),
            allocation=[("vr", Decimal("1"))],  # type: ignore[arg-type]  # not a Mapping
            dry_run=True,
            kill_switch=False,
        )
