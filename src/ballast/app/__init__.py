"""Public surface of the ballast application / orchestration layer (RUNNER-001).

@CODE:SPEC-RUNNER-001

The worker's "run one cycle" core: the typed operational :class:`RunnerConfig`
lens over the STATE-001 config snapshot, and the :class:`Runner` that acquires the
single-writer lease, seeds/loads the config, and drives one cycle
(``Strategy.plan_orders`` -> ``OrderManager`` -> injected ``BrokerOrderPort``)
returning an inspectable :class:`CycleResult`. This layer embeds no IO and reads
no wall clock: the store, port, clock (``cycle_key``), and market snapshot are all
injected. Money is ``Decimal`` (2 dp); secrets are env-only (via ``from_env``) and
never logged. It wires existing CORE / STRATEGY / ORDER / STATE / ADAPTER
contracts and forks none of them.
"""

from __future__ import annotations

from ballast.app.config import RunnerConfig
from ballast.app.runner import (
    AccountInputs,
    CycleResult,
    Runner,
    RunnerNotStartedError,
    resolve_owner,
)

__all__ = [
    "AccountInputs",
    "CycleResult",
    "Runner",
    "RunnerConfig",
    "RunnerNotStartedError",
    "resolve_owner",
]
