"""Environment-driven bootstrap for the Toss adapter.

Reads the OAuth2 client credentials from the process environment (NEVER from
source or version control) and assembles an authenticated :class:`TossClient`
plus the read-only account / market-data adapters. The actual secret values live
only in the environment (e.g. a git-ignored ``.env`` exported into the process);
this module handles variable NAMES, never embeds values, and relies on the
adapter layer's redacted ``repr`` so secrets never reach logs.

Required environment variables:

* ``TOSS_CLIENT_ID`` — OAuth2 client id.
* ``TOSS_CLIENT_SECRET`` — OAuth2 client secret.

Optional:

* ``TOSS_BASE_URL`` — API base URL (defaults to the public Toss Open API host).

A missing required variable raises :class:`MissingCredentialsError`, whose
message names the absent variables only — it never echoes any value.

@CODE:SPEC-ADAPTER-001
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from ballast.adapters.errors import TossError
from ballast.adapters.toss.account import TossAccountAdapter
from ballast.adapters.toss.auth import TokenManager
from ballast.adapters.toss.client import DEFAULT_BASE_URL, TossClient
from ballast.adapters.toss.marketdata import TossMarketDataAdapter

ENV_CLIENT_ID = "TOSS_CLIENT_ID"
ENV_CLIENT_SECRET = "TOSS_CLIENT_SECRET"
ENV_BASE_URL = "TOSS_BASE_URL"

_REQUIRED = (ENV_CLIENT_ID, ENV_CLIENT_SECRET)


class MissingCredentialsError(TossError):
    """Raised when a required Toss credential variable is absent from the env.

    The message names the missing variables only; it never contains a value.
    """


@dataclass(frozen=True, slots=True)
class TossAdapters:
    """The assembled, authenticated Toss surface (read-only adapters)."""

    client: TossClient
    account: TossAccountAdapter
    marketdata: TossMarketDataAdapter


def from_env(
    env: Mapping[str, str] | None = None,
    *,
    http: httpx.Client | None = None,
) -> TossAdapters:
    """Build the authenticated Toss adapters from environment variables.

    ``env`` defaults to :data:`os.environ`. When ``http`` is omitted a new
    :class:`httpx.Client` bound to the base URL is created; the caller owns it and
    is responsible for closing it (e.g. via the client's context manager) at
    shutdown. No network call is made here — the token is fetched lazily on the
    first authenticated request.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    missing = [name for name in _REQUIRED if not source.get(name)]
    if missing:
        raise MissingCredentialsError(
            "missing required Toss credential environment variable(s): " + ", ".join(missing)
        )

    client_id = source[ENV_CLIENT_ID]
    client_secret = source[ENV_CLIENT_SECRET]
    base_url = source.get(ENV_BASE_URL) or DEFAULT_BASE_URL

    transport = http if http is not None else httpx.Client(base_url=base_url)
    tokens = TokenManager(http=transport, client_id=client_id, client_secret=client_secret)
    client = TossClient(http=transport, token_manager=tokens)
    return TossAdapters(
        client=client,
        account=TossAccountAdapter(client),
        marketdata=TossMarketDataAdapter(client),
    )
