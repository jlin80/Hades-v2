"""Health and status endpoints report measured state, never assumed state."""

import pytest
from fastapi.testclient import TestClient

from hades.api.routes import health as health_route
from hades.database.engine import DatabaseHealth

# Fields task.md §11 sketches for a later phase. Reporting them now — even as
# zeros — would be fabricated telemetry (task.md §20).
FUTURE_PHASE_FIELDS = (
    "tokens_discovered",
    "tokens_tracked",
    "snapshots_collected",
    "last_discovery_at",
    "last_snapshot_at",
    "providers",
)


def test_health_reports_unhealthy_when_database_is_unreachable(client: TestClient) -> None:
    """A degraded dependency must surface as 503, not a cheerful 200."""
    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


def test_status_reports_the_real_database_failure(client: TestClient) -> None:
    response = client.get("/status")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["database"]["connected"] is False
    assert body["database"]["latency_ms"] is None
    # The failure is named, not swallowed.
    assert body["database"]["error"]


def test_status_omits_capabilities_that_do_not_exist_yet(client: TestClient) -> None:
    body = client.get("/status").json()

    for field in FUTURE_PHASE_FIELDS:
        assert field not in body, f"{field} reported before its phase exists"

    assert body["phase"] == 0
    assert body["pending_capabilities"]


def test_health_reports_healthy_when_the_probe_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the success path's mapping from probe result to verdict."""

    async def _succeeding_probe(_engine: object) -> DatabaseHealth:
        return DatabaseHealth(connected=True, latency_ms=1.23, error=None)

    monkeypatch.setattr(health_route, "probe_database", _succeeding_probe)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_status_exposes_uptime_and_version(client: TestClient) -> None:
    body = client.get("/status").json()

    assert body["uptime_seconds"] >= 0
    assert body["version"]
    assert body["started_at"]


def test_status_never_leaks_the_database_password(client: TestClient) -> None:
    """The DSN password must not reach an HTTP response via an error string."""
    assert "secret" not in client.get("/status").text
