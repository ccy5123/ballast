"""Tests for the PURE reconciliation core (REQ-STATE-001-R4).

@TEST:SPEC-STATE-001

The reconciliation core reads no clock and does no IO: fills, order statuses, and
the current snapshot are injected. Position/avg-cost math is cross-checked against
the backtest engine's ``_apply_buy`` / ``_apply_sell`` so live and backtest state
evolve identically. Re-running over the same fills converges (idempotent).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ballast.backtest.engine import _apply_buy, _Ledger
from ballast.backtest.types import Fill
from ballast.core.models import Order, OrderType, Side
from ballast.state.models import (
    FillRecord,
    OrderLedgerRecord,
    OrderState,
    StateSnapshot,
    StrategyStateRecord,
)
from ballast.state.reconcile import reconcile

_COMMISSION = Decimal("0.50")


def _mab_snapshot(
    *,
    avg_price: str,
    holdings: str,
    seed_remaining: str,
    round_idx: str,
    order: OrderLedgerRecord | None = None,
) -> StateSnapshot:
    orders = {order.client_order_id: order} if order is not None else {}
    return StateSnapshot(
        strategy={
            "mab": StrategyStateRecord(
                ns="mab",
                data={
                    "avg_price": Decimal(avg_price),
                    "holdings": Decimal(holdings),
                    "seed_remaining": Decimal(seed_remaining),
                    "round_idx": Decimal(round_idx),
                },
                version=0,
            )
        },
        orders=orders,
    )


def _submitted(coid: str, broker_id: str, ordered_qty: str) -> OrderLedgerRecord:
    return OrderLedgerRecord(
        client_order_id=coid,
        account_seq="acc-1",
        broker_order_id=broker_id,
        status=OrderState.SUBMITTED,
        ordered_qty=Decimal(ordered_qty),
        filled_qty=Decimal("0"),
        ts="t0",
    )


# --- AC-11 — MAB BUY fill matches engine _apply_buy ----------------------------------


def test_mab_buy_fill_matches_engine_apply_buy() -> None:
    order = _submitted("mab-c1", "toss-1", "5")
    snap = _mab_snapshot(
        avg_price="80.00", holdings="10", seed_remaining="500.00", round_idx="2", order=order
    )
    fill = FillRecord(
        fill_id="f1",
        client_order_id="mab-c1",
        broker_order_id="toss-1",
        side=Side.BUY,
        qty=Decimal("5"),
        price=Decimal("84.00"),
        commission=_COMMISSION,
        ts="t1",
    )

    result = reconcile(snap, fills=[fill], order_statuses={})

    # Ledger order is now FILLED (filled_qty == ordered_qty).
    assert result.snapshot.orders["mab-c1"].status is OrderState.FILLED
    assert result.snapshot.orders["mab-c1"].filled_qty == Decimal("5.00")

    mab = result.snapshot.strategy["mab"].data

    # Cross-check against the backtest engine ledger math on the SAME fill.
    ledger = _Ledger(
        cash_usd=Decimal("0"),
        holdings=Decimal("10"),
        avg_price=Decimal("80.00"),
        seed_remaining=Decimal("500.00"),
        round_idx=Decimal("2"),
        v_n=Decimal("0"),
    )
    engine_fill = Fill(
        order=Order(
            side=Side.BUY,
            ticker="QLD",
            qty=Decimal("5"),
            limit_price=Decimal("84.00"),
            order_type=OrderType.RESERVED_LIMIT,
            account_seq="acc-1",
        ),
        fill_price=Decimal("84.00"),
        qty=Decimal("5"),
        date=date(2026, 6, 26),
        commission=_COMMISSION,
    )
    _apply_buy(engine_fill, ledger)

    assert mab["avg_price"] == ledger.avg_price
    assert mab["holdings"] == ledger.holdings == Decimal("15.00")
    assert mab["seed_remaining"] == ledger.seed_remaining
    assert mab["round_idx"] == ledger.round_idx == Decimal("3")
    assert all(isinstance(v, Decimal) for v in mab.values())


# --- AC-12 — idempotent: re-running over the same fill converges ---------------------


def test_reconcile_is_idempotent_no_double_count() -> None:
    order = _submitted("mab-c1", "toss-1", "5")
    snap = _mab_snapshot(
        avg_price="80.00", holdings="10", seed_remaining="500.00", round_idx="2", order=order
    )
    fill = FillRecord(
        fill_id="f1",
        client_order_id="mab-c1",
        broker_order_id="toss-1",
        side=Side.BUY,
        qty=Decimal("5"),
        price=Decimal("84.00"),
        commission=_COMMISSION,
        ts="t1",
    )

    once = reconcile(snap, fills=[fill], order_statuses={})
    twice = reconcile(once.snapshot, fills=[fill], order_statuses={})

    # No double-count: holdings stays 15, not 20.
    assert twice.snapshot.strategy["mab"].data["holdings"] == Decimal("15.00")
    assert twice.snapshot.orders["mab-c1"].status is OrderState.FILLED
    # Re-run is a no-op beyond the first application.
    assert twice.snapshot.strategy["mab"].data == once.snapshot.strategy["mab"].data
    assert twice.mutations == ()


# --- AC-13 — SELL fill + CANCELED order ----------------------------------------------


def test_mab_sell_fill_matches_engine_apply_sell() -> None:
    order = _submitted("mab-sell", "toss-2", "15")
    snap = _mab_snapshot(
        avg_price="84.00", holdings="15", seed_remaining="80.00", round_idx="3", order=order
    )
    fill = FillRecord(
        fill_id="f2",
        client_order_id="mab-sell",
        broker_order_id="toss-2",
        side=Side.SELL,
        qty=Decimal("15"),
        price=Decimal("90.00"),
        commission=_COMMISSION,
        ts="t2",
    )

    result = reconcile(snap, fills=[fill], order_statuses={})
    mab = result.snapshot.strategy["mab"].data

    assert mab["holdings"] == Decimal("0.00")
    assert mab["avg_price"] == Decimal("0.00")  # flat -> avg_price zeroed (engine _apply_sell)
    assert result.snapshot.orders["mab-sell"].status is OrderState.FILLED


def test_canceled_broker_status_transitions_unfilled_order() -> None:
    order = _submitted("vr-stale", "toss-stale", "2")
    snap = StateSnapshot(orders={"vr-stale": order})

    result = reconcile(snap, fills=[], order_statuses={"toss-stale": "CANCELED"})

    assert result.snapshot.orders["vr-stale"].status is OrderState.CANCELED


def test_expired_broker_status_transitions_unfilled_order() -> None:
    order = _submitted("vr-old", "toss-old", "2")
    snap = StateSnapshot(orders={"vr-old": order})

    result = reconcile(snap, fills=[], order_statuses={"toss-old": "EXPIRED"})

    assert result.snapshot.orders["vr-old"].status is OrderState.EXPIRED


def test_broker_status_does_not_regress_a_filled_order() -> None:
    order = OrderLedgerRecord(
        client_order_id="vr-done",
        account_seq="acc-1",
        broker_order_id="toss-done",
        status=OrderState.FILLED,
        ordered_qty=Decimal("2"),
        filled_qty=Decimal("2"),
        ts="t0",
    )
    snap = StateSnapshot(orders={"vr-done": order})

    result = reconcile(snap, fills=[], order_statuses={"toss-done": "CANCELED"})

    # A terminal FILLED status is never regressed by a stale broker status.
    assert result.snapshot.orders["vr-done"].status is OrderState.FILLED


# --- AC-14 — VR fill updates qty/pool, leaves V_n to the apply-delta channel ----------


def test_vr_fill_updates_qty_pool_leaves_v_n() -> None:
    order = _submitted("vr-buy", "toss-vr", "2")
    snap = StateSnapshot(
        strategy={
            "vr": StrategyStateRecord(
                ns="vr",
                data={
                    "V_n": Decimal("1000.00"),
                    "pool": Decimal("500.00"),
                    "qty": Decimal("4"),
                },
                version=0,
            )
        },
        orders={"vr-buy": order},
    )
    fill = FillRecord(
        fill_id="f3",
        client_order_id="vr-buy",
        broker_order_id="toss-vr",
        side=Side.BUY,
        qty=Decimal("2"),
        price=Decimal("100.00"),
        commission=Decimal("0"),
        ts="t3",
    )

    result = reconcile(snap, fills=[fill], order_statuses={})
    vr = result.snapshot.strategy["vr"].data

    assert vr["qty"] == Decimal("6.00")  # 4 -> 6
    assert vr["pool"] == Decimal("300.00")  # 500 - 100*2 debited
    assert vr["V_n"] == Decimal("1000.00")  # unchanged by reconciliation (R1 channel only)
    assert result.snapshot.orders["vr-buy"].status is OrderState.FILLED


# --- partial fill -> PARTIAL ----------------------------------------------------------


def test_partial_fill_marks_order_partial() -> None:
    order = _submitted("mab-part", "toss-3", "10")
    snap = _mab_snapshot(
        avg_price="80.00", holdings="0", seed_remaining="1000.00", round_idx="0", order=order
    )
    fill = FillRecord(
        fill_id="f4",
        client_order_id="mab-part",
        broker_order_id="toss-3",
        side=Side.BUY,
        qty=Decimal("4"),  # < ordered 10
        price=Decimal("80.00"),
        commission=Decimal("0"),
        ts="t4",
    )

    result = reconcile(snap, fills=[fill], order_statuses={})

    assert result.snapshot.orders["mab-part"].status is OrderState.PARTIAL
    assert result.snapshot.orders["mab-part"].filled_qty == Decimal("4.00")


# --- AC-15 — the core is pure (no clock, no IO) --------------------------------------


def test_reconcile_module_reads_no_clock_and_does_no_io() -> None:
    # The pure core's source imports no clock/IO module and references no
    # wall-clock or network/filesystem sink. Asserting on the source guarantees
    # purity structurally (AC-15) without fragile runtime monkeypatching.
    import inspect

    import ballast.state.reconcile as recon_mod

    source = inspect.getsource(recon_mod)
    forbidden = (
        "import datetime",
        "from datetime",
        "datetime.now",
        "date.today",
        "time.time",
        "import socket",
        "import requests",
        "import httpx",
        "open(",
        "Path(",
        "sqlite3",
    )
    for token in forbidden:
        assert token not in source, f"pure reconcile must not reference {token!r}"


def test_reconcile_is_deterministic_function_of_its_arguments() -> None:
    # Same inputs -> identical outputs every time (no hidden clock/IO state).
    order = _submitted("mab-c1", "toss-1", "5")
    snap = _mab_snapshot(
        avg_price="80.00", holdings="10", seed_remaining="500.00", round_idx="2", order=order
    )
    fill = FillRecord(
        fill_id="f1",
        client_order_id="mab-c1",
        broker_order_id="toss-1",
        side=Side.BUY,
        qty=Decimal("5"),
        price=Decimal("84.00"),
        commission=_COMMISSION,
        ts="t1",
    )

    first = reconcile(snap, fills=[fill], order_statuses={})
    second = reconcile(snap, fills=[fill], order_statuses={})
    assert first.snapshot.strategy["mab"].data == second.snapshot.strategy["mab"].data
    assert first.snapshot.orders["mab-c1"].status is OrderState.FILLED
    assert len(first.mutations) >= 1


def test_fill_located_by_broker_order_id_when_coid_absent() -> None:
    # A fill that carries only a broker_order_id is matched to its ledger order.
    order = _submitted("mab-c1", "toss-77", "3")
    snap = _mab_snapshot(
        avg_price="80.00", holdings="0", seed_remaining="1000.00", round_idx="0", order=order
    )
    fill = FillRecord(
        fill_id="f-by-broker",
        client_order_id=None,
        broker_order_id="toss-77",
        side=Side.BUY,
        qty=Decimal("3"),
        price=Decimal("80.00"),
        commission=Decimal("0"),
        ts="t0",
    )
    result = reconcile(snap, fills=[fill], order_statuses={})
    assert result.snapshot.orders["mab-c1"].status is OrderState.FILLED


def test_fill_advances_ledger_without_strategy_record() -> None:
    # The fill matches a ledger order but no strategy state exists for its ns:
    # the ledger status advances yet no position state is recomputed.
    order = _submitted("mab-noState", "toss-ns", "3")
    snap = StateSnapshot(orders={"mab-noState": order})  # no strategy record
    fill = FillRecord(
        fill_id="f-ns",
        client_order_id="mab-noState",
        broker_order_id="toss-ns",
        side=Side.BUY,
        qty=Decimal("3"),
        price=Decimal("80.00"),
        commission=Decimal("0"),
        ts="t0",
    )
    result = reconcile(snap, fills=[fill], order_statuses={})
    assert result.snapshot.orders["mab-noState"].status is OrderState.FILLED
    assert "mab" not in result.snapshot.strategy
    # Only the ledger mutation is recorded (no strategy_state mutation).
    assert {m.kind for m in result.mutations} == {"ledger_status"}


def test_fill_broker_id_scans_multiple_orders_no_match() -> None:
    # client_order_id is unknown and the broker_order_id matches none of several
    # orders: the fill is an orphan (records only its watermark).
    o1 = _submitted("vr-a", "toss-a", "1")
    o2 = _submitted("vr-b", "toss-b", "1")
    snap = StateSnapshot(orders={"vr-a": o1, "vr-b": o2})
    fill = FillRecord(
        fill_id="f-miss",
        client_order_id="unknown-coid",
        broker_order_id="toss-MISSING",
        side=Side.BUY,
        qty=Decimal("1"),
        price=Decimal("10.00"),
        commission=Decimal("0"),
        ts="t0",
    )
    result = reconcile(snap, fills=[fill], order_statuses={})
    assert result.snapshot.orders["vr-a"].status is OrderState.SUBMITTED
    assert result.snapshot.orders["vr-b"].status is OrderState.SUBMITTED
    assert result.snapshot.applied_fills["f-miss"] == Decimal("1")
    assert result.mutations == ()


def test_fill_with_no_matching_order_records_watermark_only() -> None:
    # An orphan fill (no ledger order, no strategy ns) updates only the watermark.
    snap = StateSnapshot()
    fill = FillRecord(
        fill_id="orphan",
        client_order_id="zz-unknown",
        broker_order_id=None,
        side=Side.BUY,
        qty=Decimal("1"),
        price=Decimal("10.00"),
        commission=Decimal("0"),
        ts="t0",
    )
    result = reconcile(snap, fills=[fill], order_statuses={})
    assert result.snapshot.applied_fills["orphan"] == Decimal("1")
    assert result.mutations == ()


def test_vr_sell_fill_updates_qty_and_pool() -> None:
    order = _submitted("vr-sell", "toss-vs", "2")
    snap = StateSnapshot(
        strategy={
            "vr": StrategyStateRecord(
                ns="vr",
                data={
                    "V_n": Decimal("1000.00"),
                    "pool": Decimal("100.00"),
                    "qty": Decimal("6"),
                },
                version=0,
            )
        },
        orders={"vr-sell": order},
    )
    fill = FillRecord(
        fill_id="f-vs",
        client_order_id="vr-sell",
        broker_order_id="toss-vs",
        side=Side.SELL,
        qty=Decimal("2"),
        price=Decimal("100.00"),
        commission=Decimal("0"),
        ts="t0",
    )
    result = reconcile(snap, fills=[fill], order_statuses={})
    vr = result.snapshot.strategy["vr"].data
    assert vr["qty"] == Decimal("4.00")  # 6 -> 4
    assert vr["pool"] == Decimal("300.00")  # 100 + 100*2 proceeds
    assert vr["V_n"] == Decimal("1000.00")  # unchanged by reconciliation


def test_mab_partial_sell_keeps_holdings_and_avg_price() -> None:
    # A SELL that leaves holdings positive does NOT zero avg_price (engine parity).
    order = _submitted("mab-psell", "toss-ps", "5")
    snap = _mab_snapshot(
        avg_price="84.00", holdings="15", seed_remaining="0.00", round_idx="3", order=order
    )
    fill = FillRecord(
        fill_id="f-ps",
        client_order_id="mab-psell",
        broker_order_id="toss-ps",
        side=Side.SELL,
        qty=Decimal("5"),  # 15 -> 10 (still positive)
        price=Decimal("90.00"),
        commission=Decimal("0"),
        ts="t0",
    )
    result = reconcile(snap, fills=[fill], order_statuses={})
    mab = result.snapshot.strategy["mab"].data
    assert mab["holdings"] == Decimal("10.00")
    assert mab["avg_price"] == Decimal("84.00")  # unchanged while still holding


def test_fill_does_not_advance_a_terminal_ledger_order() -> None:
    # A late fill for an already-CANCELED order does not regress its status.
    canceled = OrderLedgerRecord(
        client_order_id="mab-late",
        account_seq="acc-1",
        broker_order_id="toss-late",
        status=OrderState.CANCELED,
        ordered_qty=Decimal("5"),
        filled_qty=Decimal("0"),
        ts="t0",
    )
    snap = _mab_snapshot(
        avg_price="80.00", holdings="0", seed_remaining="500.00", round_idx="0", order=canceled
    )
    fill = FillRecord(
        fill_id="f-late",
        client_order_id="mab-late",
        broker_order_id="toss-late",
        side=Side.BUY,
        qty=Decimal("5"),
        price=Decimal("80.00"),
        commission=Decimal("0"),
        ts="t1",
    )
    result = reconcile(snap, fills=[fill], order_statuses={})
    # Status stays CANCELED (terminal, never regressed by a late fill).
    assert result.snapshot.orders["mab-late"].status is OrderState.CANCELED


def test_non_terminal_broker_status_is_ignored() -> None:
    # A broker status that is not CANCELED/EXPIRED (e.g. FILLED) is not a
    # reconcile-against terminal here; the ledger is left to the fill path.
    order = _submitted("vr-x", "toss-x", "2")
    snap = StateSnapshot(orders={"vr-x": order})
    result = reconcile(snap, fills=[], order_statuses={"toss-x": "FILLED"})
    assert result.snapshot.orders["vr-x"].status is OrderState.SUBMITTED
    assert result.mutations == ()


def test_broker_status_for_unknown_broker_id_is_ignored() -> None:
    order = _submitted("vr-y", "toss-y", "2")
    snap = StateSnapshot(orders={"vr-y": order})
    result = reconcile(snap, fills=[], order_statuses={"toss-MISSING": "CANCELED"})
    assert result.snapshot.orders["vr-y"].status is OrderState.SUBMITTED


def test_reconcile_mutations_are_inspectable() -> None:
    order = _submitted("mab-c1", "toss-1", "5")
    snap = _mab_snapshot(
        avg_price="80.00", holdings="10", seed_remaining="500.00", round_idx="2", order=order
    )
    fill = FillRecord(
        fill_id="f1",
        client_order_id="mab-c1",
        broker_order_id="toss-1",
        side=Side.BUY,
        qty=Decimal("5"),
        price=Decimal("84.00"),
        commission=_COMMISSION,
        ts="t1",
    )

    result = reconcile(snap, fills=[fill], order_statuses={})
    kinds = {m.kind for m in result.mutations}
    assert "ledger_status" in kinds
    assert "strategy_state" in kinds
