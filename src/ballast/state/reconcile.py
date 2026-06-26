"""The PURE reconciliation core (REQ-STATE-001-R4, FD7).

@CODE:SPEC-STATE-001

``reconcile(snapshot, fills, order_statuses) -> ReconResult`` turns executed fills
plus the current persisted snapshot into the next snapshot. It is a **pure**
function: no clock, no network, no filesystem IO, and no ``float`` — fills, order
statuses, and any ``ts`` are injected. Re-running over the same fills converges
(idempotent): a fill already applied (keyed by ``fill_id`` at its filled-qty
watermark) is not double-counted, and a terminal ledger status never regresses.

The position/avg-cost recomputation mirrors the backtest engine's
``_apply_buy`` / ``_apply_sell`` (``ballast.backtest.engine``) byte-for-byte so
live and backtest state evolve identically:

* MAB BUY: volume-weighted ``avg_price``; grow ``holdings``; debit
  ``seed_remaining`` by ``price*qty + commission`` (floored at 0); ``round_idx += 1``.
* MAB SELL: reduce ``holdings``; zero ``avg_price`` at flat.
* VR BUY/SELL: update ``qty`` (holdings) and ``pool`` (cash); ``V_n`` is left to
  the R1 apply-delta channel, never advanced from fills.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from ballast.core.models import Side, quantize_money
from ballast.state._shared import status_allows_transition
from ballast.state.models import (
    FillRecord,
    OrderLedgerRecord,
    OrderState,
    ReconMutation,
    ReconResult,
    StateSnapshot,
    StrategyStateRecord,
)

_ZERO = Decimal("0")

# Broker statuses that reconcile a SUBMITTED-but-unfilled order to a terminal.
_BROKER_TERMINALS: dict[str, OrderState] = {
    "CANCELED": OrderState.CANCELED,
    "EXPIRED": OrderState.EXPIRED,
}


def reconcile(
    snapshot: StateSnapshot,
    fills: Sequence[FillRecord],
    order_statuses: Mapping[str, str],
) -> ReconResult:
    """Reconcile ``fills`` + broker ``order_statuses`` against ``snapshot`` (pure)."""
    strategy: dict[str, StrategyStateRecord] = dict(snapshot.strategy)
    orders: dict[str, OrderLedgerRecord] = dict(snapshot.orders)
    applied_fills: dict[str, Decimal] = dict(snapshot.applied_fills)
    mutations: list[ReconMutation] = []

    for fill in fills:
        _apply_fill(fill, strategy, orders, applied_fills, mutations)

    _apply_broker_statuses(order_statuses, orders, mutations)

    new_snapshot = StateSnapshot(strategy=strategy, orders=orders, applied_fills=applied_fills)
    return ReconResult(snapshot=new_snapshot, mutations=tuple(mutations))


def _apply_fill(
    fill: FillRecord,
    strategy: dict[str, StrategyStateRecord],
    orders: dict[str, OrderLedgerRecord],
    applied_fills: dict[str, Decimal],
    mutations: list[ReconMutation],
) -> None:
    """Apply one fill to the ledger + strategy state, idempotently (watermark)."""
    watermark = applied_fills.get(fill.fill_id)
    if watermark is not None and watermark >= fill.qty:
        return  # already applied at/above this filled-qty watermark (no double-count)

    coid = _locate_order_key(fill, orders)
    if coid is not None:
        _advance_ledger_for_fill(coid, fill, orders, mutations)
        ns = _ns_of(coid)
        record = strategy.get(ns)
        if record is not None:
            strategy[ns] = _apply_fill_to_state(record, fill)
            mutations.append(ReconMutation(kind="strategy_state", target=ns))

    applied_fills[fill.fill_id] = fill.qty


def _locate_order_key(fill: FillRecord, orders: Mapping[str, OrderLedgerRecord]) -> str | None:
    """Find the ledger key for a fill by ``client_order_id`` then ``broker_order_id``."""
    if fill.client_order_id is not None and fill.client_order_id in orders:
        return fill.client_order_id
    if fill.broker_order_id is not None:
        for key, record in orders.items():
            if record.broker_order_id == fill.broker_order_id:
                return key
    return None


def _advance_ledger_for_fill(
    coid: str,
    fill: FillRecord,
    orders: dict[str, OrderLedgerRecord],
    mutations: list[ReconMutation],
) -> None:
    """Advance a ledger order's status from a fill (SUBMITTED -> PARTIAL -> FILLED)."""
    record = orders[coid]
    new_filled = quantize_money(record.filled_qty + fill.qty)
    if new_filled >= record.ordered_qty:
        new_status = OrderState.FILLED
        new_filled = record.ordered_qty
    else:
        new_status = OrderState.PARTIAL
    if not status_allows_transition(record.status, new_status):
        return  # never regress a terminal status
    orders[coid] = record.model_copy(update={"status": new_status, "filled_qty": new_filled})
    mutations.append(ReconMutation(kind="ledger_status", target=coid, status=new_status))


def _apply_fill_to_state(record: StrategyStateRecord, fill: FillRecord) -> StrategyStateRecord:
    """Recompute position state from a fill, mirroring the engine ledger math."""
    data = dict(record.data)
    if record.ns == "mab":
        _apply_mab_fill(data, fill)
    else:  # VR (and any vr-namespaced state): update qty/pool, leave V_n.
        _apply_vr_fill(data, fill)
    return StrategyStateRecord(ns=record.ns, data=data, version=record.version)


def _apply_mab_fill(data: dict[str, Decimal], fill: FillRecord) -> None:
    """MAB position update from a fill (== engine ``_apply_buy`` / ``_apply_sell``)."""
    avg_price = data.get("avg_price", _ZERO)
    holdings = data.get("holdings", _ZERO)
    seed_remaining = data.get("seed_remaining", _ZERO)
    round_idx = data.get("round_idx", _ZERO)

    if fill.side is Side.BUY:
        new_avg, new_holdings, new_seed = _buy_math(
            avg_price, holdings, seed_remaining, fill.price, fill.qty, fill.commission
        )
        data["avg_price"] = new_avg
        data["holdings"] = new_holdings
        data["seed_remaining"] = new_seed
        data["round_idx"] = round_idx + Decimal("1")
    else:
        new_holdings = quantize_money(holdings - fill.qty)
        data["holdings"] = new_holdings
        if new_holdings <= _ZERO:
            data["avg_price"] = _ZERO


def _apply_vr_fill(data: dict[str, Decimal], fill: FillRecord) -> None:
    """VR position update from a fill: update ``qty`` + ``pool``; leave ``V_n``."""
    qty = data.get("qty", _ZERO)
    pool = data.get("pool", _ZERO)
    if fill.side is Side.BUY:
        debit = fill.price * fill.qty + fill.commission
        data["qty"] = quantize_money(qty + fill.qty)
        data["pool"] = quantize_money(pool - debit)
    else:
        proceeds = fill.price * fill.qty
        data["qty"] = quantize_money(qty - fill.qty)
        data["pool"] = quantize_money(pool + proceeds - fill.commission)


def _buy_math(
    avg_price: Decimal,
    holdings: Decimal,
    cash: Decimal,
    price: Decimal,
    qty: Decimal,
    commission: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Engine-identical BUY arithmetic (``_apply_buy``): new (avg_price, holdings, cash).

    ``cash`` is the debited balance (MAB ``seed_remaining``); the new balance is
    floored at zero exactly as the engine floors ``seed_remaining``.
    """
    debit = price * qty + commission
    new_holdings = holdings + qty
    weighted = avg_price * holdings + price * qty
    new_avg = quantize_money(weighted / new_holdings)
    return (
        new_avg,
        quantize_money(new_holdings),
        quantize_money(max(_ZERO, cash - debit)),
    )


def _apply_broker_statuses(
    order_statuses: Mapping[str, str],
    orders: dict[str, OrderLedgerRecord],
    mutations: list[ReconMutation],
) -> None:
    """Transition SUBMITTED-but-unfilled orders to CANCELED/EXPIRED per broker truth."""
    by_broker_id = {
        record.broker_order_id: key
        for key, record in orders.items()
        if record.broker_order_id is not None
    }
    for broker_id, raw_status in order_statuses.items():
        terminal = _BROKER_TERMINALS.get(raw_status.upper())
        if terminal is None:
            continue
        coid = by_broker_id.get(broker_id)
        if coid is None:
            continue
        record = orders[coid]
        if not status_allows_transition(record.status, terminal):
            continue  # never regress a terminal (e.g. already FILLED)
        orders[coid] = record.model_copy(update={"status": terminal})
        mutations.append(ReconMutation(kind="ledger_status", target=coid, status=terminal))


def _ns_of(client_order_id: str) -> str:
    """Derive the strategy namespace from a deterministic ORDER-001 ``client_order_id``.

    The ORDER-001 ``derive_client_order_id`` always prefixes the id with the
    namespace (e.g. ``"vr-..."`` / ``"mab-..."``), so the leading segment before
    the first ``"-"`` is the namespace. This keeps the pure core free of any
    account->namespace lookup or store access.
    """
    head, _, _rest = client_order_id.partition("-")
    return head
