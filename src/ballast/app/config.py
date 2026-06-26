"""The typed operational ``RunnerConfig`` lens (REQ-RUNNER-001-R1, FD1/FD2).

@CODE:SPEC-RUNNER-001

An immutable (pydantic frozen, ``extra="forbid"``) model carrying the
**operational** knobs the worker and dashboard share — ``ticker``,
``account_capital``, per-strategy ``allocation``, ``dry_run``, ``kill_switch`` —
and nothing else. It is a typed **lens** over the STATE-001
``ConfigSnapshotRecord.data`` (the generic mapping STATE-001 FD5 deliberately left
to "the Runner SPEC"), NOT a fork of the strategy-domain
``ballast.core.config.Config`` (VR/MAB knobs, instruments, ``account_seq`` stay
there) and NOT a new persisted record type.

Money/quantity fields (``account_capital`` and the ``allocation`` ratios) are
:class:`~decimal.Decimal` normalized to 2 dp via
:func:`ballast.core.models.quantize_money`; a bare ``float`` is rejected at
construction (reusing the ``_reject_float`` BeforeValidator pattern from CORE /
ORDER / STATE models) so precision never silently leaks onto the money path.

:meth:`RunnerConfig.to_snapshot` and :meth:`RunnerConfig.from_snapshot` are exact
inverses, serializing money as canonical decimal **strings** (never a binary
float) so the snapshot round-trips losslessly back to ``Decimal`` (FD2). The
shape matches the STATE-001 AC-18 generic mapping (``ticker`` / ``account_capital``
/ ``allocation`` / ``dry_run`` / ``kill_switch``).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

from ballast.core.models import quantize_money


def _reject_float(value: Any) -> Any:
    """Reject bare ``float`` money inputs before Decimal coercion.

    A string (e.g. ``"10000.00"``) or a ``Decimal`` passes through; a bare
    ``float`` (e.g. ``10000.5``) is rejected so precision never silently leaks
    onto the money path. ``bool`` is an ``int`` subclass in Python and is not
    affected here. Mirrors CORE / ORDER / STATE ``_reject_float``.
    """
    if isinstance(value, float):
        raise ValueError("money/quantity must be a string or Decimal, not a float")
    return value


def _normalize_money(value: Any) -> Any:
    """Normalize an accepted money value to 2 dp (after float rejection)."""
    if isinstance(value, Decimal):
        return quantize_money(value)
    if isinstance(value, str):
        return quantize_money(Decimal(value))
    return value


# A money/ratio value: never a bare float, always normalized to 2 dp.
Money = Annotated[Decimal, BeforeValidator(_normalize_money), BeforeValidator(_reject_float)]


def _normalize_allocation(value: Any) -> Any:
    """Reject floats and 2-dp-normalize every ratio of the allocation map.

    Applied to ``RunnerConfig.allocation`` so each per-strategy ratio is a 2-dp
    ``Decimal`` (never a bare ``float``), mirroring STATE-001's
    ``_normalize_decimal_map``.
    """
    if isinstance(value, Mapping):
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            _reject_float(raw)
            if isinstance(raw, Decimal):
                out[key] = quantize_money(raw)
            elif isinstance(raw, str):
                out[key] = quantize_money(Decimal(raw))
            else:
                raise ValueError("allocation ratios must be Decimal or decimal strings")
        return out
    return value


class RunnerConfig(BaseModel):
    """The typed operational config the worker and dashboard share (FD1).

    Frozen and ``extra="forbid"``: unknown keys fail loudly and instances are
    immutable. Carries only the operational knobs; it never redefines VR/MAB
    strategy knobs, instruments, or ``account_seq`` (those remain in
    ``ballast.core.config.Config``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    account_capital: Money
    allocation: Annotated[Mapping[str, Decimal], BeforeValidator(_normalize_allocation)]
    dry_run: bool
    kill_switch: bool

    def to_snapshot(self) -> dict[str, Any]:
        """Serialize to the STATE-001 generic snapshot shape (FD2).

        Money values (``account_capital`` and the ``allocation`` ratios) become
        canonical decimal **strings** (never a binary float) so
        ``StateStorePort.set_config`` persists them losslessly. The exact inverse
        is :meth:`from_snapshot`.
        """
        return {
            "ticker": self.ticker,
            "account_capital": str(self.account_capital),
            "allocation": {key: str(ratio) for key, ratio in self.allocation.items()},
            "dry_run": self.dry_run,
            "kill_switch": self.kill_switch,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> RunnerConfig:
        """Reconstruct a :class:`RunnerConfig` from a snapshot mapping (FD2).

        The exact inverse of :meth:`to_snapshot`: the canonical decimal strings are
        parsed back to ``Decimal`` (via the model's float-rejecting validators), so
        ``from_snapshot(cfg.to_snapshot())`` yields a ``RunnerConfig`` equal to
        ``cfg`` (lossless round-trip).
        """
        return cls.model_validate(dict(data))
