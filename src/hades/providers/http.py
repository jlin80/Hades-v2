"""Shared HTTP behaviour for every provider (spec §6).

Timeout, limited retry, exponential backoff, rate-limit handling, structured
error logging. And one addition drawn from Hades V1: an **explicit**
``httpx.Limits``.

V1 had no connection limits anywhere in the codebase, so every client could
open 100 concurrent connections. Its worker then logged ConnectTimeouts against
providers that answered fine from a shell in the same container — the shape of
an exhausted pool, misread as four separate provider outages across three
sessions. The ceiling here is small and deliberate.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, Self

import httpx

from hades.providers.errors import ProviderRateLimitedError, ProviderUnavailableError

logger = logging.getLogger(__name__)


class ProviderHttpClient:
    """An httpx client with a retry policy and a name to log under."""

    def __init__(
        self,
        provider: str,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        backoff_base_seconds: float = 0.5,
        max_connections: int = 10,
        user_agent: str = "hades-v2/0.1",
    ) -> None:
        self.provider = provider
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        # Kept as an attribute rather than passed inline so the ceiling is
        # inspectable. "What is this client's connection limit?" was a question
        # V1 could not answer about itself while its pool was exhausting.
        self.limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max(1, max_connections // 2),
        )
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            limits=self.limits,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET and parse JSON, retrying transport and 5xx failures.

        Raises ProviderRateLimitedError on 429 and ProviderUnavailableError once the
        attempt budget is spent. Never returns a partial or invented result.
        """
        last: str = "no attempt made"

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self._client.get(path, params=params)
            except (httpx.HTTPError, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                self._log_attempt(path, attempt, last)
                await self._backoff(attempt)
                continue

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                logger.warning(
                    "provider_rate_limited",
                    extra={
                        "context": {
                            "provider": self.provider,
                            "path": path,
                            "retry_after_seconds": retry_after,
                        }
                    },
                )
                raise ProviderRateLimitedError(self.provider, f"429 on {path}", retry_after)

            if response.status_code >= 500:
                last = f"HTTP {response.status_code}"
                self._log_attempt(path, attempt, last)
                await self._backoff(attempt)
                continue

            if response.status_code != 200:
                # 4xx other than 429 will not fix itself by retrying.
                raise ProviderUnavailableError(
                    self.provider, f"HTTP {response.status_code} on {path}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise ProviderUnavailableError(
                    self.provider, f"200 on {path} but body is not JSON: {exc}"
                ) from exc

        raise ProviderUnavailableError(
            self.provider, f"{self.max_attempts} attempts exhausted on {path}: {last}"
        )

    def _log_attempt(self, path: str, attempt: int, reason: str) -> None:
        logger.warning(
            "provider_attempt_failed",
            extra={
                "context": {
                    "provider": self.provider,
                    "path": path,
                    "attempt": attempt,
                    "max_attempts": self.max_attempts,
                    "reason": reason,
                }
            },
        )

    async def _backoff(self, attempt: int) -> None:
        if attempt < self.max_attempts:
            await asyncio.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        # The header also permits an HTTP date. We do not parse it: reporting
        # "unknown" is better than reporting a number we guessed.
        return None
