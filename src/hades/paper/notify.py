"""Detailed Discord notifications for paper trades. Optional, best-effort.

Same contract as ``hades.signals.notify.DiscordNotifier``: a trade is already
durable in Postgres the moment it fills or closes, so a webhook failure must
never affect the paper-trading loop. No retry — a duplicate post on retry
would be worse than a missed one, since the database row is the record either
way.

This is a richer notification than the signal one, because only the paper
side knows what actually happened: the fill price, the size in SOL *and* USD
(derived from the same snapshot's own market-cap quote, never a second price
source), and the account balance the fill left behind.
"""

from __future__ import annotations

import logging

import httpx

from hades.signals.notify import DISCLAIMER

logger = logging.getLogger(__name__)

_COLOR_OPEN = 0x57F287
_COLOR_CLOSE_PROFIT = 0x57F287
_COLOR_CLOSE_LOSS = 0xED4245


class PaperDiscordNotifier:
    """Fire-and-forget POSTs describing paper trade fills and closes."""

    def __init__(self, webhook_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def send_trade_opened(
        self,
        *,
        token_address: str,
        token_symbol: str | None,
        position_size_sol: float,
        entry_price_sol: float,
        entry_slippage: float,
        market_cap_sol: float | None,
        market_cap_usd: float | None,
        sol_price_usd: float | None,
        balance_sol: float,
        equity_sol: float,
        open_positions: int,
        observations_total: int,
        signals_total: int,
        trades_total: int,
    ) -> None:
        """A position opened. Reports what was bought, at what cap, and with
        what's left in the account -- the "$10 of X at a $30k market cap" the
        operator asked to see."""
        size_usd = position_size_sol * sol_price_usd if sol_price_usd else None
        name = f"{token_symbol} (`{token_address}`)" if token_symbol else f"`{token_address}`"

        fields = [
            {"name": "Token", "value": name, "inline": False},
            {
                "name": "Position size",
                "value": _sol_and_usd(position_size_sol, size_usd),
                "inline": True,
            },
            {"name": "Entry price", "value": f"{entry_price_sol:.4e} SOL", "inline": True},
            {"name": "Slippage", "value": f"{entry_slippage:.2%}", "inline": True},
            {
                "name": "Market cap at entry",
                "value": _sol_and_usd(market_cap_sol, market_cap_usd),
                "inline": True,
            },
            {"name": "Balance remaining", "value": f"{balance_sol:.4f} SOL", "inline": True},
            {"name": "Equity", "value": f"{equity_sol:.4f} SOL", "inline": True},
            {"name": "Open positions", "value": str(open_positions), "inline": True},
            {
                "name": "Pipeline so far",
                "value": (
                    f"{observations_total} analyses -> {signals_total} signals -> "
                    f"{trades_total} trades"
                ),
                "inline": False,
            },
        ]
        await self._post(
            {
                "title": "📈 Paper trade opened",
                "color": _COLOR_OPEN,
                "fields": fields,
                "footer": {"text": DISCLAIMER},
            }
        )

    async def send_trade_closed(
        self,
        *,
        token_address: str,
        token_symbol: str | None,
        exit_reason: str,
        net_pnl_sol: float,
        fees_sol: float,
        balance_sol: float,
        equity_sol: float,
    ) -> None:
        """A position closed. Reports the actual outcome, fees included."""
        name = f"{token_symbol} (`{token_address}`)" if token_symbol else f"`{token_address}`"
        sign = "+" if net_pnl_sol >= 0 else ""
        fields = [
            {"name": "Token", "value": name, "inline": False},
            {"name": "Exit reason", "value": exit_reason, "inline": True},
            {"name": "Net PnL", "value": f"{sign}{net_pnl_sol:.5f} SOL", "inline": True},
            {"name": "Fees paid", "value": f"{fees_sol:.5f} SOL", "inline": True},
            {"name": "Balance now", "value": f"{balance_sol:.4f} SOL", "inline": True},
            {"name": "Equity", "value": f"{equity_sol:.4f} SOL", "inline": True},
        ]
        await self._post(
            {
                "title": "📉 Paper trade closed" if net_pnl_sol < 0 else "✅ Paper trade closed",
                "color": _COLOR_CLOSE_LOSS if net_pnl_sol < 0 else _COLOR_CLOSE_PROFIT,
                "fields": fields,
                "footer": {"text": DISCLAIMER},
            }
        )

    async def _post(self, embed: dict[str, object]) -> None:
        try:
            response = await self._client.post(self._url, json={"embeds": [embed]})
            if response.status_code >= 400:
                logger.warning(
                    "discord_trade_notify_failed",
                    extra={"context": {"status": response.status_code}},
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "discord_trade_notify_error",
                extra={"context": {"reason": f"{type(exc).__name__}: {exc}"}},
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def _sol_and_usd(sol: float | None, usd: float | None) -> str:
    if sol is None:
        return "—"
    if usd is None:
        return f"{sol:.4f} SOL"
    return f"{sol:.4f} SOL (${usd:,.2f})"
