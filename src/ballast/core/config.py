"""Pydantic Config schema loaded and validated from YAML (REQ-CORE-001-R5).

YAML is read only at this boundary; the validated :class:`Config` is plain data
passed into the pure core. Money/quantity fields are strict :class:`~decimal.Decimal`
(bare ``float`` literals are rejected, not silently coerced), and ``extra="forbid"``
makes unknown keys fail loudly with a field-addressable error.

@CODE:SPEC-CORE-001
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict


def _reject_float(value: Any) -> Any:
    """Reject bare ``float`` money inputs before Decimal coercion.

    YAML strings (e.g. ``"0.95"``) and ``Decimal`` instances pass through; a bare
    ``float`` (e.g. YAML ``0.95``) is rejected so precision never silently leaks
    onto the money path. ``bool`` is a ``float`` subclass conceptually but is an
    ``int`` subclass in Python, so it is not affected here.
    """
    if isinstance(value, float):
        raise ValueError("money/quantity must be a string or Decimal, not a float")
    return value


# A money/ratio value that must arrive as a string or Decimal, never a bare float.
Money = Annotated[Decimal, BeforeValidator(_reject_float)]

_FrozenForbid = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class CommonConfig(BaseModel):
    """Cross-cutting knobs shared by every strategy."""

    model_config = _FrozenForbid

    allow_fractional: bool
    round_digits: int
    strict_instrument: bool


class ExecutionConfig(BaseModel):
    """Broker/execution settings and the live-trading position guard."""

    model_config = _FrozenForbid

    broker: str
    dry_run: bool
    max_position_pct: Money


class InstrumentConfig(BaseModel):
    """A single instrument definition from the ``instruments:`` block."""

    model_config = _FrozenForbid

    leverage: int
    underlying: str
    default_target_pct: Money | None = None
    default_band: Money | None = None


class StrategyConfig(BaseModel):
    """Per-strategy config; may override target_pct/band and carries account_seq."""

    model_config = _FrozenForbid

    account_seq: str
    ticker: str
    target_pct: Money | None = None
    band: Money | None = None


class StrategiesConfig(BaseModel):
    """The ``strategies:`` block with the two known namespaces."""

    model_config = _FrozenForbid

    vr: StrategyConfig
    mab: StrategyConfig


class Config(BaseModel):
    """Top-level validated configuration for a ballast run."""

    model_config = _FrozenForbid

    common: CommonConfig
    execution: ExecutionConfig
    instruments: dict[str, InstrumentConfig]
    strategies: StrategiesConfig

    @classmethod
    def load(cls, path: str | Path) -> Config:
        """Load and validate a :class:`Config` from a YAML file.

        Raises :class:`FileNotFoundError` if the file is absent and a pydantic
        ``ValidationError`` (naming the offending field) on schema violations.
        """
        text = Path(path).read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        return cls.model_validate(raw)
