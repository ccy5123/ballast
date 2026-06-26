"""Typed exceptions for the Toss adapter (REQ-ADAPTER-001-R3).

@CODE:SPEC-ADAPTER-001

Errors carry the broker's ``code`` / ``message`` / ``requestId`` (FD1) so callers
map on a stable identifier. No secret or token is ever placed on an exception.
"""

from __future__ import annotations


class TossError(Exception):
    """Base class for every Toss adapter error."""


class TossAuthError(TossError):
    """Authentication failure.

    Raised for OAuth2 token-endpoint errors (``OAuth2ErrorResponse``) and for
    ``401`` / ``code in {expired-token, invalid-token}`` on authenticated calls.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        request_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status_code = status_code
        super().__init__(f"{code}: {message}".rstrip(": "))


class TossRateLimitError(TossError):
    """Rate-limit failure (``429`` / ``code = rate-limit-exceeded``).

    Surfaces ``Retry-After`` and the ``X-RateLimit-*`` headers (FD8) so callers
    can pace retries.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        request_id: str | None = None,
        retry_after: int | None = None,
        limit: int | None = None,
        remaining: int | None = None,
        reset: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining
        self.reset = reset
        super().__init__(f"{code}: {message}".rstrip(": "))


class TossApiError(TossError):
    """Any other domain/transport error mapped from an ``ErrorResponse``.

    Covers ``account-header-required``, ``account-not-found``, ``stock-not-found``,
    ``exchange-rate-not-found``, ``forbidden``, ``internal-error``, ``maintenance``,
    timeouts, and unknown codes.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        request_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status_code = status_code
        super().__init__(f"{code}: {message}".rstrip(": "))
