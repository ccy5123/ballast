#!/usr/bin/env python
"""Read-only smoke test for the live Toss Open API connection.

Loads credentials from the environment (or a local ``.env`` in the current
directory) and exercises a few READ-ONLY endpoints to confirm the adapter is
wired correctly: OAuth2 token issuance (implicit on the first call), an exchange
rate, the US market calendar, and the account list. It places NO orders and
mutates nothing.

Sensitive values are never printed: secrets stay in the environment and account
numbers are masked to their last four digits.

Usage::

    # with a local .env (auto-loaded), from the repo root:
    python scripts/toss_smoke.py

    # or with the variables already exported:
    set -a && . ./.env && set +a && python scripts/toss_smoke.py

Credential mapping reminder (see .env.example): the Toss console "Secret Key"
(``tsck_…``) is ``TOSS_CLIENT_ID`` and the "API Key" (``tssk_…``) is
``TOSS_CLIENT_SECRET``. Swapping them yields ``401 invalid_client``.

Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

from ballast.adapters.errors import TossError
from ballast.adapters.models import Currency
from ballast.adapters.toss import MissingCredentialsError, from_env


def load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ`` (no overwrite).

    A minimal, dependency-free parser: blank lines and ``#`` comments are
    skipped, surrounding quotes are stripped, and an already-set variable is left
    untouched so an explicit export always wins over the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def mask(value: str) -> str:
    """Mask all but the last four characters of a sensitive identifier."""
    return value if len(value) <= 4 else "*" * (len(value) - 4) + value[-4:]


def main() -> int:
    """Run the read-only checks and return a process exit code."""
    load_dotenv(Path.cwd() / ".env")

    try:
        adapters = from_env()
    except MissingCredentialsError as exc:
        print(f"[FAIL] credentials not configured: {exc}")
        print("       Copy .env.example to .env and fill in your Toss keys.")
        return 1

    checks: list[tuple[str, object]] = []
    try:
        rate = adapters.marketdata.get_exchange_rate(Currency.USD, Currency.KRW)
        checks.append(("exchange rate USD->KRW", rate))

        calendar = adapters.marketdata.get_market_calendar("US")
        checks.append(("US market calendar (today)", calendar.today))

        accounts = adapters.account.list_accounts()
        summary = [
            f"{mask(a.account_no)} (seq={a.account_seq}, {a.account_type})" for a in accounts
        ]
        checks.append((f"accounts [{len(accounts)}]", summary))
    except TossError as exc:
        print(f"[FAIL] Toss API error: {type(exc).__name__}: {exc}")
        return 1
    except (httpx.HTTPError, httpx.TransportError) as exc:
        print(f"[FAIL] network error: {type(exc).__name__}: {exc}")
        return 1

    for label, value in checks:
        print(f"[OK]  {label}: {value}")
    print("\nLive Toss connection verified (read-only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
