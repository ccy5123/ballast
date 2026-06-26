"""The worker's "run one cycle" core (REQ-RUNNER-001-R2..R6, FD3..FD7).

@CODE:SPEC-RUNNER-001

The single assembly point for every live action. The :class:`Runner` acquires
the STATE-001 single-writer lease once at startup and holds it across cycles
(Decision 4, option A), seeds the operational config snapshot once on first boot
(Decision 2, option A), and drives **one** cycle per :meth:`Runner.run_one_cycle`
call for **one** strategy / ``account_seq`` (Decision 1, option A):

    load operational config -> load persisted ``State`` -> ``strategy.plan_orders``
    -> ``OrderManager.place`` (reusing the ORDER-001 kill-switch / ``max_position_pct``
    / dry-run guards) -> durable ``upsert_order`` of each live result -> advance the
    strategy ``state_delta`` via ``apply_strategy_delta``.

The Runner **coordinates only**: it reads **no** wall clock and performs **no**
network IO of its own. The clock (``cycle_key``), the :class:`~ballast.core.models.Market`
snapshot (carrying ``is_open`` / ``is_holiday``), the ``StateStorePort``, and the
``BrokerOrderPort`` are all **injected**; the only IO happens inside the injected
store backend and the injected broker port. Money is ``Decimal`` (2 dp); secrets
are env-only (via the existing ``from_env``) and never logged, ``repr``-ed, or
embedded here.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import TracebackType

from ballast.app.config import RunnerConfig
from ballast.core.config import Config
from ballast.core.models import Market, State
from ballast.core.strategy import Strategy
from ballast.orders.manager import OrderManager
from ballast.orders.models import OrderPlan, SubmissionStatus
from ballast.orders.ports import BrokerOrderPort
from ballast.state.models import Lease, OrderState
from ballast.state.ports import StateStorePort

# Statuses durably written to the STATE-001 order ledger. A guard-blocked
# (BLOCKED) or dry-run record-only (RECORDED) result is deliberately NOT
# persisted (FD5 / R5: no ledger mutation for a blocked or non-submitted order).
_LEDGER_PERSIST: frozenset[SubmissionStatus] = frozenset(
    {SubmissionStatus.SUBMITTED, SubmissionStatus.DUPLICATE, SubmissionStatus.FAILED}
)

# Live-submission statuses (used to detect an idempotent DUPLICATE-only re-run).
_LIVE_STATUSES: frozenset[SubmissionStatus] = frozenset(
    {SubmissionStatus.SUBMITTED, SubmissionStatus.DUPLICATE}
)

# The ``filled_qty`` written at submission time; fills are applied later by the
# STATE-001 reconciliation routine (out of scope here).
_UNFILLED = Decimal("0")

# The environment variable carrying the configured worker identity (Decision 3).
_ENV_WORKER_ID = "WORKER_ID"


def resolve_owner(
    env: Mapping[str, str] | None = None,
    *,
    hostname: str | None = None,
    pid: int | None = None,
) -> str:
    """Resolve the single-writer lease owner identity (FD3, Decision 3, option A).

    Returns the configured ``WORKER_ID`` environment value when present, else a
    deterministic ``f"{hostname}:{pid}"`` fallback. A stable identity lets a
    redeploy reclaim / observe its own lease; the ``ttl`` covers a crashed owner.
    Pure and injectable (``env`` / ``hostname`` / ``pid`` are overridable) so a
    test can pin the owner with no wall-clock, UUID, or randomness.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    configured = source.get(_ENV_WORKER_ID)
    if configured:
        return configured
    host = hostname if hostname is not None else socket.gethostname()
    process = pid if pid is not None else os.getpid()
    return f"{host}:{process}"


class RunnerNotStartedError(RuntimeError):
    """Raised when a mutating cycle runs before the writer lease is acquired (R3).

    The Runner must not silently proceed without the single-writer lease; calling
    :meth:`Runner.run_one_cycle` before :meth:`Runner.start` raises this so a
    mutation / submission never happens lease-less.
    """


@dataclass(frozen=True, slots=True)
class AccountInputs:
    """Injected ``max_position_pct`` clamp inputs for one cycle (FD4, R5).

    The position / buying-power / reference-price maps the caller obtains from the
    ADAPTER-001 read port and threads into ``OrderManager.place`` so the existing
    position-cap guard applies unchanged. All money values are ``Decimal``.
    """

    base_values: Mapping[str, Decimal] = field(default_factory=dict)
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    prices: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CycleResult:
    """An inspectable, broker-neutral summary of one cycle (FD6).

    Carries the strategy ``ns``, the reused ORDER-001 :class:`OrderPlan` (intents
    plus their :class:`SubmissionResult`s), the ``state_delta`` the strategy
    surfaced, the resulting persisted strategy-state ``new_version``, and the
    ``client_order_id``s upserted into the STATE-001 ledger this cycle. It carries
    no Toss-specific field and no secret.
    """

    ns: str
    plan: OrderPlan
    state_delta: Mapping[str, Decimal]
    new_version: int
    ledger_upserts: tuple[str, ...]


class Runner:
    """The always-on worker's "run one cycle" core (sole writer, lease-holding).

    Construct with the injected ``StateStorePort`` and the resolved lease
    ``owner`` / ``ttl``; :meth:`start` acquires the single-writer lease once and
    holds it across cycles (Decision 4). The Runner owns one long-lived
    :class:`OrderManager`. It embeds no IO; the store, port, clock (``cycle_key``),
    and market snapshot are all injected.
    """

    def __init__(self, store: StateStorePort, *, owner: str, ttl: int | None = None) -> None:
        self._store = store
        self._owner = owner
        self._ttl = ttl
        self._lease: Lease | None = None
        self._manager = OrderManager()

    @property
    def lease_held(self) -> bool:
        """Whether the single-writer lease is currently held by this Runner."""
        return self._lease is not None

    def start(self) -> Lease:
        """Acquire the single-writer lease once and hold it (FD3, Decision 4).

        Idempotent: a second call returns the already-held lease without a second
        acquire. A concurrent acquire by a *different* owner raises
        ``WriterLeaseHeldError`` from the store (a stray second process cannot
        become a second writer).
        """
        if self._lease is None:
            self._lease = self._store.acquire_writer_lease(self._owner, ttl=self._ttl)
        return self._lease

    def shutdown(self) -> None:
        """Release the held writer lease on shutdown (FD3).

        Idempotent: a no-op when no lease is held. After release the store frees
        the lease so a restarted worker (or another writer) can re-take it.
        """
        if self._lease is not None:
            self._store.release_writer_lease(self._lease)
            self._lease = None

    def __enter__(self) -> Runner:
        """Acquire the lease on context entry (ergonomic startup)."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the lease on context exit (ergonomic shutdown)."""
        self.shutdown()

    def seed_config(self, cfg: RunnerConfig) -> int | None:
        """Seed the operational config snapshot once on first boot (R2, Decision 2).

        When the durable snapshot is empty (``get_config().version == 0``), persist
        ``cfg.to_snapshot()`` via ``set_config(..., expected_version=0)`` and return
        the new version. When already seeded, leave the snapshot untouched (the
        State Store snapshot — which the dashboard may have flipped — is the runtime
        authority, A3) and return ``None``.
        """
        record = self._store.get_config()
        if record.version == 0:
            return self._store.set_config(cfg.to_snapshot(), expected_version=0)
        return None

    def load_config(self) -> RunnerConfig:
        """Load the operational config from the authoritative snapshot (R2, A3).

        Reads ``get_config()`` and reconstructs ``RunnerConfig.from_snapshot`` so a
        dashboard-written ``dry_run`` / ``kill_switch`` flip is observed on the next
        cycle. Reads never require the lease.
        """
        return RunnerConfig.from_snapshot(self._store.get_config().data)

    def run_one_cycle(
        self,
        strategy: Strategy,
        market: Market,
        *,
        cfg: Config,
        cycle_key: str,
        port: BrokerOrderPort | None = None,
        account_inputs: AccountInputs | None = None,
    ) -> CycleResult:
        """Drive exactly one cycle for one strategy (FD4; R3/R4/R5/R6).

        Requires the single-writer lease to be held (raises
        :class:`RunnerNotStartedError` otherwise — never proceeds lease-less, R3).
        Loads the authoritative operational config, loads the persisted ``State``
        for ``strategy.ns``, calls the pure ``strategy.plan_orders(market, state,
        cfg)``, drives ``OrderManager.place`` (threading the operational
        ``dry_run`` / ``kill_switch`` and ``cfg.execution.max_position_pct`` plus
        the injected clamp inputs into the reused ORDER-001 guards), durably
        ``upsert_order``s each live result, and advances the strategy
        ``state_delta`` — returning an inspectable :class:`CycleResult`.

        Idempotent / re-entrant (FD7): re-running the same ``(ns, cycle_key,
        market, cfg)`` derives the same deterministic ``client_order_id``s; the
        injected port dedups them to ``DUPLICATE`` (no second distinct live order),
        and a pure-``DUPLICATE`` re-run skips ``apply_strategy_delta`` so the
        ``state_delta`` is never double-applied (convergence). The Runner reads no
        wall clock and does no IO of its own.
        """
        if self._lease is None:
            raise RunnerNotStartedError(
                "the single-writer lease must be acquired (start) before a mutating cycle"
            )
        inputs = account_inputs if account_inputs is not None else AccountInputs()

        runner_cfg = self.load_config()

        state_rec = self._store.load_strategy_state(strategy.ns)
        state = State(ns=strategy.ns, data=dict(state_rec.data))
        result = strategy.plan_orders(market, state, cfg)

        plan = self._manager.place(
            result.orders,
            ns=strategy.ns,
            cycle_key=cycle_key,
            dry_run=runner_cfg.dry_run,
            kill_switch=runner_cfg.kill_switch,
            max_position_pct=cfg.execution.max_position_pct,
            base_values=inputs.base_values,
            positions=inputs.positions,
            prices=inputs.prices,
            port=port,
        )

        ledger_upserts = self._persist_ledger(plan, cycle_key=cycle_key)
        new_version = self._advance_state(
            strategy.ns,
            plan,
            state_delta=result.state_delta,
            expected_version=state_rec.version,
        )

        return CycleResult(
            ns=strategy.ns,
            plan=plan,
            state_delta=result.state_delta,
            new_version=new_version,
            ledger_upserts=ledger_upserts,
        )

    def _persist_ledger(self, plan: OrderPlan, *, cycle_key: str) -> tuple[str, ...]:
        """Durably upsert each live submission result into the ledger (R4).

        Only ``SUBMITTED`` / ``DUPLICATE`` / ``FAILED`` results are persisted; a
        ``BLOCKED`` (guard-blocked) or ``RECORDED`` (dry-run) result mutates neither
        the ledger nor state (R5). ``ts`` is the injected ``cycle_key`` (an opaque
        stamp — the Runner reads no clock). Returns the upserted ``client_order_id``s.
        """
        upserts: list[str] = []
        for intent, res in zip(plan.intents, plan.results, strict=True):
            if res.status not in _LEDGER_PERSIST:
                continue
            self._store.upsert_order(
                res.client_order_id,
                account_seq=intent.account_seq,
                broker_order_id=res.broker_order_id,
                status=OrderState(res.status.value),
                ordered_qty=intent.qty,
                filled_qty=_UNFILLED,
                ts=cycle_key,
            )
            upserts.append(res.client_order_id)
        return tuple(upserts)

    def _advance_state(
        self,
        ns: str,
        plan: OrderPlan,
        *,
        state_delta: Mapping[str, Decimal],
        expected_version: int,
    ) -> int:
        """Apply the strategy ``state_delta`` unless this is a DUPLICATE re-run (R6).

        When every live submission this cycle came back ``DUPLICATE`` (the exact
        cycle already ran and submitted), the ``state_delta`` was already applied:
        the apply is skipped (convergence) so ``V_n`` is never advanced a second
        time. Otherwise the delta is applied with optimistic concurrency
        (``expected_version``); a genuine stale/concurrent re-apply is rejected by
        the store's ``ConcurrencyError`` guard (FD7).
        """
        live = [res for res in plan.results if res.status in _LIVE_STATUSES]
        already_applied = bool(live) and all(
            res.status is SubmissionStatus.DUPLICATE for res in live
        )
        if already_applied:
            return expected_version
        return self._store.apply_strategy_delta(ns, state_delta, expected_version=expected_version)
