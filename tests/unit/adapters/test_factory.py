"""Tests for the environment-driven Toss adapter bootstrap.

Verifies that ``from_env`` reads credentials from the environment (never source),
assembles the authenticated adapters, defaults/overrides the base URL, raises a
value-free error naming missing variables, and never leaks a secret through
``repr``. No network call is made (the token is fetched lazily).

@TEST:SPEC-ADAPTER-001
"""

from __future__ import annotations

import httpx
import pytest

from ballast.adapters.toss.account import TossAccountAdapter
from ballast.adapters.toss.client import DEFAULT_BASE_URL, TossClient
from ballast.adapters.toss.factory import (
    ENV_BASE_URL,
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    MissingCredentialsError,
    TossAdapters,
    from_env,
)
from ballast.adapters.toss.marketdata import TossMarketDataAdapter

_FAKE_ID = "fake-client-id"
_FAKE_SECRET = "fake-client-secret-value"


def _full_env(**overrides: str) -> dict[str, str]:
    env = {ENV_CLIENT_ID: _FAKE_ID, ENV_CLIENT_SECRET: _FAKE_SECRET}
    env.update(overrides)
    return env


def test_from_env_builds_authenticated_adapters() -> None:
    with httpx.Client(base_url=DEFAULT_BASE_URL) as http:
        adapters = from_env(_full_env(), http=http)
    assert isinstance(adapters, TossAdapters)
    assert isinstance(adapters.client, TossClient)
    assert isinstance(adapters.account, TossAccountAdapter)
    assert isinstance(adapters.marketdata, TossMarketDataAdapter)


def test_from_env_defaults_base_url_when_absent() -> None:
    # No TOSS_BASE_URL → the created client binds to the default host. Building
    # the adapters makes NO network call (token fetched lazily), so this is safe.
    adapters = from_env(_full_env())
    assert isinstance(adapters.client, TossClient)


def test_from_env_honors_base_url_override() -> None:
    with httpx.Client() as http:
        adapters = from_env(_full_env(**{ENV_BASE_URL: "https://example.test"}), http=http)
    assert isinstance(adapters.client, TossClient)


@pytest.mark.parametrize(
    ("env", "expected_missing"),
    [
        ({ENV_CLIENT_SECRET: _FAKE_SECRET}, ENV_CLIENT_ID),
        ({ENV_CLIENT_ID: _FAKE_ID}, ENV_CLIENT_SECRET),
        ({}, ENV_CLIENT_ID),
        ({ENV_CLIENT_ID: "", ENV_CLIENT_SECRET: ""}, ENV_CLIENT_ID),
    ],
)
def test_from_env_raises_naming_missing_variables(
    env: dict[str, str], expected_missing: str
) -> None:
    with httpx.Client() as http, pytest.raises(MissingCredentialsError) as exc:
        from_env(env, http=http)
    assert expected_missing in str(exc.value)


def test_missing_error_names_variables_not_values() -> None:
    with httpx.Client() as http, pytest.raises(MissingCredentialsError) as exc:
        from_env({ENV_CLIENT_ID: _FAKE_ID}, http=http)  # secret missing
    message = str(exc.value)
    assert ENV_CLIENT_SECRET in message
    # The error names the absent variable, never echoes a provided value.
    assert _FAKE_ID not in message


def test_repr_does_not_leak_secret() -> None:
    with httpx.Client(base_url=DEFAULT_BASE_URL) as http:
        adapters = from_env(_full_env(), http=http)
    rendered = repr(adapters) + repr(adapters.client)
    assert _FAKE_SECRET not in rendered
    assert _FAKE_ID not in rendered
