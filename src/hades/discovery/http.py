"""Shared outbound HTTP client and bounded retry.

Every requirement in task.md §6 is handled here rather than in each provider:
timeout, rate limits, network errors, response validation, bounded retry, and
never blocking the system indefinitely.

Two v1 lessons are baked in:

* **Connection limits are set explicitly.** v1 never configured `httpx.Limits`
  anywhere and spent a long investigation blaming its RPC provider for
  connection churn that was its own doing.
* **URLs are never logged with their query string.** v1 leaked a Helius API key
  into container logs because httpx logs full URLs including query parameters,
  and that log stream was rendered in the dashboard.
"""

import asyncio
import random
from typing import Any

import httpx2

from hades.config.settings import Settings
from hades.discovery.errors import ProviderError, ProviderSchemaError
from hades.observability.logging import get_logger

logger = get_logger(__name__)

# Statuses worth another attempt: rate limiting and transient server faults.
# A 4xx other than 429 means the request itself is wrong; retrying just burns
# the rate limit budget.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Upper bound on a single backoff sleep, so a Retry-After of 900 cannot stall
# the discovery loop for fifteen minutes.
MAX_BACKOFF_SECONDS = 30.0


def create_http_client(settings: Settings) -> httpx2.AsyncClient:
    """Build the shared async HTTP client."""
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(settings.http_timeout_seconds),
        limits=httpx2.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive_connections,
        ),
        follow_redirects=True,
        headers={"User-Agent": "hades-v2/0.2 (solana data collection)"},
    )


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """Return how long to wait before the next attempt.

    Honours the server's ``Retry-After`` when it sends one, since ignoring it is
    the fastest way to get rate limited harder.
    """
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        except ValueError:
            # Retry-After may be an HTTP date. Fall through to exponential.
            pass
    # Exponential with jitter, so parallel callers do not retry in lockstep.
    base = min(2.0**attempt, MAX_BACKOFF_SECONDS)
    return base * (0.5 + random.random() / 2)  # noqa: S311 — jitter, not cryptography


async def get_json(
    client: httpx2.AsyncClient,
    *,
    provider: str,
    url: str,
    endpoint: str,
    max_retries: int,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET ``url`` and return parsed JSON, retrying a bounded number of times.

    Args:
        endpoint: Path used for logging and error attribution. Never the full
            URL, which may carry credentials in its query string.
        max_retries: Additional attempts after the first. 0 means a single try.

    Raises:
        ProviderError: transport failure, non-retryable status, or retries
            exhausted.
        ProviderSchemaError: the response was not JSON.
    """
    last_error: ProviderError | None = None

    for attempt in range(max_retries + 1):
        try:
            response = await client.get(url, params=params)
        except httpx2.TimeoutException as exc:
            last_error = ProviderError(
                provider=provider,
                endpoint=endpoint,
                error_type=type(exc).__name__,
                message=str(exc) or "request timed out",
                retry_count=attempt,
            )
        except httpx2.RequestError as exc:
            # DNS failure, connection refused, TLS error. This is the class that
            # silently killed v1 when Jupiter retired its host.
            last_error = ProviderError(
                provider=provider,
                endpoint=endpoint,
                error_type=type(exc).__name__,
                message=str(exc) or "transport error",
                retry_count=attempt,
            )
        else:
            if response.status_code in RETRYABLE_STATUSES:
                last_error = ProviderError(
                    provider=provider,
                    endpoint=endpoint,
                    error_type="RetryableStatus",
                    message=response.text[:200],
                    status_code=response.status_code,
                    retry_count=attempt,
                )
                if attempt < max_retries:
                    delay = _backoff_seconds(attempt, response.headers.get("Retry-After"))
                    logger.warning(
                        "provider_retrying",
                        delay_seconds=round(delay, 2),
                        **last_error.as_log_fields(),
                    )
                    await asyncio.sleep(delay)
                    continue
            elif response.is_error:
                # Not retryable. Fail immediately with full attribution.
                raise ProviderError(
                    provider=provider,
                    endpoint=endpoint,
                    error_type="HTTPStatusError",
                    message=response.text[:200],
                    status_code=response.status_code,
                    retry_count=attempt,
                )
            else:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderSchemaError(
                        provider=provider,
                        endpoint=endpoint,
                        error_type="InvalidJSON",
                        message=str(exc),
                        status_code=response.status_code,
                        retry_count=attempt,
                    ) from exc

        if attempt < max_retries:
            delay = _backoff_seconds(attempt, None)
            logger.warning(
                "provider_retrying",
                delay_seconds=round(delay, 2),
                **last_error.as_log_fields(),
            )
            await asyncio.sleep(delay)

    if last_error is not None:
        raise last_error

    # Only reachable if the loop body stops recording failures, which would be a
    # bug in this function rather than in the provider.
    raise ProviderError(
        provider=provider,
        endpoint=endpoint,
        error_type="RetryLoopExhaustedWithoutError",
        message="retry loop completed without producing a result or an error",
        retry_count=max_retries,
    )
