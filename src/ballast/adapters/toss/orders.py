"""Toss order-write adapter (REQ-ADAPTER-002 R1-R5).

@CODE:SPEC-ADAPTER-002

The concrete :class:`~ballast.orders.ports.BrokerOrderPort` the P2 Order Manager
calls on the live path. :meth:`TossOrderAdapter.place_order` maps a broker-neutral
:class:`~ballast.orders.models.OrderIntent` onto the quantity-based Toss
``OrderCreateRequest`` (FD1/FD4) and ``POST /api/v1/orders``;
:meth:`TossOrderAdapter.cancel_order` resolves ``client_order_id -> orderId`` from
an in-process map (FD7) and ``POST /api/v1/orders/{orderId}/cancel`` (FD5).

Money/quantity values are serialized as decimal **strings** from ``Decimal``
(``format: decimal``); ``float`` never appears on the money path (R5). Every Toss
error is caught and mapped to a :class:`~ballast.orders.models.SubmissionResult`
(``FAILED``) — a :class:`~ballast.adapters.errors.TossError` never leaks onto the
caller (R4). No secret/token is placed on a log line, ``reason``, or ``repr`` (R5).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from ballast.adapters.errors import TossError
from ballast.adapters.toss.client import TossClient
from ballast.orders.models import (
    OrderIntent,
    OrderKind,
    SubmissionResult,
    SubmissionStatus,
    Tif,
)

logger = logging.getLogger("ballast.adapters")

_CLS_NON_US_REASON = "CLS (LOC) is supported for US stocks only"


def _decimal_str(value: Decimal) -> str:
    """Serialize a ``Decimal`` to a canonical ``format: decimal`` string.

    Strips the 2-dp normalization the upstream ``OrderIntent`` applies so the
    emitted value matches the Toss ``^\\d+(\\.\\d+)?$`` shape (e.g. ``"70000"``,
    ``"185.5"``, ``"0.5"``) while preserving the exact numeric value. ``float`` is
    never involved.
    """
    normalized = value.normalize()
    _sign, _digits, exponent = normalized.as_tuple()
    # A positive exponent (e.g. ``7E+4``) is a trailing-zero integer in scientific
    # form; re-quantize to exponent 0 so it renders as plain digits, not ``7E+4``.
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized}"


def _is_kr_symbol(ticker: str) -> bool:
    """True for a clearly KR symbol (all-digit / 6-digit numeric; FD4)."""
    return ticker.isdigit()


def _build_create_body(intent: OrderIntent) -> dict[str, str]:
    """Map an ``OrderIntent`` onto a quantity-based ``OrderCreateRequest`` (FD4).

    ``confirmHighValueOrder`` is never set (omitted -> defaults ``false``; R5
    safety stop). ``price`` is present only for ``LIMIT`` and absent for
    ``MARKET``. ``orderAmount`` (the US-MARKET amount-based variant) is never
    produced.
    """
    body: dict[str, str] = {
        "clientOrderId": intent.client_order_id,
        "symbol": intent.ticker,
        "side": intent.side.value,
        "orderType": intent.kind.value,
        "timeInForce": intent.tif.value,
        "quantity": _decimal_str(intent.qty),
    }
    if intent.kind is OrderKind.LIMIT and intent.limit_price is not None:
        body["price"] = _decimal_str(intent.limit_price)
    return body


class TossOrderAdapter:
    """Order-write adapter over a :class:`TossClient` (``BrokerOrderPort``)."""

    def __init__(self, client: TossClient) -> None:
        self._client = client
        # client_order_id -> the original SUBMITTED result (carries the orderId).
        # Populated on create success; drives idempotent-replay relabeling (R2)
        # and cancel resolution (R3, FD7). In-process only; cross-restart
        # resolution is a pre-live follow-up.
        self._submitted: dict[str, SubmissionResult] = {}

    def place_order(self, intent: OrderIntent) -> SubmissionResult:
        """``POST /api/v1/orders`` for ``intent`` (R1/R2/R4/R5)."""
        # R5 — fail fast on CLS for a clearly KR symbol (Toss would 400 anyway),
        # without an HTTP call. Ambiguous (non-numeric) symbols are forwarded.
        if intent.tif is Tif.CLS and _is_kr_symbol(intent.ticker):
            return SubmissionResult(
                client_order_id=intent.client_order_id,
                status=SubmissionStatus.FAILED,
                reason=_CLS_NON_US_REASON,
            )

        prior = self._submitted.get(intent.client_order_id)
        body = _build_create_body(intent)
        try:
            result = self._client.post("/api/v1/orders", json=body, account_seq=intent.account_seq)
        except TossError as exc:
            return self._failed(intent.client_order_id, exc)

        order_id = str(result["orderId"])
        if prior is not None:
            # R2 — a within-window idempotent replay: relabel the prior result
            # DUPLICATE; no second distinct live order is created.
            return SubmissionResult(
                client_order_id=intent.client_order_id,
                status=SubmissionStatus.DUPLICATE,
                broker_order_id=prior.broker_order_id,
            )

        submitted = SubmissionResult(
            client_order_id=intent.client_order_id,
            status=SubmissionStatus.SUBMITTED,
            broker_order_id=order_id,
        )
        self._submitted[intent.client_order_id] = submitted
        return submitted

    def cancel_order(self, account_seq: str, client_order_id: str) -> SubmissionResult:
        """``POST /api/v1/orders/{orderId}/cancel`` for ``client_order_id`` (R3/R4)."""
        prior = self._submitted.get(client_order_id)
        if prior is None or prior.broker_order_id is None:
            # R3 — unknown key (e.g. created in a previous process): fail without
            # guessing an orderId and without calling Toss.
            return SubmissionResult(
                client_order_id=client_order_id,
                status=SubmissionStatus.FAILED,
                reason=f"unknown client_order_id: {client_order_id}",
            )

        try:
            result = self._client.post(
                f"/api/v1/orders/{prior.broker_order_id}/cancel",
                account_seq=account_seq,
            )
        except TossError as exc:
            return self._failed(client_order_id, exc)

        return SubmissionResult(
            client_order_id=client_order_id,
            status=SubmissionStatus.SUBMITTED,
            broker_order_id=str(result["orderId"]),
        )

    @staticmethod
    def _failed(client_order_id: str, exc: TossError) -> SubmissionResult:
        """Map a typed Toss error to a ``FAILED`` result carrying its stable code.

        ``reason`` carries only the Toss ``code`` (+ message) — never a secret or
        token. The ``requestId`` is logged for traceability, not embedded here.
        """
        code = getattr(exc, "code", "error")
        message = getattr(exc, "message", "")
        reason = f"{code}: {message}".rstrip(": ") if message else code
        return SubmissionResult(
            client_order_id=client_order_id,
            status=SubmissionStatus.FAILED,
            reason=reason,
        )
