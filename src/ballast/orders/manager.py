"""The dry-run-first Order Manager (REQ-ORDER-001-R2 / R3 / R4).

@CODE:SPEC-ORDER-001

Orchestrates: map (CORE ``Order`` -> ``OrderIntent``) -> ordered safety guards
(kill-switch -> max_position_pct -> dry-run gate) -> either the dry-run
record-only path (default, no port call, no network IO) or the live submission
path (idempotent dedup via an in-memory ledger keyed by ``client_order_id``).

The manager reads NO wall clock and performs NO network IO: the clock
(``cycle_key``) and any prices / positions / buying-power are injected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal

from ballast.core.models import Order
from ballast.orders.guards import (
    GuardAction,
    GuardOutcome,
    dry_run_route,
    kill_switch_guard,
    position_cap_guard,
)
from ballast.orders.mapping import order_to_intent
from ballast.orders.models import (
    OrderIntent,
    OrderPlan,
    SubmissionResult,
    SubmissionStatus,
)
from ballast.orders.ports import BrokerOrderPort

# Statuses that pin a key as terminal-done (never re-submitted within the cycle).
_DEDUP_TERMINAL = frozenset({SubmissionStatus.SUBMITTED, SubmissionStatus.DUPLICATE})


class OrderManager:
    """Account-addressable Order Manager with an in-memory submission ledger.

    Holds no global mutable state beyond an in-memory ledger keyed by
    ``client_order_id`` (A8). A ``FAILED`` key may be retried explicitly; a
    ``SUBMITTED``/``DUPLICATE`` key is never re-submitted.
    """

    def __init__(self) -> None:
        self._ledger: dict[str, SubmissionResult] = {}

    def status_of(self, client_order_id: str) -> SubmissionStatus | None:
        """Return the tracked status for a key, or ``None`` if unseen."""
        result = self._ledger.get(client_order_id)
        return result.status if result is not None else None

    def place(
        self,
        orders: Iterable[Order],
        *,
        ns: str,
        cycle_key: str,
        dry_run: bool,
        kill_switch: bool,
        max_position_pct: Decimal | None = None,
        base_values: Mapping[str, Decimal] | None = None,
        positions: Mapping[str, Decimal] | None = None,
        prices: Mapping[str, Decimal] | None = None,
        port: BrokerOrderPort | None = None,
    ) -> OrderPlan:
        """Place CORE orders through guards and the dry-run/live path.

        Returns an inspectable :class:`OrderPlan` pairing each mapped intent with
        its :class:`SubmissionResult`. Under ``dry_run`` (the default behavior of
        shipped configs) no port is touched and ``port`` may be ``None``.
        """
        intents: list[OrderIntent] = []
        results: list[SubmissionResult] = []

        for order in orders:
            intent = order_to_intent(order, ns=ns, cycle_key=cycle_key)

            # Guard 1: kill-switch — precedence over everything, incl. dry-run.
            kill = kill_switch_guard(intent, engaged=kill_switch)
            if kill.action is GuardAction.BLOCK:
                intents.append(intent)
                results.append(
                    SubmissionResult(
                        client_order_id=intent.client_order_id,
                        status=SubmissionStatus.BLOCKED,
                        reason=kill.reason,
                    )
                )
                continue

            # Guard 2: max_position_pct clamp / block (only when inputs supplied).
            clamp_reason: str | None = None
            cap = self._apply_position_cap(
                intent,
                max_position_pct=max_position_pct,
                base_values=base_values,
                positions=positions,
                prices=prices,
            )
            if cap.action is GuardAction.BLOCK:
                intents.append(intent)
                results.append(
                    SubmissionResult(
                        client_order_id=intent.client_order_id,
                        status=SubmissionStatus.BLOCKED,
                        reason=cap.reason,
                    )
                )
                continue
            if cap.action is GuardAction.CLAMP and cap.qty is not None:
                intent = intent.model_copy(update={"qty": cap.qty})
                clamp_reason = cap.reason

            intents.append(intent)

            # Guard 3: dry-run gate — record-only vs live.
            if not dry_run_route(dry_run=dry_run):
                results.append(
                    SubmissionResult(
                        client_order_id=intent.client_order_id,
                        status=SubmissionStatus.RECORDED,
                        reason=clamp_reason,
                    )
                )
                continue

            results.append(self._submit_live(intent, port=port, clamp_reason=clamp_reason))

        return OrderPlan(intents=tuple(intents), results=tuple(results))

    def _apply_position_cap(
        self,
        intent: OrderIntent,
        *,
        max_position_pct: Decimal | None,
        base_values: Mapping[str, Decimal] | None,
        positions: Mapping[str, Decimal] | None,
        prices: Mapping[str, Decimal] | None,
    ) -> GuardOutcome:
        """Run the position-cap guard when all of its inputs are available."""
        base_value = base_values.get(intent.account_seq) if base_values else None
        reference_price = prices.get(intent.ticker) if prices else None
        if max_position_pct is None or base_value is None or reference_price is None:
            return _PASS_OUTCOME

        current = positions.get(intent.ticker, Decimal("0")) if positions else Decimal("0")
        return position_cap_guard(
            intent,
            current_position_value=current,
            base_value=base_value,
            max_position_pct=max_position_pct,
            reference_price=reference_price,
        )

    def _submit_live(
        self,
        intent: OrderIntent,
        *,
        port: BrokerOrderPort | None,
        clamp_reason: str | None,
    ) -> SubmissionResult:
        """Submit an intent live with idempotent dedup against the ledger (R3)."""
        if port is None:  # pragma: no cover - defensive; live path requires a port
            raise ValueError("a BrokerOrderPort is required for live submission")

        coid = intent.client_order_id
        prior = self._ledger.get(coid)
        if prior is not None and prior.status in _DEDUP_TERMINAL:
            # Already submitted/deduped in this cycle: no second place_order.
            duplicate = prior.model_copy(update={"status": SubmissionStatus.DUPLICATE})
            self._ledger[coid] = duplicate
            return duplicate

        try:
            result = port.place_order(intent)
        except Exception as exc:
            result = SubmissionResult(
                client_order_id=coid,
                status=SubmissionStatus.FAILED,
                reason=str(exc),
            )

        if result.status is SubmissionStatus.SUBMITTED and clamp_reason is not None:
            result = result.model_copy(update={"reason": clamp_reason})

        self._ledger[coid] = result
        return result


_PASS_OUTCOME = GuardOutcome(action=GuardAction.PASS)
