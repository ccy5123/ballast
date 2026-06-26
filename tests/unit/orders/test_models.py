"""Tests for Order Manager DTO invariants (REQ-ORDER-001-R1).

@TEST:SPEC-ORDER-001

Covers the broker-neutral enums, Decimal-at-boundary float rejection (AC-17),
2-dp normalization, immutability, and the OrderPlan / SubmissionResult shapes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ballast.core.models import Side
from ballast.orders.models import (
    OrderIntent,
    OrderKind,
    OrderPlan,
    SubmissionResult,
    SubmissionStatus,
    Tif,
)


def test_enum_values_are_broker_neutral() -> None:
    assert Tif.DAY == "DAY"
    assert Tif.CLS == "CLS"
    assert OrderKind.LIMIT == "LIMIT"
    assert OrderKind.MARKET == "MARKET"
    assert SubmissionStatus.RECORDED == "RECORDED"
    assert SubmissionStatus.SUBMITTED == "SUBMITTED"
    assert SubmissionStatus.DUPLICATE == "DUPLICATE"
    assert SubmissionStatus.BLOCKED == "BLOCKED"
    assert SubmissionStatus.FAILED == "FAILED"


def _intent(**overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "client_order_id": "vr-20260626-abc",
        "account_seq": "acc-1",
        "side": Side.BUY,
        "ticker": "QLD",
        "qty": Decimal("3.00"),
        "kind": OrderKind.LIMIT,
        "tif": Tif.DAY,
        "limit_price": Decimal("80.00"),
    }
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


# AC-17 — a bare float on a money field is rejected (no silent coercion).
def test_order_intent_rejects_float_qty() -> None:
    with pytest.raises(ValidationError):
        _intent(qty=70.12)


def test_order_intent_rejects_float_limit_price() -> None:
    with pytest.raises(ValidationError):
        _intent(limit_price=80.5)


def test_order_intent_accepts_decimal_and_normalizes_to_2dp() -> None:
    intent = _intent(qty=Decimal("3"), limit_price=Decimal("80"))
    assert intent.qty == Decimal("3.00")
    assert intent.limit_price == Decimal("80.00")
    # quantization rounds to 2 dp (idempotent, ROUND_HALF_UP).
    assert _intent(qty=Decimal("3.005")).qty == Decimal("3.01")


def test_order_intent_allows_none_limit_price() -> None:
    intent = _intent(kind=OrderKind.LIMIT, tif=Tif.CLS, limit_price=None)
    assert intent.limit_price is None


def test_order_intent_is_immutable() -> None:
    intent = _intent()
    with pytest.raises(ValidationError):
        intent.ticker = "OTHER"  # type: ignore[misc]


def test_submission_result_shape_and_defaults() -> None:
    result = SubmissionResult(
        client_order_id="vr-20260626-abc",
        status=SubmissionStatus.SUBMITTED,
        broker_order_id="rec-0",
    )
    assert result.status == SubmissionStatus.SUBMITTED
    assert result.broker_order_id == "rec-0"
    assert result.reason is None
    # optional fields default to None.
    blocked = SubmissionResult(
        client_order_id="x", status=SubmissionStatus.BLOCKED, reason="kill-switch"
    )
    assert blocked.broker_order_id is None
    assert blocked.reason == "kill-switch"


def test_submission_result_is_immutable() -> None:
    result = SubmissionResult(client_order_id="x", status=SubmissionStatus.RECORDED)
    with pytest.raises(ValidationError):
        result.status = SubmissionStatus.SUBMITTED  # type: ignore[misc]


def test_order_plan_holds_intents_and_results() -> None:
    intent = _intent()
    result = SubmissionResult(
        client_order_id=intent.client_order_id, status=SubmissionStatus.RECORDED
    )
    plan = OrderPlan(intents=(intent,), results=(result,))
    assert plan.intents == (intent,)
    assert plan.results == (result,)


def test_order_plan_is_immutable() -> None:
    plan = OrderPlan(intents=(), results=())
    with pytest.raises(ValidationError):
        plan.intents = (_intent(),)  # type: ignore[misc]
