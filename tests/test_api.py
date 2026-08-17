"""Endpoint behaviour with PostgreSQL unreachable — the default fixture state."""

from __future__ import annotations

from fastapi.testclient import TestClient

from hades import __version__
from hades.api.app import create_app
from hades.api.routes import NOT_IMPLEMENTED_METRICS
from hades.config import Settings


def test_health_reports_degraded_when_database_is_down(client: TestClient) -> None:
    """An outage must be reported, not crashed on.

    The process staying up is the point: a crash loop cannot serve the
    endpoint that would explain why it is crashing.
    """
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"]["connected"] is False
    assert body["database"]["error"]
    assert body["database"]["latency_ms"] is None


def test_health_reports_the_real_version(client: TestClient) -> None:
    """Version drift bit V1: pyproject said 0.10.0 while the API served 0.3.0."""
    assert client.get("/health").json()["version"] == __version__


def test_paper_mode_is_not_a_runtime_choice(client: TestClient) -> None:
    """There is no live path to switch to. The schema makes that a type."""
    body = client.get("/health").json()
    assert body["trading_mode"] == "paper"
    assert body["is_live"] is False


def test_status_returns_null_counters_not_zero_when_database_is_down(
    client: TestClient,
) -> None:
    """`0` would read as "no tokens discovered". The truth is "we cannot know"."""
    body = client.get("/status").json()
    assert body["status"] == "degraded"
    assert body["tokens_discovered"] is None
    assert body["tokens_tracking"] is None


def test_status_declares_which_metrics_do_not_exist_yet(client: TestClient) -> None:
    body = client.get("/status").json()
    assert body["phase"] == 3
    assert set(body["not_implemented"]) == set(NOT_IMPLEMENTED_METRICS)


def test_tracking_publishes_its_own_capacity_arithmetic(client: TestClient) -> None:
    """The budget has to be inspectable, not folded into a constant.

    Tracking every token would need 64x-146x the primary's measured capacity,
    so the concurrency limit *is* the design. Surfacing the implied request
    rate is what makes raising that limit a visible decision.
    """
    tracking = client.get("/status").json()["tracking"]
    assert tracking["enabled"] is False
    assert tracking["running"] is False
    assert tracking["max_concurrent"] == 40
    assert tracking["retire_after_seconds"] == 3600.0
    assert tracking["snapshots_per_token"] == 110.0
    assert tracking["estimated_requests_per_second"] < 1.64


def test_tracking_counters_are_null_not_zero_when_the_database_is_down(
    client: TestClient,
) -> None:
    body = client.get("/status").json()["tracking"]
    assert body["snapshots_total"] is None
    assert body["eligible_waiting"] is None


def test_discovery_reports_configured_and_running_separately(client: TestClient) -> None:
    """ "Configured" and "actually running" are different facts.

    The default fixture has discovery off, so both are false. The pair exists
    because V1's dashboard could not tell a working collector from a dead one.
    """
    discovery = client.get("/status").json()["discovery"]
    assert discovery["enabled"] is False
    assert discovery["running"] is False
    assert discovery["last_error"] is None


def test_discovery_does_not_start_when_the_database_is_unreachable(
    settings: Settings,
) -> None:
    """Enabled plus no database must not start a writer.

    It would spend the primary's rate limit producing nothing, and report
    itself as running while doing it.
    """
    enabled = settings.model_copy(update={"discovery_enabled": True})
    with TestClient(create_app(enabled)) as client:
        discovery = client.get("/status").json()["discovery"]
        assert discovery["enabled"] is True
        assert discovery["running"] is False


def test_status_reports_uptime(client: TestClient) -> None:
    body = client.get("/status").json()
    assert body["uptime_seconds"] >= 0


def test_status_never_reports_a_metric_it_cannot_measure(client: TestClient) -> None:
    """The unbuilt counters must be absent from the payload, not present as 0."""
    body = client.get("/status").json()
    for metric in NOT_IMPLEMENTED_METRICS:
        assert metric not in body
