"""Public surface of the ballast adapters (IO boundary) layer.

@CODE:SPEC-ADAPTER-001

The broker-agnostic ports, immutable adapter DTOs, typed errors, and the Toss
implementation (token manager, transport, market-data/account adapters). Money
is :class:`~decimal.Decimal` at this boundary; the pure core never imports this
package.
"""

from __future__ import annotations

from ballast.adapters.errors import (
    TossApiError,
    TossAuthError,
    TossError,
    TossRateLimitError,
)
from ballast.adapters.models import (
    Account,
    Commission,
    Currency,
    Holding,
    Holdings,
    MarketCalendar,
    OrderExecution,
    OrderRecord,
    OrdersPage,
    Quote,
)
from ballast.adapters.ports import BrokerAccountPort, MarketDataPort
from ballast.adapters.toss.account import TossAccountAdapter
from ballast.adapters.toss.auth import TokenManager
from ballast.adapters.toss.client import DEFAULT_BASE_URL, TossClient, to_decimal
from ballast.adapters.toss.factory import (
    ENV_BASE_URL,
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    MissingCredentialsError,
    TossAdapters,
    from_env,
)
from ballast.adapters.toss.marketdata import TossMarketDataAdapter

__all__ = [
    "DEFAULT_BASE_URL",
    "ENV_BASE_URL",
    "ENV_CLIENT_ID",
    "ENV_CLIENT_SECRET",
    "Account",
    "BrokerAccountPort",
    "Commission",
    "Currency",
    "Holding",
    "Holdings",
    "MarketCalendar",
    "MarketDataPort",
    "MissingCredentialsError",
    "OrderExecution",
    "OrderRecord",
    "OrdersPage",
    "Quote",
    "TokenManager",
    "TossAccountAdapter",
    "TossAdapters",
    "TossApiError",
    "TossAuthError",
    "TossClient",
    "TossError",
    "TossMarketDataAdapter",
    "TossRateLimitError",
    "from_env",
    "to_decimal",
]
