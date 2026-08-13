"""Discovery orchestration: fallback order and honest provider health."""

from typing import Any

import pytest

from hades.clock import utc_now
from hades.discovery import service as service_module
from hades.discovery.errors import ProviderError
from hades.discovery.models import DiscoveredToken
from hades.discovery.service import DiscoveryService

VALID_ADDRESS = "9BsHRRVeCkKhLcTStBnUcHBmqssJgcbEcphgcopump"
OTHER_ADDRESS = "EwAmHqXTzWsHdKZSCengDu15SM6ZyurDd2mrZhLBpump"


class StubProvider:
    """A provider that returns tokens or raises, on command."""

    def __init__(
        self,
        name: str,
        tokens: list[DiscoveredToken] | None = None,
        error: ProviderError | None = None,
    ) -> None:
        self._name = name
        self._tokens = tokens or []
        self._error = error
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def discover_tokens(self) -> list[DiscoveredToken]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._tokens


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


def fake_session_factory() -> _FakeSession:
    return _FakeSession()


def token(address: str = VALID_ADDRESS, provider: str = "primary") -> DiscoveredToken:
    return DiscoveredToken(
        token_address=address,
        symbol="SYM",
        name=None,
        pool_address=None,
        first_seen_at=None,
        observed_at=utc_now(),
        provider_name=provider,
        raw={},
    )


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch: pytest.MonkeyPatch) -> list[list[DiscoveredToken]]:
    """Capture what would be persisted, without touching a database."""
    persisted: list[list[DiscoveredToken]] = []

    async def _insert(_session: object, tokens: list[DiscoveredToken]) -> int:
        persisted.append(tokens)
        return len(tokens)

    monkeypatch.setattr(service_module, "insert_new_tokens", _insert)
    return persisted


def make_service(*providers: StubProvider) -> DiscoveryService:
    return DiscoveryService(
        providers=list(providers),
        session_factory=fake_session_factory,  # type: ignore[arg-type]
    )


async def test_provider_health_starts_unknown_not_healthy() -> None:
    """task.md §20: never mark a provider healthy without checking it."""
    service = make_service(StubProvider("primary"))
    assert service.health["primary"].status == "unknown"
    assert service.last_run is None


async def test_successful_primary_run_skips_the_fallback() -> None:
    primary = StubProvider("primary", tokens=[token()])
    fallback = StubProvider("fallback", tokens=[token(OTHER_ADDRESS)])

    run = await make_service(primary, fallback).run_once()

    assert run.provider_name == "primary"
    assert run.inserted == 1
    assert fallback.calls == 0, "the fallback must only run when the primary fails"


async def test_fallback_is_used_when_the_primary_fails() -> None:
    primary = StubProvider(
        "primary",
        error=ProviderError(
            provider="primary",
            endpoint="/new_pools",
            error_type="ConnectError",
            message="host does not resolve",
        ),
    )
    fallback = StubProvider("fallback", tokens=[token(OTHER_ADDRESS, "fallback")])

    service = make_service(primary, fallback)
    run = await service.run_once()

    assert run.provider_name == "fallback"
    assert run.inserted == 1
    assert service.health["primary"].status == "failed"
    assert service.health["fallback"].status == "healthy"


async def test_all_providers_failing_is_recorded_not_hidden() -> None:
    def failing(name: str) -> StubProvider:
        return StubProvider(
            name,
            error=ProviderError(
                provider=name, endpoint="/x", error_type="ConnectError", message="down"
            ),
        )

    service = make_service(failing("primary"), failing("fallback"))
    run = await service.run_once()

    assert run.succeeded is False
    assert run.error == "all discovery providers failed"
    assert run.inserted == 0
    assert all(h.status == "failed" for h in service.health.values())


async def test_consecutive_failures_are_counted() -> None:
    service = make_service(
        StubProvider(
            "primary",
            error=ProviderError(
                provider="primary", endpoint="/x", error_type="Timeout", message="slow"
            ),
        )
    )
    await service.run_once()
    await service.run_once()

    assert service.health["primary"].consecutive_failures == 2


async def test_recovery_clears_the_failure_state() -> None:
    provider = StubProvider(
        "primary",
        error=ProviderError(
            provider="primary", endpoint="/x", error_type="Timeout", message="slow"
        ),
    )
    service = make_service(provider)
    await service.run_once()

    provider._error = None
    provider._tokens = [token()]
    await service.run_once()

    assert service.health["primary"].status == "healthy"
    assert service.health["primary"].consecutive_failures == 0
    assert service.health["primary"].last_error is None


async def test_invalid_tokens_are_rejected_before_persistence(
    _stub_persistence: list[list[DiscoveredToken]],
) -> None:
    provider = StubProvider("primary", tokens=[token(), token("0xdeadbeef")])

    run = await make_service(provider).run_once()

    assert run.fetched == 2
    assert run.valid == 1
    assert run.rejected == 1
    assert [t.token_address for t in _stub_persistence[0]] == [VALID_ADDRESS]


async def test_no_enabled_provider_is_reported_rather_than_silently_idle() -> None:
    run = await make_service().run_once()

    assert run.succeeded is False
    assert run.error == "no discovery provider is enabled"
