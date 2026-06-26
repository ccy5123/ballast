"""OAuth2 client-credentials TokenManager (REQ-ADAPTER-001-R2).

@CODE:SPEC-ADAPTER-001

Issues and caches an ``access_token`` via ``POST /oauth2/token`` (form-urlencoded,
no ``Authorization`` header), refreshing it before expiry using a safety margin.
The clock is injected so expiry/refresh is deterministic and testable. Secrets
and the token are never logged or exposed via ``repr`` (FD2, secrets policy).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from ballast.adapters.errors import TossAuthError

logger = logging.getLogger("ballast.adapters")

# Refresh this many seconds before the advertised expiry to avoid mid-flight
# expiry races.
DEFAULT_SAFETY_MARGIN = 60


def _utcnow() -> datetime:
    """Wall-clock now in UTC. Injected by callers; never read in the pure core."""
    return datetime.now(tz=UTC)


class TokenManager:
    """Caches and refreshes an OAuth2 client-credentials access token."""

    def __init__(
        self,
        *,
        http: httpx.Client,
        client_id: str,
        client_secret: str,
        now: Callable[[], datetime] = _utcnow,
        safety_margin: int = DEFAULT_SAFETY_MARGIN,
        token_path: str = "/oauth2/token",
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._now = now
        self._safety_margin = safety_margin
        self._token_path = token_path
        self._access_token: str | None = None
        self._expires_at: datetime | None = None

    def get_token(self) -> str:
        """Return a valid cached token, refreshing first if needed."""
        if self._needs_refresh():
            return self.refresh()
        # _access_token is non-None: a cached token that is not near expiry.
        assert self._access_token is not None
        return self._access_token

    def refresh(self) -> str:
        """Force a token refresh and return the new access token."""
        response = self._http.post(
            self._token_path,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if response.status_code != 200:
            self._raise_oauth_error(response)

        body = response.json()
        access_token = str(body["access_token"])
        expires_in = int(body["expires_in"])
        self._access_token = access_token
        self._expires_at = self._now() + timedelta(seconds=expires_in)
        # Never log the token itself; only its lifetime for traceability.
        logger.debug("issued access token (expires_in=%ss)", expires_in)
        return access_token

    def _needs_refresh(self) -> bool:
        """True when no token is cached or it is within the safety margin."""
        if self._expires_at is None:
            return True
        threshold = self._expires_at - timedelta(seconds=self._safety_margin)
        return self._now() >= threshold

    def _raise_oauth_error(self, response: httpx.Response) -> None:
        """Map an ``OAuth2ErrorResponse`` body to ``TossAuthError``."""
        code = "invalid_client"
        message = ""
        try:
            body = response.json()
            code = str(body.get("error", code))
            message = str(body.get("error_description", ""))
        except (ValueError, KeyError):  # pragma: no cover - defensive
            pass
        logger.error("oauth2 token error (status=%s code=%s)", response.status_code, code)
        raise TossAuthError(code, message, status_code=response.status_code)

    def __repr__(self) -> str:
        """Redacted repr: never expose the client secret or token."""
        state = "cached" if self._access_token is not None else "empty"
        return f"TokenManager(client_id=***, secret=***, token={state})"
