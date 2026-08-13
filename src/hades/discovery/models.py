"""Domain models for token discovery.

These are what a provider returns after normalisation, before validation and
persistence. They are intentionally separate from the ORM model: a provider
response is not a database row, and conflating the two is how a schema change
upstream silently becomes a schema change downstream.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class DiscoveredToken:
    """A token observed by a provider.

    Attributes:
        token_address: The mint address on Solana. The identity of the token.
        symbol: Ticker, when the provider supplies one.
        name: Human-readable name, when the provider supplies one.
        pool_address: The pool the token was observed in, when known.
        first_seen_at: When the pool was created, per the provider. This is the
            closest thing to the token's real age. None when not supplied —
            never guessed.
        observed_at: When our process received the response carrying this token.
        provider_name: Which provider produced this observation.
        raw: The provider's own object for this token, stored verbatim so that
            an upstream schema change can be detected after the fact rather than
            silently discarded.
    """

    token_address: str
    symbol: str | None
    name: str | None
    pool_address: str | None
    first_seen_at: datetime | None
    observed_at: datetime
    provider_name: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    """The measured outcome of one discovery cycle."""

    provider_name: str | None
    started_at: datetime
    finished_at: datetime
    fetched: int
    valid: int
    rejected: int
    inserted: int
    duplicates: int
    error: str | None

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000.0

    @property
    def succeeded(self) -> bool:
        return self.error is None
