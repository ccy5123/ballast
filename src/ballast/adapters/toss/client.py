"""httpx transport for the Toss adapter (REQ-ADAPTER-001-R3).

@CODE:SPEC-ADAPTER-001

Injects ``Authorization: Bearer`` (and ``X-Tossinvest-Account`` on account-scoped
calls; FD5), unwraps the ``{ "result": <data> }`` envelope (FD1), parses
``format: decimal`` strings to :class:`~decimal.Decimal` (FD3), maps errors to
typed exceptions, performs exactly one 401 re-auth+retry, and bounded
retry/backoff on transient failures honoring ``Retry-After``. Secrets/tokens
never appear in logs, error messages, or ``repr``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ballast.adapters.errors import TossApiError, TossAuthError, TossRateLimitError
from ballast.adapters.toss.auth import TokenManager

logger = logging.getLogger("ballast.adapters")

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"
MAX_ATTEMPTS = 3

# Error codes that indicate an expired/invalid token on an authenticated call.
_AUTH_CODES = frozenset({"expired-token", "invalid-token"})
# Domain codes that are safe to retry (paired with their transient HTTP statuses).
_TRANSIENT_CODES = frozenset({"rate-limit-exceeded", "internal-error", "maintenance"})
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def to_decimal(value: str | Decimal | None) -> Decimal | None:
    """Parse a ``format: decimal`` string to ``Decimal``; ``null`` -> ``None``.

    Rejects ``float`` so precision never silently leaks onto the money path
    (FD3, AC-12). A ``Decimal`` passes through unchanged.
    """
    if value is None:
        return None
    if isinstance(value, (bool, float)):
        raise TypeError("money/quantity must be a string or Decimal, not a float")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - malformed payload guard
        raise ValueError(f"invalid decimal string: {value!r}") from exc


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    """Parse an integer rate-limit header; return ``None`` when absent/invalid."""
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:  # pragma: no cover - defensive
        return None


class TossClient:
    """Authenticated, envelope-unwrapping HTTP client for Toss endpoints."""

    def __init__(
        self,
        *,
        http: httpx.Client,
        token_manager: TokenManager,
        backoff_base: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._tokens = token_manager
        self._backoff_base = backoff_base
        self._sleep = sleep

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        account_seq: str | None = None,
    ) -> Any:
        """GET ``path`` and return the unwrapped ``result`` payload.

        Handles auth-header injection, exactly one 401 re-auth+retry, bounded
        retry/backoff on transient failures, and typed-error mapping.
        """
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        reauthed = False
        attempt = 0

        while True:
            attempt += 1
            headers = self._build_headers(account_seq)
            try:
                response = self._http.get(path, params=clean_params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < MAX_ATTEMPTS:
                    self._backoff(attempt, retry_after=None)
                    continue
                logger.error("request failed after %s attempts (path=%s)", attempt, path)
                raise TossApiError("timeout", str(exc)) from exc

            if response.status_code == 200:
                return self._unwrap(response)

            error = self._parse_error(response)

            # Exactly one re-auth on a 401 for an authenticated call.
            if self._is_auth_failure(response.status_code, error.code) and not reauthed:
                reauthed = True
                attempt -= 1  # the re-auth retry does not consume a transient attempt
                self._tokens.refresh()
                logger.debug("re-authenticated after 401 (requestId=%s)", error.request_id)
                continue

            # Bounded retry/backoff on transient failures.
            if self._is_transient(response.status_code, error.code) and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, retry_after=error.retry_after)
                continue

            raise self._to_exception(response.status_code, error)

    def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        account_seq: str | None = None,
    ) -> Any:
        """POST ``path`` with ``json`` and return the unwrapped ``result`` payload.

        Mirrors :meth:`get` exactly: same auth-header injection, ``{ "result" }``
        unwrap, exactly one 401 re-auth+retry, bounded retry/backoff honoring
        ``Retry-After`` on transient failures, and typed-error mapping. Only the
        HTTP verb and the request body differ; no new transport semantics.
        """
        body = dict(json) if json is not None else None
        reauthed = False
        attempt = 0

        while True:
            attempt += 1
            headers = self._build_headers(account_seq)
            try:
                response = self._http.post(path, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < MAX_ATTEMPTS:
                    self._backoff(attempt, retry_after=None)
                    continue
                logger.error("request failed after %s attempts (path=%s)", attempt, path)
                raise TossApiError("timeout", str(exc)) from exc

            if response.status_code == 200:
                return self._unwrap(response)

            error = self._parse_error(response)

            # Exactly one re-auth on a 401 for an authenticated call.
            if self._is_auth_failure(response.status_code, error.code) and not reauthed:
                reauthed = True
                attempt -= 1  # the re-auth retry does not consume a transient attempt
                self._tokens.refresh()
                logger.debug("re-authenticated after 401 (requestId=%s)", error.request_id)
                continue

            # Bounded retry/backoff on transient failures.
            if self._is_transient(response.status_code, error.code) and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, retry_after=error.retry_after)
                continue

            raise self._to_exception(response.status_code, error)

    def _build_headers(self, account_seq: str | None) -> dict[str, str]:
        """Build per-request headers: Bearer always, account header when scoped."""
        headers = {"Authorization": f"Bearer {self._tokens.get_token()}"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = account_seq
        return headers

    def _unwrap(self, response: httpx.Response) -> Any:
        """Return the ``result`` payload from a success envelope (FD1)."""
        body = response.json()
        return body["result"]

    def _backoff(self, attempt: int, *, retry_after: int | None) -> None:
        """Sleep before the next attempt, honoring ``Retry-After`` when present."""
        delay = (
            float(retry_after)
            if retry_after is not None
            else self._backoff_base * (2 ** (attempt - 1))
        )
        if delay > 0:
            self._sleep(delay)

    @staticmethod
    def _is_auth_failure(status_code: int, code: str) -> bool:
        return status_code == 401 or code in _AUTH_CODES

    @staticmethod
    def _is_transient(status_code: int, code: str) -> bool:
        return status_code in _TRANSIENT_STATUS or code in _TRANSIENT_CODES

    def _parse_error(self, response: httpx.Response) -> _ParsedError:
        """Extract ``error.{code, message, requestId}`` from an error envelope."""
        code = ""
        message = ""
        request_id: str | None = None
        try:
            body = response.json()
            err = body.get("error", {})
            if isinstance(err, Mapping):
                code = str(err.get("code", ""))
                message = str(err.get("message", ""))
                request_id = err.get("requestId")
        except ValueError:  # pragma: no cover - non-JSON error body guard
            pass
        if not code:
            code = f"http-{response.status_code}"
        logger.error(
            "toss error (status=%s code=%s requestId=%s)",
            response.status_code,
            code,
            request_id,
        )
        return _ParsedError(
            code=code,
            message=message,
            request_id=request_id,
            retry_after=_int_header(response.headers, "Retry-After"),
            limit=_int_header(response.headers, "X-RateLimit-Limit"),
            remaining=_int_header(response.headers, "X-RateLimit-Remaining"),
            reset=_int_header(response.headers, "X-RateLimit-Reset"),
        )

    def _to_exception(self, status_code: int, error: _ParsedError) -> Exception:
        """Map a parsed error to the appropriate typed exception."""
        if status_code == 401 or error.code in _AUTH_CODES:
            return TossAuthError(
                error.code,
                error.message,
                request_id=error.request_id,
                status_code=status_code,
            )
        if status_code == 429 or error.code == "rate-limit-exceeded":
            return TossRateLimitError(
                error.code,
                error.message,
                request_id=error.request_id,
                retry_after=error.retry_after,
                limit=error.limit,
                remaining=error.remaining,
                reset=error.reset,
            )
        return TossApiError(
            error.code,
            error.message,
            request_id=error.request_id,
            status_code=status_code,
        )

    def __repr__(self) -> str:
        """Redacted repr: never expose the token manager's secrets."""
        return "TossClient(base_url=***, token_manager=***)"


class _ParsedError:
    """Internal carrier for a parsed error envelope plus rate-limit headers."""

    __slots__ = ("code", "limit", "message", "remaining", "request_id", "reset", "retry_after")

    def __init__(
        self,
        *,
        code: str,
        message: str,
        request_id: str | None,
        retry_after: int | None,
        limit: int | None,
        remaining: int | None,
        reset: int | None,
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining
        self.reset = reset
