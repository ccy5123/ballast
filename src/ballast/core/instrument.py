"""Instrument registry, resolution chain, and guardrails (REQ-CORE-001-R3 / R4).

The registry turns a validated :class:`~ballast.core.config.Config` into
:class:`~ballast.core.models.InstrumentMeta` lookups and applies the
``explicit > default > error`` resolution chain for ``target_pct`` / ``band``.

``validate_instrument`` enforces the strategy's behavioral assumptions: leveraged
ETFs tracking a broad index. Guardrail breaches warn (or block under ``strict``);
an unresolved ``target_pct`` / ``band`` is a hard data error regardless of ``strict``.

@CODE:SPEC-CORE-001
"""

from __future__ import annotations

import warnings
from decimal import Decimal

from ballast.core.config import Config
from ballast.core.models import InstrumentMeta

# Known broad-index symbols. Data-driven (no per-ticker branching) so the
# guardrail can classify "index vs single/theme" without hardcoding instruments.
KNOWN_INDEX_SYMBOLS: frozenset[str] = frozenset(
    {"NDX", "SOX", "SPX", "DJI", "RUT", "NDX100", "SP500"}
)

_LEVERAGE_WARNING = "Laoer strategies assume leveraged ETFs; 1x / single-stock breaks behavior"
_NON_INDEX_WARNING = (
    "underlying is not a broad index (single/theme stock); "
    "Laoer strategies recommend against single/theme stocks"
)

MIN_LEVERAGE = 2


class UnknownTickerError(KeyError):
    """Raised when a ticker is not present in the instrument registry."""


class UnresolvedParameterError(ValueError):
    """Raised when ``target_pct``/``band`` is unresolved after the chain.

    This is a hard data error distinct from a guardrail warning and from an
    :class:`UnknownTickerError`.
    """


def is_index_underlying(underlying: str) -> bool:
    """Return ``True`` if ``underlying`` names a known broad-index symbol."""
    return underlying.upper() in KNOWN_INDEX_SYMBOLS


def validate_instrument(meta: InstrumentMeta, strict: bool) -> None:
    """Validate an instrument against Laoer guardrails (REQ-CORE-001-R4).

    Hard data error first: if both defaults are unresolved the function raises
    :class:`UnresolvedParameterError` regardless of ``strict``. Otherwise low
    leverage (``< MIN_LEVERAGE``) and non-index underlyings block when ``strict``
    is true and warn (``UserWarning``) when it is false.
    """
    if meta.default_target_pct is None and meta.default_band is None:
        raise UnresolvedParameterError(
            f"target_pct/band unresolved for ticker {meta.ticker!r}: "
            "no instrument default and no explicit value"
        )

    if meta.leverage < MIN_LEVERAGE:
        if strict:
            raise ValueError(_LEVERAGE_WARNING)
        warnings.warn(_LEVERAGE_WARNING, UserWarning, stacklevel=2)

    if not is_index_underlying(meta.underlying):
        if strict:
            raise ValueError(_NON_INDEX_WARNING)
        warnings.warn(_NON_INDEX_WARNING, UserWarning, stacklevel=2)


class InstrumentRegistry:
    """Resolves tickers and parameters from a validated :class:`Config`."""

    def __init__(self, instruments: dict[str, InstrumentMeta]) -> None:
        self._instruments = dict(instruments)

    @classmethod
    def from_config(cls, config: Config) -> InstrumentRegistry:
        """Build a registry from the ``instruments:`` block of a ``Config``."""
        instruments = {
            ticker: InstrumentMeta(
                ticker=ticker,
                leverage=spec.leverage,
                underlying=spec.underlying,
                default_target_pct=spec.default_target_pct,
                default_band=spec.default_band,
            )
            for ticker, spec in config.instruments.items()
        }
        return cls(instruments)

    def resolve(self, ticker: str) -> InstrumentMeta:
        """Return the :class:`InstrumentMeta` for ``ticker`` or raise."""
        try:
            return self._instruments[ticker]
        except KeyError:
            raise UnknownTickerError(
                f"unknown ticker {ticker!r}: not present in the instrument registry"
            ) from None

    def resolve_target_pct(self, ticker: str, explicit: Decimal | None) -> Decimal:
        """Resolve ``target_pct``: explicit > instrument default > error."""
        meta = self.resolve(ticker)
        return self._resolve_chain(
            ticker=ticker,
            name="target_pct",
            explicit=explicit,
            default=meta.default_target_pct,
        )

    def resolve_band(self, ticker: str, explicit: Decimal | None) -> Decimal:
        """Resolve ``band``: explicit > instrument default > error."""
        meta = self.resolve(ticker)
        return self._resolve_chain(
            ticker=ticker,
            name="band",
            explicit=explicit,
            default=meta.default_band,
        )

    @staticmethod
    def _resolve_chain(
        ticker: str,
        name: str,
        explicit: Decimal | None,
        default: Decimal | None,
    ) -> Decimal:
        """First non-``None`` of explicit/default wins; otherwise raise."""
        if explicit is not None:
            return explicit
        if default is not None:
            return default
        raise UnresolvedParameterError(
            f"{name} unresolved for ticker {ticker!r}: "
            "no explicit strategy value and no instrument default"
        )
