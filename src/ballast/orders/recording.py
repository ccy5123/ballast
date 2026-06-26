"""In-memory, network-free recording order port (REQ-ORDER-001-R5).

@CODE:SPEC-ORDER-001

``RecordingBrokerOrderPort`` structurally satisfies ``BrokerOrderPort`` and acts
as a faithful, idempotent stand-in for a real broker write adapter in tests: it
records every ``place_order`` / ``cancel_order`` call, returns ``SUBMITTED`` with
a synthetic ``broker_order_id`` for a new ``client_order_id``, and returns the
prior result as ``DUPLICATE`` on a repeat. It needs no credentials, base URL, or
network access.
"""

from __future__ import annotations

from ballast.orders.models import OrderIntent, SubmissionResult, SubmissionStatus


class RecordingBrokerOrderPort:
    """A network-free, idempotent in-memory implementation of ``BrokerOrderPort``."""

    def __init__(self) -> None:
        self._intents: list[OrderIntent] = []
        self._results: dict[str, SubmissionResult] = {}
        self._cancels: list[tuple[str, str]] = []

    def place_order(self, intent: OrderIntent) -> SubmissionResult:
        """Record a new intent (SUBMITTED) or return the prior result (DUPLICATE)."""
        prior = self._results.get(intent.client_order_id)
        if prior is not None:
            return prior.model_copy(update={"status": SubmissionStatus.DUPLICATE})

        broker_order_id = f"rec-{len(self._intents)}"
        result = SubmissionResult(
            client_order_id=intent.client_order_id,
            status=SubmissionStatus.SUBMITTED,
            broker_order_id=broker_order_id,
        )
        self._intents.append(intent)
        self._results[intent.client_order_id] = result
        return result

    def cancel_order(self, account_seq: str, client_order_id: str) -> SubmissionResult:
        """Record a cancel request and return a synthetic submission result."""
        self._cancels.append((account_seq, client_order_id))
        return SubmissionResult(
            client_order_id=client_order_id,
            status=SubmissionStatus.SUBMITTED,
            broker_order_id=f"cancel-{len(self._cancels) - 1}",
        )

    @property
    def recorded_intents(self) -> tuple[OrderIntent, ...]:
        """The ordered intents accepted by ``place_order`` (for inspection)."""
        return tuple(self._intents)

    @property
    def results(self) -> tuple[SubmissionResult, ...]:
        """The recorded submission results (for inspection)."""
        return tuple(self._results.values())

    @property
    def cancels(self) -> tuple[tuple[str, str], ...]:
        """The ordered ``(account_seq, client_order_id)`` cancel requests."""
        return tuple(self._cancels)
