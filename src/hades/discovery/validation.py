"""Explicit validation of discovered tokens (task.md §10).

We do not collect garbage. A token that fails any check is rejected, and every
rejection is recorded with a machine-readable reason so Phase 4 can report on
data quality instead of guessing at it.

Nothing here is silent, and nothing is repaired. A malformed address is not
trimmed into a valid-looking one; it is rejected.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from hades.clock import utc_now
from hades.discovery.models import DiscoveredToken

# Solana addresses are base58: 32-44 characters, excluding 0, O, I and l.
_BASE58_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

MAX_SYMBOL_LENGTH = 64
MAX_NAME_LENGTH = 256

# A pool cannot have been created in the future. A small tolerance absorbs clock
# skew between us and the provider; beyond it, the data is wrong.
FUTURE_TOLERANCE_SECONDS = 300.0


class RejectionReason(StrEnum):
    """Why a discovered token was refused."""

    INVALID_ADDRESS = "invalid_address"
    MISSING_ADDRESS = "missing_address"
    SYMBOL_TOO_LONG = "symbol_too_long"
    NAME_TOO_LONG = "name_too_long"
    TIMESTAMP_IN_FUTURE = "timestamp_in_future"
    TIMESTAMP_NOT_UTC = "timestamp_not_utc"


@dataclass(frozen=True, slots=True)
class Rejection:
    """A rejected token and the reason, kept for reporting."""

    token_address: str
    provider_name: str
    reason: RejectionReason
    detail: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of validating one batch."""

    valid: list[DiscoveredToken]
    rejections: list[Rejection]


def validate_token(token: DiscoveredToken) -> Rejection | None:
    """Return a Rejection if the token is unusable, or None if it is sound."""
    address = token.token_address

    if not address:
        return Rejection(
            token_address="",
            provider_name=token.provider_name,
            reason=RejectionReason.MISSING_ADDRESS,
            detail="provider returned an empty token address",
        )

    if not _BASE58_ADDRESS.match(address):
        return Rejection(
            token_address=address,
            provider_name=token.provider_name,
            reason=RejectionReason.INVALID_ADDRESS,
            detail=f"not a base58 Solana address of length 32-44 (length {len(address)})",
        )

    if token.symbol is not None and len(token.symbol) > MAX_SYMBOL_LENGTH:
        return Rejection(
            token_address=address,
            provider_name=token.provider_name,
            reason=RejectionReason.SYMBOL_TOO_LONG,
            detail=f"symbol is {len(token.symbol)} characters, limit {MAX_SYMBOL_LENGTH}",
        )

    if token.name is not None and len(token.name) > MAX_NAME_LENGTH:
        return Rejection(
            token_address=address,
            provider_name=token.provider_name,
            reason=RejectionReason.NAME_TOO_LONG,
            detail=f"name is {len(token.name)} characters, limit {MAX_NAME_LENGTH}",
        )

    if token.first_seen_at is not None:
        if token.first_seen_at.tzinfo is None:
            return Rejection(
                token_address=address,
                provider_name=token.provider_name,
                reason=RejectionReason.TIMESTAMP_NOT_UTC,
                detail="first_seen_at is timezone-naive",
            )
        drift = (token.first_seen_at - utc_now()).total_seconds()
        if drift > FUTURE_TOLERANCE_SECONDS:
            return Rejection(
                token_address=address,
                provider_name=token.provider_name,
                reason=RejectionReason.TIMESTAMP_IN_FUTURE,
                detail=f"first_seen_at is {drift:.0f}s in the future",
            )

    return None


def validate_batch(tokens: list[DiscoveredToken]) -> ValidationResult:
    """Split a batch into sound tokens and recorded rejections.

    Duplicates *within* the batch are collapsed here, keeping the first
    occurrence. Providers do return the same token twice across pools, and
    letting that reach the database would rely on the unique constraint to
    absorb work we can cheaply avoid.
    """
    valid: list[DiscoveredToken] = []
    rejections: list[Rejection] = []
    seen: set[str] = set()

    for token in tokens:
        rejection = validate_token(token)
        if rejection is not None:
            rejections.append(rejection)
            continue
        if token.token_address in seen:
            continue
        seen.add(token.token_address)
        valid.append(token)

    return ValidationResult(valid=valid, rejections=rejections)
