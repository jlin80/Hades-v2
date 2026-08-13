"""Provider failure reporting.

task.md §6 requires that when a provider fails we can answer: which provider,
which endpoint, what kind of error, what status code, which token, and how many
retries were spent. A bare exception string answers none of those.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderError(Exception):
    """A provider failed in a way that is fully attributable.

    Attributes:
        provider: Provider name, e.g. "geckoterminal".
        endpoint: The URL path that failed.
        error_type: Exception class name or a schema-failure label.
        message: Human-readable detail.
        status_code: HTTP status when the request completed, else None.
        retry_count: How many attempts were spent before giving up.
        token_address: The token being processed, when the failure is per-token.
    """

    provider: str
    endpoint: str
    error_type: str
    message: str
    status_code: int | None = None
    retry_count: int = 0
    token_address: str | None = None

    def __str__(self) -> str:
        return (
            f"{self.provider} {self.endpoint} failed: {self.error_type}: {self.message} "
            f"(status={self.status_code}, retries={self.retry_count})"
        )

    def as_log_fields(self) -> dict[str, Any]:
        """Return the structured fields to attach to a log event."""
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "error_type": self.error_type,
            "error": self.message,
            "status_code": self.status_code,
            "retry_count": self.retry_count,
            "token_address": self.token_address,
        }


class ProviderSchemaError(ProviderError):
    """A provider responded successfully but not in the shape we expect.

    This is separated from transport failures because it means the upstream
    contract changed. Retrying will not help, and continuing would mean storing
    data we no longer understand. Hades v1's honeypot check kept "working"
    against a dead endpoint precisely because nothing distinguished these cases.
    """
