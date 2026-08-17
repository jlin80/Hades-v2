"""Provider failure types.

Distinct types because the caller reacts differently to each: a rate limit is
worth waiting out, an unavailable provider is worth failing over, and a schema
error means the contract changed and *nobody* should paper over it.

Spec §6: never ``except Exception: pass``.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base for every provider failure."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class ProviderUnavailableError(ProviderError):
    """Transport failed, or the provider answered with a server error."""


class ProviderRateLimitedError(ProviderError):
    """The provider asked us to slow down."""

    def __init__(self, provider: str, message: str, retry_after_seconds: float | None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(provider, message)


class ProviderSchemaError(ProviderError):
    """The response did not match what we expect.

    Deliberately not retried and never swallowed. A provider that changed its
    schema will keep returning the new one, and a silent skip here would show
    up much later as an unexplained gap in the dataset.
    """
