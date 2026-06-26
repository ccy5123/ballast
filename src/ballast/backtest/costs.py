"""Pure, Decimal-only cost model: commission, capital-gains tax, FX (R3).

Every function here is pure and operates entirely on :class:`~decimal.Decimal`
(no ``float`` on the money path). It models:

* a per-trade commission (default ``0.00``, Toss US-stock commission-free),
* a per-lot realized gain ``(sell_price - avg_cost) * qty - commission``, and
* an annual capital-gains tax of 22% on the net realized USD gain *after* a
  2,500,000 KRW deduction converted to USD at the realization FX, where the
  deduction resets each calendar year and a non-positive net gain is never taxed.

@CODE:SPEC-BACKTEST-001
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ballast.core.models import quantize_money

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class CostModel:
    """Provisional, fully parameterized cost assumptions (TBD T7-bt / T8-bt / T9-bt).

    * ``commission_per_trade`` — flat per-fill commission (default ``0.00``).
    * ``tax_rate`` — capital-gains rate (default ``0.22``).
    * ``annual_deduction_krw`` — KRW deduction per calendar year (default 2.5M).
    * ``slippage_bps`` — execution slippage in basis points (default ``0``).
    """

    commission_per_trade: Decimal = Decimal("0.00")
    tax_rate: Decimal = Decimal("0.22")
    annual_deduction_krw: Decimal = Decimal("2500000")
    slippage_bps: Decimal = Decimal("0")

    def realized_gain_usd(
        self,
        *,
        sell_price: Decimal,
        avg_cost: Decimal,
        qty: Decimal,
        commission: Decimal,
    ) -> Decimal:
        """Per-lot realized USD gain: ``(sell_price - avg_cost) * qty - commission``."""
        gain = (sell_price - avg_cost) * qty - commission
        return quantize_money(gain)

    def annual_tax(self, year_net_gain_usd: Decimal, fx_at_realization: Decimal) -> Decimal:
        """Tax for one calendar year: ``max(0, gain - deduction_usd) * tax_rate``.

        The 2.5M KRW deduction is converted to USD at ``fx_at_realization``. A year
        whose net realized gain is at or below the deduction is taxed ``0.00`` (never
        negative). This function is per-call, so each calendar year receives a fresh
        deduction — unused deduction never carries forward.
        """
        deduction_usd = quantize_money(self.annual_deduction_krw / fx_at_realization)
        taxable_base = year_net_gain_usd - deduction_usd
        if taxable_base <= _ZERO:
            return quantize_money(_ZERO)
        return quantize_money(taxable_base * self.tax_rate)
