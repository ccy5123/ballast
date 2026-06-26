"""Public surface of the ballast Order Manager (P2, broker-neutral, dry-run-first).

@CODE:SPEC-ORDER-001

The broker-neutral order DTOs/enums, the order-write ``BrokerOrderPort`` Protocol,
the CORE ``Order`` -> ``OrderIntent`` mapping with a deterministic
``client_order_id``, the ordered safety guards, the dry-run-first ``OrderManager``,
and an in-memory ``RecordingBrokerOrderPort``. This layer does no network IO and
reads no wall clock; time and prices/positions are injected. No real broker write
is implemented here (deferred to P3 / SPEC-ADAPTER-002).
"""

from __future__ import annotations

from ballast.orders.guards import (
    GuardAction,
    GuardOutcome,
    dry_run_route,
    kill_switch_guard,
    position_cap_guard,
)
from ballast.orders.manager import OrderManager
from ballast.orders.mapping import derive_client_order_id, order_to_intent
from ballast.orders.models import (
    OrderIntent,
    OrderKind,
    OrderPlan,
    SubmissionResult,
    SubmissionStatus,
    Tif,
)
from ballast.orders.ports import BrokerOrderPort
from ballast.orders.recording import RecordingBrokerOrderPort

__all__ = [
    "BrokerOrderPort",
    "GuardAction",
    "GuardOutcome",
    "OrderIntent",
    "OrderKind",
    "OrderManager",
    "OrderPlan",
    "RecordingBrokerOrderPort",
    "SubmissionResult",
    "SubmissionStatus",
    "Tif",
    "derive_client_order_id",
    "dry_run_route",
    "kill_switch_guard",
    "order_to_intent",
    "position_cap_guard",
]
