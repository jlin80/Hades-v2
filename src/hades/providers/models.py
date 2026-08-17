"""Normalized provider types.

The boundary between "whatever a provider sent" and "what the rest of the
system may assume". Providers map into these; nothing downstream ever sees a
raw provider payload.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Solana addresses are base58: no 0, O, I or l.
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Plausibility window for a provider timestamp, used to catch unit confusion
# rather than to guess it. pump.fun sends `created_timestamp` in milliseconds
# and `updated_at` in seconds; a value read with the wrong unit lands decades
# away from now and must fail loudly instead of becoming a token aged 55 years.
_MIN_EPOCH_MS = 1_577_836_800_000  # 2020-01-01
_MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01


def epoch_ms_to_datetime(value: float, *, field: str) -> datetime:
    """Convert a millisecond epoch to an aware UTC datetime, or refuse."""
    if not _MIN_EPOCH_MS <= value <= _MAX_EPOCH_MS:
        msg = (
            f"{field}={value!r} is not a plausible millisecond epoch "
            f"(expected {_MIN_EPOCH_MS}..{_MAX_EPOCH_MS}); wrong unit?"
        )
        raise ValueError(msg)
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class DiscoveredToken(BaseModel):
    """A token some provider told us exists.

    ``created_at`` is optional because our fastest discovery path does not
    carry it: PumpPortal's creation event has no timestamp field. Leaving it
    None and enriching from the primary is honest; stamping arrival time as
    creation time would silently zero out the discovery latency we are trying
    to measure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_address: str
    symbol: str | None = None
    name: str | None = None
    creator_address: str | None = None
    created_at: datetime | None = None
    source: str
    # Stamped when the provider parsed the frame, not when the row is written.
    # Measured consequence of getting this wrong: an enrichment round-trip
    # between arrival and insert added ~2.5s to every latency reading, so the
    # number meant "push latency plus one HTTP call" while claiming to mean
    # "push latency".
    observed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    # On-chain proof of the claim — the creation tx signature where we have it.
    # Lets any row in the dataset be verified independently of us.
    raw_provider_reference: str | None = None

    @field_validator("token_address")
    @classmethod
    def _validate_mint(cls, value: str) -> str:
        if not BASE58.match(value):
            msg = f"token_address {value!r} is not a base58 Solana address"
            raise ValueError(msg)
        return value

    @field_validator("creator_address")
    @classmethod
    def _validate_creator(cls, value: str | None) -> str | None:
        if value is not None and not BASE58.match(value):
            # Not fatal: a bad creator address costs us one attribute, while
            # rejecting the token would cost us the observation entirely.
            return None
        return value

    @field_validator("created_at", "observed_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @field_validator("symbol", "name")
    @classmethod
    def _blank_is_missing(cls, value: str | None) -> str | None:
        """An empty string is a missing value, and must be stored as one."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None
