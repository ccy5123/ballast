"""Shared semantics for the State Store backends and reconciler (internal).

@CODE:SPEC-STATE-001

Centralizes the per-namespace **known-key set** for apply-delta (mirroring the
backtest engine's ``_build_state`` keys) and the monotonic, non-regressing order
status lifecycle so the in-memory store, the SQLite store, and the pure reconciler
all enforce identical rules (FD3 / FD4 / FD8 parity).
"""

from __future__ import annotations

from ballast.state.models import TERMINAL_STATES, OrderState

# Per-namespace known keys for apply-delta (FD2/FD3), matching the backtest
# engine's ``_build_state`` (VR: V_n/pool/qty; MAB: avg_price/holdings/
# seed_remaining/round_idx). Unknown keys are ignored (forward-compatible).
KNOWN_KEYS: dict[str, frozenset[str]] = {
    "vr": frozenset({"V_n", "pool", "qty"}),
    "mab": frozenset({"avg_price", "holdings", "seed_remaining", "round_idx"}),
}


def known_keys_for(ns: str) -> frozenset[str]:
    """Return the known apply-delta keys for ``ns`` (empty for an unknown ns)."""
    return KNOWN_KEYS.get(ns, frozenset())


def status_allows_transition(current: OrderState, proposed: OrderState) -> bool:
    """Return whether ``current -> proposed`` is a permitted ledger transition (FD4).

    A terminal status (FILLED/CANCELED/EXPIRED) never regresses: once terminal,
    only a same-status re-upsert (a no-op) is allowed. A non-terminal status may
    advance to any status. This is the single monotonic rule shared by every
    backend and the reconciler.
    """
    if current == proposed:
        return True
    return current not in TERMINAL_STATES
