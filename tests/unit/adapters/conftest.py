"""Shared fixtures for the Toss adapter test suite.

@TEST:SPEC-ADAPTER-001

All HTTP is mocked (``httpx.MockTransport``); no live network is touched. A
controllable clock is injected so token expiry/refresh is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest


@pytest.fixture
def clock() -> Callable[[], datetime]:
    """Return a frozen, advanceable clock callable.

    The returned callable yields the current fake "now"; tests advance time via
    the attached ``advance`` helper. Time stays out of the pure paths and is
    injected here at the IO edge.
    """

    state = {"now": datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)}

    def now() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        from datetime import timedelta

        state["now"] = state["now"] + timedelta(seconds=seconds)

    now.advance = advance  # type: ignore[attr-defined]
    return now


@pytest.fixture
def token_response() -> dict[str, object]:
    """A representative OAuth2 token response (OAuth2TokenResponse schema)."""
    return {"access_token": "jwt-abc", "token_type": "Bearer", "expires_in": 3600}
