"""The paper trade lifecycle: decide, fill after latency, mark, close."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.db.base import Base
from hades.db.models import (
    ExitReason,
    FeatureObservation,
    MarketSnapshot,
    PaperTrade,
    RiskDecisionRow,
    SignalRow,
    Token,
    TradeState,
)
from hades.discovery.repository import TokenRepository
from hades.paper.exits import ExitRules
from hades.paper.service import PaperConfig, PaperTradingService
from hades.providers.models import DiscoveredToken
from hades.risk.engine import RiskEngine, RiskLimits

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"

# Real reserve scale, in base units.
V_SOL = 58_027_910_356
V_TOKENS = 554_733_056_434_731


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class FakeDatabase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


async def seed(
    session: AsyncSession,
    *,
    prices: list[tuple[float, float]] | None = None,
    token_age_seconds: float = 60.0,
) -> uuid.UUID:
    """A token with a signal and a snapshot series. Returns the signal id.

    ``prices`` is (offset_seconds, price_multiplier) relative to the entry
    curve, so a test can make the price rise or fall after the fill.
    """
    created = NOW - timedelta(seconds=token_age_seconds)
    await TokenRepository(session).upsert(
        DiscoveredToken(
            token_address=MINT, source="pumpportal", created_at=created, observed_at=created
        )
    )
    token = await session.scalar(select(Token).where(Token.token_address == MINT))
    assert token is not None

    for offset, multiplier in prices or [(0.0, 1.0)]:
        observed = NOW + timedelta(seconds=offset)
        # Moving virtual SOL moves the price along the curve; k is preserved by
        # adjusting tokens, so the reserves stay a consistent curve state.
        virtual_sol = int(V_SOL * multiplier)
        virtual_tokens = int(V_SOL * V_TOKENS / virtual_sol)
        session.add(
            MarketSnapshot(
                token_id=token.id,
                token_address=MINT,
                provider_name="pumpfun",
                observed_at=observed,
                received_at=observed,
                tier="EARLY",
                token_age_seconds=token_age_seconds + offset,
                virtual_sol_reserves=virtual_sol,
                virtual_token_reserves=virtual_tokens,
                real_sol_reserves=28_027_910_356,
                liquidity_sol=28.03,
                price_sol=(virtual_sol / 1e9) / (virtual_tokens / 1e6),
                is_stale=False,
            )
        )

    observation = FeatureObservation(
        token_id=token.id,
        token_address=MINT,
        observed_at=NOW,
        feature_version="1.0.0",
        features={},
    )
    session.add(observation)
    await session.flush()
    signal = SignalRow(
        observation_id=observation.id,
        token_id=token.id,
        token_address=MINT,
        strategy="early_momentum",
        strategy_version="1.0.0",
        created_at=NOW,
        conditions=[],
    )
    session.add(signal)
    await session.commit()
    return signal.id


def service(
    session: AsyncSession,
    *,
    limits: RiskLimits | None = None,
    exits: ExitRules | None = None,
    config: PaperConfig | None = None,
) -> PaperTradingService:
    return PaperTradingService(
        FakeDatabase(session),  # type: ignore[arg-type]
        risk=RiskEngine(limits or RiskLimits(max_data_age_seconds=10**9)),
        exit_rules=exits,
        config=config or PaperConfig(latency_seconds=0.0),
    )


class TestRiskDecisions:
    async def test_every_signal_gets_a_persisted_verdict(self, session: AsyncSession) -> None:
        """Rejections too. §17 needs the strategy's reach, not just its hits."""
        await seed(session)
        await service(session).decide_new_signals(now=NOW)

        row = await session.scalar(select(RiskDecisionRow))
        assert row is not None
        assert row.token_address == MINT
        assert row.data_age_ms >= 0
        assert row.checks

    async def test_a_rejected_signal_creates_no_trade(self, session: AsyncSession) -> None:
        await seed(session)
        await service(
            session, limits=RiskLimits(min_liquidity_sol=10**6, max_data_age_seconds=10**9)
        ).decide_new_signals(now=NOW)

        row = await session.scalar(select(RiskDecisionRow))
        assert row is not None
        assert row.approved is False
        assert (await session.scalar(select(func.count()).select_from(PaperTrade))) == 0

    async def test_a_signal_is_decided_only_once(self, session: AsyncSession) -> None:
        """A restart must not produce a second, possibly different verdict."""
        await seed(session)
        engine = service(session)
        await engine.decide_new_signals(now=NOW)
        await engine.decide_new_signals(now=NOW)

        assert (await session.scalar(select(func.count()).select_from(RiskDecisionRow))) == 1
        assert (await session.scalar(select(func.count()).select_from(PaperTrade))) == 1


class TestFillsModelLatency:
    async def test_an_approved_signal_starts_pending_not_open(self, session: AsyncSession) -> None:
        """Spec §14: an order decided at T is not filled at T."""
        await seed(session)
        await service(session, config=PaperConfig(latency_seconds=5.0)).decide_new_signals(now=NOW)

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.state is TradeState.PENDING
        assert trade.submit_at > trade.decision_at

    async def test_a_fill_uses_a_snapshot_at_or_after_submit(self, session: AsyncSession) -> None:
        """Not the snapshot the decision was made on.

        Filling at the decision's own price would hand the simulator a price
        nobody could have traded at.
        """
        # Price rises 20% ten seconds after the decision.
        await seed(session, prices=[(0.0, 1.0), (10.0, 1.2)])
        engine = service(session, config=PaperConfig(latency_seconds=0.0))
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.state is TradeState.OPEN
        assert trade.entry_price is not None
        assert trade.entry_tokens is not None
        assert trade.entry_fee_sol == pytest.approx(0.02 * 0.01)
        # Slippage is derived from the curve, not assumed.
        assert trade.entry_slippage is not None
        assert trade.entry_slippage > 0

    async def test_an_unpriceable_fill_cancels_rather_than_opening(
        self, session: AsyncSession
    ) -> None:
        """A position we could not price is not a position we hold."""
        signal_id = await seed(session)
        engine = service(session)
        await engine.decide_new_signals(now=NOW)

        await session.execute(
            update(MarketSnapshot).values(virtual_sol_reserves=None, virtual_token_reserves=None)
        )
        await session.commit()
        await engine.fill_pending(now=NOW)

        trade = await session.scalar(select(PaperTrade).where(PaperTrade.signal_id == signal_id))
        assert trade is not None
        assert trade.state is TradeState.CANCELLED


class TestExits:
    async def _open_then(
        self, session: AsyncSession, *, later: list[tuple[float, float]]
    ) -> PaperTradingService:
        await seed(session, prices=[(0.0, 1.0), *later])
        engine = service(session)
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)
        return engine

    async def test_a_large_rise_takes_profit(self, session: AsyncSession) -> None:
        engine = await self._open_then(session, later=[(30.0, 2.0)])
        assert await engine.manage_open(now=NOW) == 1

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.state is TradeState.CLOSED
        assert trade.exit_reason is ExitReason.TAKE_PROFIT
        assert trade.net_pnl_sol is not None
        assert trade.net_pnl_sol > 0

    async def test_a_large_fall_stops_out(self, session: AsyncSession) -> None:
        engine = await self._open_then(session, later=[(30.0, 0.5)])
        assert await engine.manage_open(now=NOW) == 1

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.exit_reason is ExitReason.STOP_LOSS
        assert trade.net_pnl_sol is not None
        assert trade.net_pnl_sol < 0

    async def test_a_flat_price_keeps_the_position_open(self, session: AsyncSession) -> None:
        engine = await self._open_then(session, later=[(30.0, 1.0)])
        assert await engine.manage_open(now=NOW) == 0

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.state is TradeState.OPEN

    async def test_timeout_closes_a_stagnant_position(self, session: AsyncSession) -> None:
        await seed(session, prices=[(0.0, 1.0), (900.0, 1.01)])
        engine = service(session, exits=ExitRules(max_hold_seconds=60.0))
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)
        assert await engine.manage_open(now=NOW) == 1

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.exit_reason is ExitReason.TIMEOUT


class TestFrictionIsRecordedSeparately:
    async def test_fees_and_slippage_are_stored_apart_from_pnl(self, session: AsyncSession) -> None:
        """So a result reads as 'the edge' and 'what friction took'."""
        await seed(session, prices=[(0.0, 1.0), (30.0, 2.0)])
        engine = service(session)
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)
        await engine.manage_open(now=NOW)

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.fees_sol is not None
        assert trade.gross_pnl_sol is not None
        assert trade.net_pnl_sol is not None
        # Both sides of the round trip paid a fee -- the friction V1's realised
        # PnL understated until its Stage 2 hardening.
        assert trade.entry_fee_sol is not None
        assert trade.exit_fee_sol is not None
        assert trade.fees_sol == pytest.approx(trade.entry_fee_sol + trade.exit_fee_sol)
        # Net is what actually came back versus what went in.
        assert trade.net_pnl_sol == pytest.approx((trade.exit_sol or 0.0) - trade.position_size_sol)

    async def test_a_round_trip_at_an_unchanged_price_loses_money(
        self, session: AsyncSession
    ) -> None:
        """The number that decides whether an edge survives.

        Nothing moved, and the trade still lost: two fees plus slippage both
        ways. A simulator that showed break-even here would be lying.
        """
        await seed(session, prices=[(0.0, 1.0), (900.0, 1.0)])
        engine = service(session, exits=ExitRules(max_hold_seconds=60.0))
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)
        await engine.manage_open(now=NOW)

        trade = await session.scalar(select(PaperTrade))
        assert trade is not None
        assert trade.net_pnl_sol is not None
        assert trade.net_pnl_sol < 0


class TestPortfolio:
    async def test_realised_pnl_is_recomputed_from_closed_trades(
        self, session: AsyncSession
    ) -> None:
        """Not held in memory.

        A restart must not reset the daily loss limit, or a bad day starts over
        as often as the process does.
        """
        await seed(session, prices=[(0.0, 1.0), (30.0, 0.5)])
        engine = service(session)
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)
        await engine.manage_open(now=NOW)

        # A brand-new service object, with no memory at all.
        fresh = service(session)
        state = await fresh.portfolio(now=NOW)
        assert state.realised_pnl_today_sol < 0
        assert state.current_equity_sol < 1.0

    async def test_peak_equity_remembers_a_high_the_account_gave_back(
        self, session: AsyncSession
    ) -> None:
        """The drawdown the old approximation could not see.

        ``peak_equity_sol = max(start, now)`` has no memory: an account that
        rose to 1.5 and fell back to 1.0 reported a peak of 1.0 and therefore a
        drawdown of zero. Every drawdown that actually mattered — the ones that
        peaked and gave it back — was measured against the wrong reference.
        """
        engine = service(session)
        token = await TokenRepository(session).upsert(
            DiscoveredToken(token_address=MINT, source="test", created_at=NOW)
        )
        del token

        token_id = await session.scalar(select(Token.id).where(Token.token_address == MINT))
        for index, (pnl, offset) in enumerate([(0.5, 1), (-0.5, 2)]):
            session.add(
                PaperTrade(
                    id=uuid.uuid4(),
                    signal_id=uuid.uuid4(),
                    token_id=token_id,
                    token_address=MINT,
                    strategy="early_momentum",
                    state=TradeState.CLOSED,
                    decision_at=NOW,
                    submit_at=NOW,
                    entry_time=NOW,
                    exit_time=NOW + timedelta(minutes=offset),
                    position_size_sol=0.02,
                    net_pnl_sol=pnl,
                    exit_reason=ExitReason.TAKE_PROFIT if index == 0 else ExitReason.STOP_LOSS,
                )
            )
        await session.commit()

        state = await engine.portfolio(now=NOW + timedelta(minutes=5))

        assert state.current_equity_sol == pytest.approx(1.0)
        assert state.peak_equity_sol == pytest.approx(1.5)
        assert state.drawdown_fraction == pytest.approx(1 / 3)

    async def test_an_open_position_reserves_its_size(self, session: AsyncSession) -> None:
        await seed(session)
        engine = service(session)
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)

        state = await engine.portfolio(now=NOW)
        assert state.open_positions == 1
        assert state.balance_sol == pytest.approx(1.0 - 0.02)


class TestOnePositionPerToken:
    async def test_a_second_signal_on_a_held_token_is_refused(self, session: AsyncSession) -> None:
        """Phase 5 measured four signals on one token inside a minute."""
        await seed(session)
        engine = service(session)
        await engine.decide_new_signals(now=NOW)
        await engine.fill_pending(now=NOW)

        # A second signal on the same token, one snapshot later.
        token = await session.scalar(select(Token).where(Token.token_address == MINT))
        assert token is not None
        observation = FeatureObservation(
            token_id=token.id,
            token_address=MINT,
            observed_at=NOW + timedelta(seconds=10),
            feature_version="1.0.0",
            features={},
        )
        session.add(observation)
        await session.flush()
        session.add(
            SignalRow(
                observation_id=observation.id,
                token_id=token.id,
                token_address=MINT,
                strategy="early_momentum",
                strategy_version="1.0.0",
                created_at=NOW + timedelta(seconds=10),
                conditions=[],
            )
        )
        await session.commit()

        await engine.decide_new_signals(now=NOW)
        decisions = (await session.execute(select(RiskDecisionRow))).scalars().all()
        assert len(decisions) == 2
        assert [d.approved for d in decisions] == [True, False]
        assert (await session.scalar(select(func.count()).select_from(PaperTrade))) == 1


async def test_a_full_pass_runs_the_whole_lifecycle(session: AsyncSession) -> None:
    await seed(session, prices=[(0.0, 1.0), (30.0, 2.0)])
    engine = service(session)

    await engine.run_once(now=NOW)  # decide + fill
    await engine.run_once(now=NOW)  # mark + close

    trade = await session.scalar(select(PaperTrade))
    assert trade is not None
    assert trade.state is TradeState.CLOSED
    assert engine.counters.approved == 1
    assert engine.counters.filled == 1
    assert engine.counters.closed == 1
