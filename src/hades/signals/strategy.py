"""The strategy interface.

Spec §12 asks for exactly this shape and nothing more:

    class Strategy(Protocol):
        name: str
        async def evaluate(self, market_state: MarketState) -> Signal | None: ...

One method. No lifecycle hooks, no registry, no plugin loader, no base class with
template methods. Hades V1 shipped fifteen strategies behind a seven-method
interface with a weighted ensemble and a dynamic weight engine, and never
established that any single one of them had an edge.

There is one strategy. When there is evidence for a second, the interface can
grow to fit it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hades.signals.models import MarketState, Signal


@runtime_checkable
class Strategy(Protocol):
    """Evaluates a market state and may emit a research signal."""

    name: str
    version: str

    async def evaluate(self, market_state: MarketState) -> Signal | None:
        """Return a Signal if the hypothesis holds, else None.

        Must be pure with respect to the market state: no I/O, no clock, no
        database. A strategy that could fetch something could fetch something
        from after the decision point.
        """
        ...
