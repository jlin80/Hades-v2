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


class MarketSnapshot(BaseModel):
    """One observation of a token's market state.

    Two kinds of field live here, and the distinction is the point:

    * **Raw curve state** — the reserves, exactly as the provider reported them.
      These are the primary record. Everything below is a function of them.
    * **Derived values** — price, market cap, liquidity. Stored because queries
      need them, but reproducible from the raw fields with the formulas in
      ``hades.tracking.derive``, so a formula error found later is fixable
      against data already collected instead of poisoning it permanently.

    Anything the provider does not supply is ``None``. Spec §9: never invent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_address: str
    source: str

    # Provenance (spec §9). Three timestamps, because they answer different
    # questions: how old the provider says its data is, when we got it, and
    # when it landed. The gap between the first two is what stale detection
    # reads; the gap between the last two is our own latency.
    provider_updated_at: datetime | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    received_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    # Raw bonding-curve state, in base units exactly as reported.
    virtual_sol_reserves: int | None = None
    virtual_token_reserves: int | None = None
    real_sol_reserves: int | None = None
    real_token_reserves: int | None = None
    total_supply: int | None = None
    base_decimals: int | None = None
    quote_decimals: int | None = None

    # Derived. See hades.tracking.derive.
    price_sol: float | None = None
    market_cap_sol: float | None = None
    liquidity_sol: float | None = None
    market_cap_usd: float | None = None
    sol_price_usd: float | None = None

    # Reported directly by the provider.
    is_complete: bool | None = None
    last_trade_at: datetime | None = None
    reply_count: int | None = None

    # Present in spec §9 and deliberately absent from every free source we
    # measured. Kept in the schema as NULL rather than dropped, so the gap is
    # visible in the data instead of being an unexplained missing column.
    #   volume, buy_volume, sell_volume, buy_count, sell_count,
    #   transaction_count, unique_buyers, unique_sellers, holder_count
    # bonding_curve_progress is also absent, for a different reason -- see
    # docs/DATA_SOURCES.md. Its curve constants are not established for the
    # current pump.fun program variant, and a wrong progress figure would
    # silently poison a feature. The raw reserves make it computable later.

    @field_validator("provider_updated_at", "observed_at", "received_at", "last_trade_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @property
    def provider_data_age_seconds(self) -> float | None:
        """How stale the provider said its own data was when we read it."""
        if self.provider_updated_at is None:
            return None
        return (self.observed_at - self.provider_updated_at).total_seconds()
