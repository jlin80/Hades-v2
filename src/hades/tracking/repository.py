"""Tracking persistence: admission, the due queue, and snapshot writes."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hades.db.models import MarketSnapshot as SnapshotRow
from hades.db.models import Token, TokenState
from hades.providers.models import MarketSnapshot
from hades.tracking.schedule import TrackingSchedule, TrackingTier

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DueToken:
    """The minimum needed to take and schedule one snapshot."""

    id: uuid.UUID
    token_address: str
    created_at: datetime
    snapshot_failures: int


@dataclass(frozen=True, slots=True)
class TrackingStats:
    """Measured tracking state, for ``/status``."""

    tracking_now: int
    eligible_waiting: int
    snapshots_total: int
    snapshots_last_hour: int
    stale_snapshots: int
    tokens_retired: int
    tokens_migrated: int
    tokens_dead: int
    oldest_due_seconds: float | None


class TrackingRepository:
    """Reads and writes everything tracking needs."""

    def __init__(self, session: AsyncSession, schedule: TrackingSchedule) -> None:
        self._session = session
        self._schedule = schedule

    async def count_tracking(self) -> int:
        return (
            await self._session.scalar(
                select(func.count()).select_from(Token).where(Token.state == TokenState.TRACKING)
            )
            or 0
        )

    async def admit(self, *, slots: int, now: datetime | None = None) -> list[str]:
        """Move up to ``slots`` eligible tokens into TRACKING.

        Newest first. Two tokens compete for a slot only when we are at
        capacity, and at capacity the younger one is strictly more valuable:
        the research question is about the first minutes, and an older token
        has already spent some of them unobserved.

        A token needs ``created_at`` to be eligible at all — without it there is
        no age, so no tier, so no schedule. Those are the tokens D12 counts as
        backfill-exhausted.
        """
        if slots <= 0:
            return []
        moment = now or datetime.now(tz=UTC)
        horizon = moment - timedelta(seconds=self._schedule.retire_after_seconds)

        candidates = (
            await self._session.execute(
                select(Token.id, Token.token_address)
                .where(
                    Token.state == TokenState.DISCOVERED,
                    Token.created_at.is_not(None),
                    Token.created_at > horizon,
                )
                .order_by(Token.created_at.desc())
                .limit(slots)
            )
        ).all()
        if not candidates:
            return []

        ids = [row[0] for row in candidates]
        await self._session.execute(
            update(Token)
            .where(Token.id.in_(ids))
            .values(
                state=TokenState.TRACKING,
                tracking_started_at=moment,
                # Due immediately: the first snapshot of a young token is the
                # most valuable one we will ever take of it.
                next_snapshot_at=moment,
            )
        )
        await self._session.commit()
        self._session.expire_all()
        return [row[1] for row in candidates]

    async def count_eligible_waiting(self, *, now: datetime | None = None) -> int:
        """Tokens that could be tracked but are not, because capacity is full.

        Reported, not hidden: it is the size of the sample we are *declining*
        to observe, and no amount of later analysis recovers it.
        """
        moment = now or datetime.now(tz=UTC)
        horizon = moment - timedelta(seconds=self._schedule.retire_after_seconds)
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(Token)
                .where(
                    Token.state == TokenState.DISCOVERED,
                    Token.created_at.is_not(None),
                    Token.created_at > horizon,
                )
            )
            or 0
        )

    async def due(self, *, limit: int, now: datetime | None = None) -> list[DueToken]:
        """Tracked tokens whose next snapshot is due, most overdue first."""
        moment = now or datetime.now(tz=UTC)
        rows = (
            await self._session.execute(
                select(
                    Token.id,
                    Token.token_address,
                    Token.created_at,
                    Token.snapshot_failures,
                )
                .where(
                    Token.state == TokenState.TRACKING,
                    Token.next_snapshot_at.is_not(None),
                    Token.next_snapshot_at <= moment,
                    Token.created_at.is_not(None),
                )
                .order_by(Token.next_snapshot_at.asc())
                .limit(limit)
            )
        ).all()
        return [
            DueToken(
                id=row[0],
                token_address=row[1],
                created_at=_as_utc(row[2]),
                snapshot_failures=row[3],
            )
            for row in rows
        ]

    async def record_snapshot(
        self,
        token: DueToken,
        snapshot: MarketSnapshot,
        *,
        stale_after_seconds: float,
    ) -> TrackingTier:
        """Append the snapshot and schedule the next one. Returns the tier used.

        Append-only, and never an upsert: two observations of the same token a
        second apart are two different facts, and collapsing them would destroy
        the series the whole phase exists to build.
        """
        age_seconds = (snapshot.observed_at - token.created_at).total_seconds()
        tier = self._schedule.tier_for(age_seconds)
        data_age = snapshot.provider_data_age_seconds

        self._session.add(
            SnapshotRow(
                token_id=token.id,
                token_address=token.token_address,
                provider_name=snapshot.source,
                provider_updated_at=snapshot.provider_updated_at,
                observed_at=snapshot.observed_at,
                received_at=snapshot.received_at,
                token_age_seconds=age_seconds,
                tier=tier.value,
                virtual_sol_reserves=snapshot.virtual_sol_reserves,
                virtual_token_reserves=snapshot.virtual_token_reserves,
                real_sol_reserves=snapshot.real_sol_reserves,
                real_token_reserves=snapshot.real_token_reserves,
                total_supply=snapshot.total_supply,
                base_decimals=snapshot.base_decimals,
                quote_decimals=snapshot.quote_decimals,
                price_sol=snapshot.price_sol,
                market_cap_sol=snapshot.market_cap_sol,
                liquidity_sol=snapshot.liquidity_sol,
                market_cap_usd=snapshot.market_cap_usd,
                sol_price_usd=snapshot.sol_price_usd,
                is_complete=snapshot.is_complete,
                last_trade_at=snapshot.last_trade_at,
                reply_count=snapshot.reply_count,
                provider_data_age_seconds=data_age,
                is_stale=data_age is not None and data_age > stale_after_seconds,
            )
        )

        interval = self._schedule.interval_for(age_seconds)
        values: dict[str, object] = {
            "last_snapshot_at": snapshot.observed_at,
            "snapshot_count": Token.snapshot_count + 1,
            # A success clears the failure streak: the counter is for detecting
            # a token that has gone, not for tallying every hiccup it ever had.
            "snapshot_failures": 0,
        }
        if snapshot.is_complete:
            # Graduated off the bonding curve. Recorded as a distinct outcome
            # rather than as an ending, because §2 asks for the migration event
            # and because a graduation is the most interesting thing a Pump.fun
            # token can do.
            values |= {"state": TokenState.MIGRATED, "next_snapshot_at": None}
        elif interval is None:
            values |= {"state": TokenState.INACTIVE, "next_snapshot_at": None}
        else:
            values["next_snapshot_at"] = snapshot.observed_at + timedelta(seconds=interval)

        await self._session.execute(update(Token).where(Token.id == token.id).values(**values))
        await self._session.commit()
        self._session.expire_all()
        return tier

    async def record_failure(
        self,
        token: DueToken,
        *,
        max_failures: int,
        retry_after: float,
        now: datetime | None = None,
    ) -> bool:
        """Count a failed snapshot. Returns True if the token was given up on.

        A token whose provider record has disappeared must stop consuming a
        tracking slot — the slot is the scarce resource, and holding one for a
        token that cannot be observed costs a token that could have been.
        """
        failures = token.snapshot_failures + 1
        give_up = failures >= max_failures
        values: dict[str, object] = {"snapshot_failures": failures}
        if give_up:
            values |= {"state": TokenState.DEAD, "next_snapshot_at": None}
        else:
            moment = now or datetime.now(tz=UTC)
            values["next_snapshot_at"] = moment + timedelta(seconds=retry_after)

        await self._session.execute(update(Token).where(Token.id == token.id).values(**values))
        await self._session.commit()
        self._session.expire_all()
        return give_up

    async def retire_overdue(self, *, now: datetime | None = None) -> int:
        """Release slots held by tokens past the tracking horizon.

        Normally ``record_snapshot`` retires a token when its own snapshot
        shows it aged out. This catches the ones that never got that snapshot —
        a token whose provider was down through its whole horizon would
        otherwise hold a slot forever.
        """
        moment = now or datetime.now(tz=UTC)
        horizon = moment - timedelta(seconds=self._schedule.retire_after_seconds)
        overdue = list(
            await self._session.scalars(
                select(Token.id).where(
                    Token.state == TokenState.TRACKING,
                    Token.created_at.is_not(None),
                    Token.created_at <= horizon,
                )
            )
        )
        if not overdue:
            return 0

        await self._session.execute(
            update(Token)
            .where(Token.id.in_(overdue))
            .values(state=TokenState.INACTIVE, next_snapshot_at=None)
        )
        await self._session.commit()
        self._session.expire_all()
        return len(overdue)

    async def stats(self, *, now: datetime | None = None) -> TrackingStats:
        moment = now or datetime.now(tz=UTC)

        async def count_state(state: TokenState) -> int:
            return (
                await self._session.scalar(
                    select(func.count()).select_from(Token).where(Token.state == state)
                )
                or 0
            )

        snapshots_total = (
            await self._session.scalar(select(func.count()).select_from(SnapshotRow)) or 0
        )
        snapshots_last_hour = (
            await self._session.scalar(
                select(func.count())
                .select_from(SnapshotRow)
                .where(SnapshotRow.observed_at > moment - timedelta(hours=1))
            )
            or 0
        )
        stale = (
            await self._session.scalar(
                select(func.count()).select_from(SnapshotRow).where(SnapshotRow.is_stale.is_(True))
            )
            or 0
        )
        oldest_due = await self._session.scalar(
            select(func.min(Token.next_snapshot_at)).where(
                Token.state == TokenState.TRACKING, Token.next_snapshot_at.is_not(None)
            )
        )

        return TrackingStats(
            tracking_now=await count_state(TokenState.TRACKING),
            eligible_waiting=await self.count_eligible_waiting(now=moment),
            snapshots_total=snapshots_total,
            snapshots_last_hour=snapshots_last_hour,
            stale_snapshots=stale,
            tokens_retired=await count_state(TokenState.INACTIVE),
            tokens_migrated=await count_state(TokenState.MIGRATED),
            tokens_dead=await count_state(TokenState.DEAD),
            # Positive means we are behind: the most overdue token has been
            # waiting this long past its scheduled time. It is the single
            # number that says whether the tracker is keeping up.
            oldest_due_seconds=(
                (moment - _as_utc(oldest_due)).total_seconds() if oldest_due else None
            ),
        )


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
