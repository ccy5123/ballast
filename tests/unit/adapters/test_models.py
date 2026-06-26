"""Tests for adapter DTO invariants (REQ-ADAPTER-001-R1).

@TEST:SPEC-ADAPTER-001

Covers Decimal-at-boundary float rejection (AC-12) and DTO immutability.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ballast.adapters.models import Currency, Quote


# AC-12 — a bare float on a money field is rejected (no silent coercion).
def test_quote_rejects_float_last_price() -> None:
    with pytest.raises(ValidationError):
        Quote(symbol="TQQQ", last_price=70.12, currency=Currency.USD)  # type: ignore[arg-type]


def test_quote_accepts_decimal_and_string_money() -> None:
    from_decimal = Quote(symbol="TQQQ", last_price=Decimal("70.12"), currency=Currency.USD)
    from_string = Quote(symbol="TQQQ", last_price="70.12", currency=Currency.USD)  # type: ignore[arg-type]
    assert from_decimal.last_price == Decimal("70.12")
    assert from_string.last_price == Decimal("70.12")


def test_quote_is_immutable() -> None:
    quote = Quote(symbol="TQQQ", last_price=Decimal("70.12"), currency=Currency.USD)
    with pytest.raises(ValidationError):
        quote.symbol = "OTHER"  # type: ignore[misc]
