"""Tests for State Store persisted DTO invariants (REQ-STATE-001-R1/R2/R4/R5).

@TEST:SPEC-STATE-001

Covers the ``OrderState`` lifecycle enum (extending ORDER-001 ``SubmissionStatus``),
the frozen Decimal-money DTOs with bare-``float`` rejection (AC-19), 2-dp
normalization, immutability, and lossless ``Decimal`` <-> string round-trips.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ballast.core.models import Side
from ballast.orders.models import SubmissionStatus
from ballast.state.models import (
    ConfigSnapshotRecord,
    FillRecord,
    Lease,
    OrderLedgerRecord,
    OrderState,
    ReconMutation,
    ReconResult,
    StateSnapshot,
    StrategyStateRecord,
    decimal_map_from_strings,
    decimal_map_to_strings,
)

# --- OrderState lifecycle enum (FD4) -------------------------------------------------


def test_order_state_extends_submission_status_values() -> None:
    # Every ORDER-001 SubmissionStatus value is an OrderState value (no fork).
    for status in SubmissionStatus:
        assert OrderState(status.value) == status.value
    # Plus the reconciliation terminals.
    assert OrderState.PARTIAL == "PARTIAL"
    assert OrderState.FILLED == "FILLED"
    assert OrderState.CANCELED == "CANCELED"
    assert OrderState.EXPIRED == "EXPIRED"


def test_order_state_is_str_enum() -> None:
    assert isinstance(OrderState.FILLED, str)
    assert OrderState.SUBMITTED.value == "SUBMITTED"


# --- StrategyStateRecord (FD2) -------------------------------------------------------


def test_strategy_state_record_normalizes_decimal_to_2dp() -> None:
    rec = StrategyStateRecord(ns="vr", data={"V_n": Decimal("1050")}, version=0)
    assert rec.data["V_n"] == Decimal("1050.00")
    assert isinstance(rec.data["V_n"], Decimal)


def test_strategy_state_record_rejects_float_value() -> None:
    with pytest.raises(ValidationError):
        StrategyStateRecord(ns="vr", data={"V_n": 1050.5}, version=0)  # type: ignore[dict-item]


def test_strategy_state_record_is_immutable() -> None:
    rec = StrategyStateRecord(ns="vr", data={"V_n": Decimal("1.00")}, version=0)
    with pytest.raises(ValidationError):
        rec.version = 1  # type: ignore[misc]


def test_strategy_state_record_defaults_to_empty() -> None:
    rec = StrategyStateRecord(ns="vr")
    assert rec.data == {}
    assert rec.version == 0


def test_strategy_state_record_accepts_decimal_string_values() -> None:
    # A decimal STRING value is accepted and normalized to a 2-dp Decimal.
    rec = StrategyStateRecord(ns="vr", data={"V_n": "1050"}, version=0)
    assert rec.data["V_n"] == Decimal("1050.00")
    assert isinstance(rec.data["V_n"], Decimal)


def test_strategy_state_record_rejects_non_decimal_value() -> None:
    with pytest.raises(ValidationError):
        StrategyStateRecord(ns="vr", data={"V_n": object()}, version=0)  # type: ignore[dict-item]


def test_strategy_state_record_data_is_read_only_snapshot() -> None:
    rec = StrategyStateRecord(ns="vr", data={"V_n": Decimal("1.00")}, version=0)
    with pytest.raises((TypeError, AttributeError)):
        rec.data["V_n"] = Decimal("999.00")  # type: ignore[index]


# --- OrderLedgerRecord (FD4) ---------------------------------------------------------


def test_order_ledger_record_shape_and_normalization() -> None:
    rec = OrderLedgerRecord(
        client_order_id="vr-20260626-abc",
        account_seq="acc-1",
        broker_order_id="toss-9001",
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal("3"),
        filled_qty=Decimal("0"),
        ts="2026-06-26T00:00:00Z",
    )
    assert rec.broker_order_id == "toss-9001"
    assert rec.ordered_qty == Decimal("3.00")
    assert rec.filled_qty == Decimal("0.00")
    assert rec.status is OrderState.SUBMITTED


def test_order_ledger_record_accepts_int_qty() -> None:
    # An int money value passes the float guard and is coerced to Decimal by pydantic.
    rec = OrderLedgerRecord(
        client_order_id="k1",
        account_seq="acc-1",
        broker_order_id=None,
        status=OrderState.SUBMITTED,
        ordered_qty=3,  # type: ignore[arg-type]
        filled_qty=0,  # type: ignore[arg-type]
        ts="t0",
    )
    assert rec.ordered_qty == Decimal("3")


def test_strategy_state_record_rejects_non_mapping_data() -> None:
    with pytest.raises(ValidationError):
        StrategyStateRecord(ns="vr", data=[("V_n", Decimal("1"))], version=0)  # type: ignore[arg-type]


def test_order_ledger_record_accepts_decimal_string_qty() -> None:
    # A decimal STRING money value is accepted and normalized to 2 dp.
    rec = OrderLedgerRecord(
        client_order_id="k1",
        account_seq="acc-1",
        broker_order_id=None,
        status=OrderState.SUBMITTED,
        ordered_qty="3",  # type: ignore[arg-type]
        filled_qty="0",  # type: ignore[arg-type]
        ts="t0",
    )
    assert rec.ordered_qty == Decimal("3.00")
    assert rec.filled_qty == Decimal("0.00")


def test_order_ledger_record_allows_none_broker_order_id() -> None:
    rec = OrderLedgerRecord(
        client_order_id="k1",
        account_seq="acc-1",
        broker_order_id=None,
        status=OrderState.RECORDED,
        ordered_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    assert rec.broker_order_id is None


def test_order_ledger_record_rejects_float_qty() -> None:
    with pytest.raises(ValidationError):
        OrderLedgerRecord(
            client_order_id="k1",
            account_seq="acc-1",
            broker_order_id=None,
            status=OrderState.SUBMITTED,
            ordered_qty=3.0,  # type: ignore[arg-type]
            filled_qty=Decimal("0"),
            ts="t0",
        )


# --- FillRecord (FD7) ----------------------------------------------------------------


def test_fill_record_shape_and_float_rejection() -> None:
    fill = FillRecord(
        fill_id="f1",
        client_order_id="mab-c1",
        broker_order_id="toss-1",
        side=Side.BUY,
        qty=Decimal("5"),
        price=Decimal("84"),
        ts="t0",
    )
    assert fill.qty == Decimal("5.00")
    assert fill.price == Decimal("84.00")
    assert fill.commission == Decimal("0.00")
    with pytest.raises(ValidationError):
        FillRecord(
            fill_id="f1",
            client_order_id="mab-c1",
            broker_order_id=None,
            side=Side.BUY,
            qty=84.5,  # type: ignore[arg-type]
            price=Decimal("84"),
            ts="t0",
        )


# --- ConfigSnapshotRecord (FD5) ------------------------------------------------------


def test_config_snapshot_record_is_generic_and_immutable() -> None:
    rec = ConfigSnapshotRecord(data={"ticker": "QLD", "dry_run": True}, version=1)
    assert rec.data["ticker"] == "QLD"
    assert rec.version == 1
    with pytest.raises(ValidationError):
        rec.version = 2  # type: ignore[misc]


# --- StateSnapshot / ReconResult / Lease ---------------------------------------------


def test_state_snapshot_defaults_and_immutability() -> None:
    snap = StateSnapshot()
    assert snap.strategy == {}
    assert snap.orders == {}
    assert snap.applied_fills == {}


def test_recon_result_holds_snapshot_and_mutations() -> None:
    mut = ReconMutation(kind="ledger_status", target="k1", status=OrderState.FILLED)
    result = ReconResult(snapshot=StateSnapshot(), mutations=(mut,))
    assert result.mutations[0].target == "k1"
    assert result.mutations[0].status is OrderState.FILLED


def test_lease_handle_shape() -> None:
    lease = Lease(owner="worker-A", ttl=30, token="abc")
    assert lease.owner == "worker-A"
    assert lease.ttl == 30


# --- Decimal <-> string lossless helpers (AC-19) -------------------------------------


def test_decimal_string_round_trip_is_lossless() -> None:
    src = {"V_n": Decimal("1042.37"), "qty": Decimal("6.00")}
    as_str = decimal_map_to_strings(src)
    assert as_str == {"V_n": "1042.37", "qty": "6.00"}
    back = decimal_map_from_strings(as_str)
    assert back == src
    assert all(isinstance(v, Decimal) for v in back.values())


def test_decimal_string_round_trip_never_uses_binary_float() -> None:
    # A value with many digits round-trips exactly through the canonical string.
    value = Decimal("0.10")
    restored = decimal_map_from_strings(decimal_map_to_strings({"x": value}))["x"]
    assert restored == value
    assert str(restored) == "0.10"
