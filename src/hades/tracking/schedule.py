"""How often to snapshot a token, as a function of its age.

Pure: no I/O, no clock of its own. Everything here can be reasoned about and
tested directly, which matters because this is the function that decides what
the research dataset's time resolution is.

Spec §8 asks for four tiers, with the exact values configurable. It also says
"no unnecessary polling of thousands of tokens simultaneously; implement limits
and prioritisation" — and the arithmetic below is why that sentence is the
load-bearing one.

## The capacity problem, in numbers

Measured (``docs/DATA_SOURCES.md``): the primary sustains **1.64 req/s**, and
Pump.fun creates **0.24 to 0.55 tokens/s** (~21k-48k/day).

The default schedule costs **434 snapshots per token per 24 hours**. Tracking
every token would therefore need:

    0.24 tokens/s  ->  104 req/s   (64x over capacity)
    0.40 tokens/s  ->  174 req/s  (106x over)
    0.55 tokens/s  ->  239 req/s  (146x over)

So tracking the whole universe is not a tuning problem, it is impossible by two
orders of magnitude. What a 1 req/s budget actually buys:

    full 24h schedule (434 snaps/token)  ->   ~199 tokens/day
    first hour        (110 snaps/token)  ->   ~785 tokens/day
    early window only  (30 snaps/token)  -> ~2,880 tokens/day

The defaults here take the middle row: **track the first hour**. That covers
every outcome horizon spec §15 asks for (return_1m through return_1h) while
leaving a dataset of ~785 tokens/day, which is ~24k observations a month.

Tracking past one hour is a deliberate later decision, not an oversight — it
costs 4x per token and buys outcome horizons nothing in §15 currently asks for.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TrackingTier(enum.StrEnum):
    """Which age band a token is in. Stored on each snapshot.

    Recorded rather than recomputed later because the tier is what determines
    the sampling interval, and a dataset whose sampling rate cannot be
    reconstructed is a dataset whose velocity features cannot be trusted.
    """

    EARLY = "EARLY"
    MEDIUM = "MEDIUM"
    NORMAL = "NORMAL"
    LONG = "LONG"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class TrackingSchedule:
    """Tier boundaries and their sampling intervals, all in seconds."""

    early_until_seconds: float = 300.0
    early_interval_seconds: float = 10.0

    medium_until_seconds: float = 1800.0
    medium_interval_seconds: float = 30.0

    normal_until_seconds: float = 7200.0
    normal_interval_seconds: float = 60.0

    long_interval_seconds: float = 300.0

    # When a token stops being tracked. One hour by default — see the module
    # docstring: it is what the measured request budget affords while still
    # covering every outcome horizon the spec asks for.
    retire_after_seconds: float = 3600.0

    def tier_for(self, age_seconds: float) -> TrackingTier:
        if age_seconds >= self.retire_after_seconds:
            return TrackingTier.RETIRED
        if age_seconds < self.early_until_seconds:
            return TrackingTier.EARLY
        if age_seconds < self.medium_until_seconds:
            return TrackingTier.MEDIUM
        if age_seconds < self.normal_until_seconds:
            return TrackingTier.NORMAL
        return TrackingTier.LONG

    def interval_for(self, age_seconds: float) -> float | None:
        """Seconds until the next snapshot, or None once the token is retired."""
        tier = self.tier_for(age_seconds)
        match tier:
            case TrackingTier.EARLY:
                return self.early_interval_seconds
            case TrackingTier.MEDIUM:
                return self.medium_interval_seconds
            case TrackingTier.NORMAL:
                return self.normal_interval_seconds
            case TrackingTier.LONG:
                return self.long_interval_seconds
            case TrackingTier.RETIRED:
                return None

    def snapshots_per_token(self) -> float:
        """How many snapshots one token costs over its tracked lifetime.

        The number that turns a concurrency limit into a request rate, so the
        budget can be checked rather than hoped for.
        """
        total = 0.0
        previous = 0.0
        for boundary, interval in (
            (self.early_until_seconds, self.early_interval_seconds),
            (self.medium_until_seconds, self.medium_interval_seconds),
            (self.normal_until_seconds, self.normal_interval_seconds),
        ):
            upper = min(boundary, self.retire_after_seconds)
            if upper > previous:
                total += (upper - previous) / interval
            previous = upper
        if self.retire_after_seconds > previous:
            total += (self.retire_after_seconds - previous) / self.long_interval_seconds
        return total

    def estimated_requests_per_second(self, concurrent_tokens: int) -> float:
        """Average request rate implied by holding this many tokens in tracking.

        Average, not peak: a token spends most of its tracked life in the cheap
        tiers. The peak — every tracked token in EARLY at once — is
        ``concurrent / early_interval``, and is what a burst of admissions
        would actually cost.
        """
        if self.retire_after_seconds <= 0:
            return 0.0
        return concurrent_tokens * self.snapshots_per_token() / self.retire_after_seconds
