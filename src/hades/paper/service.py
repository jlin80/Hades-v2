"""The paper trading loop.

Four steps per pass: decide on new signals, fill pending orders, mark open
positions, close the ones whose rules fired.

## Why a pending state exists

Spec §14 asks for latency to be modelled. An order decided at T does not fill at
T — it fills against whatever the curve looks like when it arrives. So an
approved signal becomes a PENDING trade with ``submit_at = decision_at +
latency``, and it fills against the first snapshot at or after that.

Filling against a *later* snapshot is not look-ahead: the decision was already
made, from data available at the time. Using the decision's own snapshot would
be the unrealistic choice, because it would hand the simulator a price nobody
could have traded at.

## Nothing here can execute

No signer, no wallet, no RPC. Fills are computed from stored curve reserves by
``hades.paper.curve``. The AST scan in ``tests/test_safety.py`` fails the build
if a signing library appears anywhere under ``src/``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, func, select, update

from hades.db.engine import Database
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
from hades.paper.curve import estimate_buy_slippage, simulate_buy, simulate_sell
from hades.paper.exits import ExitRules, decide_exit
from hades.paper.notify import PaperDiscordNotifier
from hades.risk.engine import RiskEngine, RiskState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CurveSnapshot:
    """Curve state read back from a stored ``MarketSnapshot``, in whole units.

    Carries market cap alongside the raw reserves so a notification can report
    "bought $10 of a token at a $30k market cap" without a second query --
    everything the curve math and everything a human would want to see comes
    from the same row.
    """

    observed_at: datetime
    virtual_sol: float | None
    virtual_tokens: float | None
    liquidity_sol: float | None
    price_sol: float | None
    market_cap_sol: float | None
    market_cap_usd: float | None


@dataclass
class PaperCounters:
    passes: int = 0
    decided: int = 0
    approved: int = 0
    rejected: int = 0
    filled: int = 0
    fill_failed: int = 0
    closed: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def as_dict(self) -> dict[str, int]:
        return {
            "passes": self.passes,
            "decided": self.decided,
            "approved": self.approved,
            "rejected": self.rejected,
            "filled": self.filled,
            "fill_failed": self.fill_failed,
            "closed": self.closed,
        }


@dataclass(frozen=True, slots=True)
class PaperConfig:
    starting_balance_sol: float = 1.0
    position_size_sol: float = 0.02
    fee_rate: float = 0.01
    # Modelled delay between deciding and the order reaching the chain.
    latency_seconds: float = 2.0
    pass_interval_seconds: float = 3.0
    batch_size: int = 25


class PaperTradingService:
    """Simulated execution. Produces trades, never orders."""

    def __init__(
        self,
        database: Database,
        *,
        risk: RiskEngine | None = None,
        exit_rules: ExitRules | None = None,
        config: PaperConfig | None = None,
        notifier: PaperDiscordNotifier | None = None,
    ) -> None:
        self._database = database
        self._risk = risk or RiskEngine()
        self._exits = exit_rules or ExitRules()
        self._config = config or PaperConfig()
        # Optional and best-effort, same contract as the signal notifier: a
        # trade is already durable in Postgres the moment it fills or closes,
        # so a missing or failing notifier must never change what this loop
        # does, only whether a human hears about it before polling /status.
        self._notifier = notifier
        self.counters = PaperCounters()

    async def aclose(self) -> None:
        if self._notifier is not None:
            await self._notifier.aclose()

    # --- portfolio ---------------------------------------------------------

    async def portfolio(self, *, token_id: object = None, now: datetime | None = None) -> RiskState:
        """Current account state, read from the database.

        Recomputed from closed trades rather than kept in memory: a restart must
        not reset the daily loss limit, which would let a bad day start over as
        often as the process does.
        """
        config = self._config
        async with self._database.session() as session:
            realised = (
                await session.scalar(
                    select(func.coalesce(func.sum(PaperTrade.net_pnl_sol), 0.0)).where(
                        PaperTrade.state == TradeState.CLOSED
                    )
                )
                or 0.0
            )
            since = (now or datetime.now(tz=UTC)) - timedelta(days=1)
            today = (
                await session.scalar(
                    select(func.coalesce(func.sum(PaperTrade.net_pnl_sol), 0.0)).where(
                        PaperTrade.state == TradeState.CLOSED,
                        PaperTrade.exit_time > since,
                    )
                )
                or 0.0
            )
            open_positions = (
                await session.scalar(
                    select(func.count())
                    .select_from(PaperTrade)
                    .where(PaperTrade.state.in_((TradeState.PENDING, TradeState.OPEN)))
                )
                or 0
            )
            committed = (
                await session.scalar(
                    select(func.coalesce(func.sum(PaperTrade.position_size_sol), 0.0)).where(
                        PaperTrade.state.in_((TradeState.PENDING, TradeState.OPEN))
                    )
                )
                or 0.0
            )
            open_for_token = 0
            if token_id is not None:
                open_for_token = (
                    await session.scalar(
                        select(func.count())
                        .select_from(PaperTrade)
                        .where(
                            PaperTrade.token_id == token_id,
                            PaperTrade.state.in_((TradeState.PENDING, TradeState.OPEN)),
                        )
                    )
                    or 0
                )

        equity = config.starting_balance_sol + realised
        return RiskState(
            balance_sol=equity - committed,
            open_positions=open_positions,
            open_for_token=open_for_token,
            realised_pnl_today_sol=today,
            # Peak equity is approximated by the better of start and now. A
            # true high-water mark needs an equity series, which Phase 7 builds;
            # this under-reports drawdown rather than over-reporting it, and the
            # limitation is recorded rather than hidden.
            peak_equity_sol=max(config.starting_balance_sol, equity),
            current_equity_sol=equity,
        )

    # --- steps -------------------------------------------------------------

    async def decide_new_signals(self, *, now: datetime | None = None) -> int:
        """Risk-evaluate signals that have no decision yet. Returns approvals.

        ``now`` is injectable for the same reason it is everywhere else in
        this codebase: a component that reads the wall clock cannot be tested
        against a fixed scenario.
        """
        async with self._database.session() as session:
            decided = select(RiskDecisionRow.signal_id)
            rows = (
                await session.execute(
                    select(
                        SignalRow.id,
                        SignalRow.token_id,
                        SignalRow.token_address,
                        SignalRow.strategy,
                        SignalRow.created_at,
                        Token.created_at,
                    )
                    .join(Token, Token.id == SignalRow.token_id)
                    .where(SignalRow.id.not_in(decided))
                    .order_by(SignalRow.created_at)
                    .limit(self._config.batch_size)
                )
            ).all()

        approved = 0
        for signal_id, token_id, address, strategy, created_at, token_created in rows:
            decision_at = now or datetime.now(tz=UTC)
            # The newest snapshot *at or before* the decision, never simply the
            # newest. In production those are the same thing; in a replay they
            # are not, and taking the latest would let a decision be gated on
            # data that did not exist when it was made.
            snapshot = await self._snapshot_at_or_before(token_id, decision_at)
            if snapshot is None:
                continue

            token_age = (
                (_as_utc(snapshot.observed_at) - _as_utc(token_created)).total_seconds()
                if token_created
                else None
            )
            slippage = (
                estimate_buy_slippage(
                    sol_in=self._config.position_size_sol,
                    virtual_sol=snapshot.virtual_sol,
                    virtual_tokens=snapshot.virtual_tokens,
                )
                if snapshot.virtual_sol and snapshot.virtual_tokens
                else None
            )

            state = await self.portfolio(token_id=token_id, now=decision_at)
            decision = self._risk.evaluate(
                signal_created_at=_as_utc(created_at),
                decision_at=decision_at,
                token_age_seconds=token_age,
                liquidity_sol=snapshot.liquidity_sol,
                estimated_slippage=slippage,
                state=state,
                requested_size_sol=self._config.position_size_sol,
            )

            async with self._database.session() as session:
                session.add(
                    RiskDecisionRow(
                        signal_id=signal_id,
                        token_address=address,
                        approved=decision.approved,
                        signal_created_at=decision.signal_created_at,
                        decision_at=decision.decision_at,
                        data_age_ms=decision.data_age_ms,
                        position_size_sol=decision.position_size_sol,
                        checks=[c.as_dict() for c in decision.checks],
                    )
                )
                if decision.approved:
                    session.add(
                        PaperTrade(
                            signal_id=signal_id,
                            token_id=token_id,
                            token_address=address,
                            strategy=strategy,
                            state=TradeState.PENDING,
                            decision_at=decision_at,
                            submit_at=decision_at + timedelta(seconds=self._config.latency_seconds),
                            position_size_sol=decision.position_size_sol,
                        )
                    )
                await session.commit()

            self.counters.decided += 1
            if decision.approved:
                self.counters.approved += 1
                approved += 1
            else:
                self.counters.rejected += 1
                logger.info(
                    "signal_rejected",
                    extra={
                        "context": {
                            "token_address": address,
                            "reasons": [r.value for r in decision.rejections],
                            "data_age_ms": decision.data_age_ms,
                        }
                    },
                )
        return approved

    async def fill_pending(self, *, now: datetime | None = None) -> int:
        """Fill orders whose latency has elapsed, against a later snapshot."""
        moment = now or datetime.now(tz=UTC)
        async with self._database.session() as session:
            pending = (
                (
                    await session.execute(
                        select(PaperTrade).where(
                            PaperTrade.state == TradeState.PENDING,
                            PaperTrade.submit_at <= moment,
                        )
                    )
                )
                .scalars()
                .all()
            )

        filled = 0
        for trade in pending:
            snapshot = await self._snapshot_at_or_after(trade.token_id, _as_utc(trade.submit_at))
            if snapshot is None:
                continue

            fill = (
                simulate_buy(
                    sol_in=trade.position_size_sol,
                    virtual_sol=snapshot.virtual_sol,
                    virtual_tokens=snapshot.virtual_tokens,
                    fee_rate=self._config.fee_rate,
                )
                if snapshot.virtual_sol and snapshot.virtual_tokens
                else None
            )
            async with self._database.session() as session:
                if fill is None:
                    # Unfillable is CANCELLED, not a zero-size open position: a
                    # position we could not price is not a position we hold.
                    await session.execute(
                        update(PaperTrade)
                        .where(PaperTrade.id == trade.id)
                        .values(state=TradeState.CANCELLED)
                    )
                    self.counters.fill_failed += 1
                else:
                    await session.execute(
                        update(PaperTrade)
                        .where(PaperTrade.id == trade.id)
                        .values(
                            state=TradeState.OPEN,
                            entry_time=_as_utc(snapshot.observed_at),
                            entry_price=fill.effective_price,
                            entry_tokens=fill.tokens_out,
                            entry_fee_sol=fill.fee_sol,
                            entry_slippage=fill.slippage_fraction,
                            peak_price=fill.effective_price,
                        )
                    )
                    filled += 1
                    self.counters.filled += 1
                await session.commit()

            if fill is not None and self._notifier is not None:
                # Outside the write transaction and after commit: a webhook
                # failure must never roll back a fill that already happened.
                await self._notify_trade_opened(trade, fill, snapshot)
        return filled

    async def _notify_trade_opened(
        self, trade: PaperTrade, fill: object, snapshot: CurveSnapshot
    ) -> None:
        if self._notifier is None:  # pragma: no cover — guarded by the caller
            return
        state = await self.portfolio(token_id=trade.token_id)
        symbol = await self._token_symbol(trade.token_id)
        counts = await self._pipeline_counts()
        await self._notifier.send_trade_opened(
            token_address=trade.token_address,
            token_symbol=symbol,
            position_size_sol=trade.position_size_sol,
            entry_price_sol=fill.effective_price,  # type: ignore[attr-defined]
            entry_slippage=fill.slippage_fraction,  # type: ignore[attr-defined]
            market_cap_sol=snapshot.market_cap_sol,
            market_cap_usd=snapshot.market_cap_usd,
            sol_price_usd=_implied_sol_price_usd(snapshot),
            balance_sol=state.balance_sol,
            equity_sol=state.current_equity_sol,
            open_positions=state.open_positions,
            observations_total=counts[0],
            signals_total=counts[1],
            trades_total=counts[2],
        )

    async def _token_symbol(self, token_id: object) -> str | None:
        async with self._database.session() as session:
            return await session.scalar(select(Token.symbol).where(Token.id == token_id))

    async def _pipeline_counts(self) -> tuple[int, int, int]:
        """(observations, signals, trades) so far -- the "cuantos analisis" in a
        notification. One extra round of cheap counts, paid only when a trade
        actually opens (~2.4% of evaluations, not per pass)."""
        async with self._database.session() as session:
            observations = (
                await session.scalar(select(func.count()).select_from(FeatureObservation))
            ) or 0
            signals = (await session.scalar(select(func.count()).select_from(SignalRow))) or 0
            trades = (await session.scalar(select(func.count()).select_from(PaperTrade))) or 0
        return observations, signals, trades

    async def manage_open(self, *, now: datetime | None = None) -> int:
        """Mark open positions and close the ones whose rules fired."""
        async with self._database.session() as session:
            open_trades = (
                (
                    await session.execute(
                        select(PaperTrade).where(PaperTrade.state == TradeState.OPEN)
                    )
                )
                .scalars()
                .all()
            )

        if not open_trades:
            return 0

        state = await self.portfolio(now=now)
        drawdown = state.drawdown_fraction
        risk_exit = state.realised_pnl_today_sol <= -abs(self._risk.limits.max_daily_loss_sol) or (
            drawdown is not None and drawdown >= self._risk.limits.max_drawdown_fraction
        )

        closed = 0
        for trade in open_trades:
            snapshot = await self._latest_snapshot(trade.token_id)
            if snapshot is None:
                continue
            if trade.entry_price is None or trade.entry_time is None:
                continue

            held = (_as_utc(snapshot.observed_at) - _as_utc(trade.entry_time)).total_seconds()
            evaluation = decide_exit(
                entry_price=trade.entry_price,
                current_price=snapshot.price_sol,
                peak_price=trade.peak_price,
                held_seconds=held,
                rules=self._exits,
                risk_exit=risk_exit,
            )

            peak = max(trade.peak_price or trade.entry_price, snapshot.price_sol or 0.0)
            if not evaluation.should_exit:
                async with self._database.session() as session:
                    await session.execute(
                        update(PaperTrade).where(PaperTrade.id == trade.id).values(peak_price=peak)
                    )
                    await session.commit()
                continue

            if await self._close(trade, snapshot, evaluation.reason):
                closed += 1
        return closed

    async def _close(
        self, trade: PaperTrade, snapshot: CurveSnapshot, reason: ExitReason | None
    ) -> bool:
        if trade.entry_tokens is None or trade.entry_price is None:
            return False
        fill = (
            simulate_sell(
                tokens_in=trade.entry_tokens,
                virtual_sol=snapshot.virtual_sol,
                virtual_tokens=snapshot.virtual_tokens,
                fee_rate=self._config.fee_rate,
            )
            if snapshot.virtual_sol and snapshot.virtual_tokens
            else None
        )
        if fill is None:
            # Cannot price the exit, so the position stays open. Inventing a
            # closing price would put a fabricated number into the PnL.
            return False

        entry_fee = trade.entry_fee_sol or 0.0
        fees = entry_fee + fill.fee_sol
        # Gross is what the position did before any friction: what the same
        # tokens were worth at the spot price on both sides.
        gross = (fill.spot_price - trade.entry_price) * trade.entry_tokens
        net = fill.sol_out - trade.position_size_sol
        slippage_cost = gross - net - fees

        async with self._database.session() as session:
            await session.execute(
                update(PaperTrade)
                .where(PaperTrade.id == trade.id)
                .values(
                    state=TradeState.CLOSED,
                    exit_time=_as_utc(snapshot.observed_at),
                    exit_price=fill.effective_price,
                    exit_sol=fill.sol_out,
                    exit_fee_sol=fill.fee_sol,
                    exit_slippage=fill.slippage_fraction,
                    exit_reason=reason,
                    gross_pnl_sol=gross,
                    fees_sol=fees,
                    slippage_cost_sol=slippage_cost,
                    net_pnl_sol=net,
                )
            )
            await session.commit()

        self.counters.closed += 1
        logger.info(
            "paper_trade_closed",
            extra={
                "context": {
                    "token_address": trade.token_address,
                    "exit_reason": reason.value if reason else None,
                    "net_pnl_sol": net,
                    "fees_sol": fees,
                }
            },
        )

        if self._notifier is not None:
            state = await self.portfolio()
            symbol = await self._token_symbol(trade.token_id)
            await self._notifier.send_trade_closed(
                token_address=trade.token_address,
                token_symbol=symbol,
                exit_reason=reason.value if reason else "UNKNOWN",
                net_pnl_sol=net,
                fees_sol=fees,
                balance_sol=state.balance_sol,
                equity_sol=state.current_equity_sol,
            )
        return True

    # --- helpers -----------------------------------------------------------

    _SNAPSHOT_COLUMNS = (
        MarketSnapshot.observed_at,
        MarketSnapshot.virtual_sol_reserves,
        MarketSnapshot.virtual_token_reserves,
        MarketSnapshot.liquidity_sol,
        MarketSnapshot.price_sol,
        MarketSnapshot.market_cap_sol,
        MarketSnapshot.market_cap_usd,
    )

    async def _latest_snapshot(self, token_id: object) -> CurveSnapshot | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(*self._SNAPSHOT_COLUMNS)
                    .where(MarketSnapshot.token_id == token_id)
                    .order_by(MarketSnapshot.observed_at.desc())
                    .limit(1)
                )
            ).first()
        return _to_curve(row)

    async def _snapshot_at_or_before(
        self, token_id: object, moment: datetime
    ) -> CurveSnapshot | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(*self._SNAPSHOT_COLUMNS)
                    .where(
                        MarketSnapshot.token_id == token_id,
                        MarketSnapshot.observed_at <= moment,
                    )
                    .order_by(MarketSnapshot.observed_at.desc())
                    .limit(1)
                )
            ).first()
        return _to_curve(row)

    async def _snapshot_at_or_after(
        self, token_id: object, moment: datetime
    ) -> CurveSnapshot | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(*self._SNAPSHOT_COLUMNS)
                    .where(
                        MarketSnapshot.token_id == token_id,
                        MarketSnapshot.observed_at >= moment,
                    )
                    .order_by(MarketSnapshot.observed_at)
                    .limit(1)
                )
            ).first()
        return _to_curve(row)

    async def run_once(self, *, now: datetime | None = None) -> int:
        self.counters.passes += 1
        await self.decide_new_signals(now=now)
        await self.fill_pending(now=now)
        return await self.manage_open(now=now)

    async def run(self) -> None:
        logger.info(
            "paper_trading_started",
            extra={"context": {"note": "simulated fills only; nothing can execute"}},
        )
        while True:
            await self.run_once()
            await asyncio.sleep(self._config.pass_interval_seconds)


def _to_curve(row: Row[Any] | None) -> CurveSnapshot | None:
    if row is None:
        return None
    observed_at, virtual_sol, virtual_tokens, liquidity, price, mcap_sol, mcap_usd = row
    # Reserves are stored in base units; the curve math works in whole units.
    return CurveSnapshot(
        observed_at=observed_at,
        virtual_sol=virtual_sol / 1e9 if virtual_sol else None,
        virtual_tokens=virtual_tokens / 1e6 if virtual_tokens else None,
        liquidity_sol=liquidity,
        price_sol=price,
        market_cap_sol=mcap_sol,
        market_cap_usd=mcap_usd,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _implied_sol_price_usd(snapshot: CurveSnapshot) -> float | None:
    """SOL/USD implied by the same snapshot quoting its market cap in both.

    Lets a notification show "$10 of SOL" without a second price source --
    consistent with how ``hades.tracking.derive`` computes the same quantity.
    """
    if not snapshot.market_cap_sol or snapshot.market_cap_usd is None:
        return None
    return snapshot.market_cap_usd / snapshot.market_cap_sol
