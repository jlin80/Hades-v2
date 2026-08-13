"""The discovery provider contract.

Deliberately one method. task.md §6 warns against a twenty-method abstraction
built for capabilities we do not yet need: Phase 1 discovers tokens, so the
protocol discovers tokens. Market snapshots add their own method in Phase 2,
when there is a real implementation to shape it.
"""

from typing import Protocol

from hades.discovery.models import DiscoveredToken


class TokenDiscoveryProvider(Protocol):
    """A source of newly observed Solana tokens."""

    @property
    def name(self) -> str:
        """Stable identifier used in logs, errors and stored rows."""
        ...

    async def discover_tokens(self) -> list[DiscoveredToken]:
        """Return tokens the provider currently reports as new.

        Raises:
            ProviderError: on transport failure, bad status, or exhausted
                retries.
            ProviderSchemaError: when the response shape is not what we parse.
        """
        ...
