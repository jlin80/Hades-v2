"""Signal research (Phase 5).

One hypothesis, evaluated against real data, generating research signals and
**never** an order. Spec §12: the hypothesis is configurable and is not
presented as truth.
"""

from hades.signals.models import Condition, MarketState, Signal
from hades.signals.strategy import Strategy

__all__ = ["Condition", "MarketState", "Signal", "Strategy"]
