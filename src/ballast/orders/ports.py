"""Broker-agnostic order-write port Protocol (REQ-ORDER-001-R1, FD5).

@CODE:SPEC-ORDER-001

``BrokerOrderPort`` prescribes no concrete transport, only the broker-agnostic
shape that an in-memory recording port and, later, P3's Toss write adapter
satisfy structurally. It is ``runtime_checkable`` so structural conformance can
be asserted in tests (mirrors the ADAPTER-001 ``ports.py`` style).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ballast.orders.models import OrderIntent, SubmissionResult


@runtime_checkable
class BrokerOrderPort(Protocol):
    """The order-write surface a broker adapter must satisfy (FD5)."""

    def place_order(self, intent: OrderIntent) -> SubmissionResult: ...

    def cancel_order(self, account_seq: str, client_order_id: str) -> SubmissionResult: ...
