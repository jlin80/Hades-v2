"""What a strategy sees, and what it may say.

A strategy gets a ``MarketState`` and returns a ``Signal`` or None. It gets no
database, no clock and no provider — so a strategy cannot look anything up, and
therefore cannot look up the future.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Condition:
    """One clause of a hypothesis, and whether it held.

    Recorded whether it passed or failed. A signal that only says "fired" is
    unanalysable: §17 asks how results change with token age, liquidity and
    activity, and answering that needs to know *which* clause was binding.
    """

    name: str
    passed: bool
    value: float | None
    threshold: float | None
    description: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "value": self.value,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class MarketState:
    """Everything a strategy is allowed to know at one instant.

    The feature vector is already truncated to ``as_of`` by the engine, so a
    strategy physically cannot see a later observation. That is the Phase 4
    guarantee carried forward rather than restated.
    """

    token_address: str
    as_of: datetime
    feature_version: str
    features: dict[str, float | None]

    def feature(self, name: str) -> float | None:
        """Read a feature. Unknown names raise rather than silently return None.

        A typo'd feature name that returns None would make its condition fail
        forever, and the strategy would look like a hypothesis that never
        triggers rather than like a bug.
        """
        if name not in self.features:
            msg = f"unknown feature {name!r}; available: {sorted(self.features)}"
            raise KeyError(msg)
        return self.features[name]


@dataclass(frozen=True, slots=True)
class Signal:
    """A research signal. Not an order, not a recommendation.

    Spec §12: this generates a *research* signal, and nothing here asserts it is
    profitable. Whether these have positive expectancy after slippage, fees and
    risk is the question the system exists to answer — Phase 7 answers it, and
    the answer may well be no.
    """

    token_address: str
    strategy: str
    strategy_version: str
    created_at: datetime
    conditions: tuple[Condition, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> tuple[Condition, ...]:
        return tuple(c for c in self.conditions if c.passed)

    @property
    def failed(self) -> tuple[Condition, ...]:
        return tuple(c for c in self.conditions if not c.passed)

    def as_dict(self) -> dict[str, object]:
        return {
            "token_address": self.token_address,
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "created_at": self.created_at.isoformat(),
            "conditions": [c.as_dict() for c in self.conditions],
        }
