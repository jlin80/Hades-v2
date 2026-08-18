"""Paper execution (Phase 6).

Simulated fills only. There is no signer, no wallet and no transaction builder
anywhere in this package — see ``docs/SAFETY.md``, and the AST scan in
``tests/test_safety.py`` that fails the build if one appears.
"""

from hades.paper.curve import BuyFill, SellFill, simulate_buy, simulate_sell

__all__ = ["BuyFill", "SellFill", "simulate_buy", "simulate_sell"]
