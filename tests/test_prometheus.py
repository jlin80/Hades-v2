"""The exposition format, and the honesty rules baked into it.

Two properties matter more than the field list: an endpoint a scraper hits every
15 seconds must never be able to load the database, and a number the process has
not measured must be absent rather than zero.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from hades.discovery.repository import DiscoveryStats
from hades.monitoring.prometheus import LoopState, render
from hades.monitoring.stats import StatsSnapshot
from hades.signals.repository import SignalStats
from hades.tracking.repository import TrackingStats

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def snapshot() -> StatsSnapshot:
    return StatsSnapshot(
        computed_at=NOW,
        duration_seconds=13.9,
        discovery=DiscoveryStats(
            total=55184,
            by_state={"DISCOVERED": 54000, "TRACKING": 40},
            with_created_at=55110,
            backfill_exhausted=54,
            last_discovered_at=NOW,
            median_discovery_latency_ms=1418.1,
        ),
        tracking=TrackingStats(
            tracking_now=40,
            eligible_waiting=1969,
            snapshots_total=100697,
            snapshots_last_hour=2595,
            stale_snapshots=80285,
            tokens_retired=1158,
            tokens_migrated=36,
            tokens_dead=0,
            oldest_due_seconds=None,
        ),
        signals=SignalStats(
            observations_total=173510,
            observations_last_hour=2000,
            signals_total=134,
            signals_last_hour=1,
            tokens_with_a_signal=82,
            signal_rate=0.00077,
            last_signal_at=NOW,
        ),
        outcomes={"outcomes_total": 520000, "outcomes_final": 519000, "observations_pending": 1000},
        signalled_final_count=133,
        portfolio=None,
    )


def body(**overrides: object) -> str:
    defaults: dict[str, object] = {
        "snapshot": snapshot(),
        "loop_status": {
            "discovery": LoopState(running=True, state="running", restarts=0),
            "tracking": LoopState(running=False, state="restarting", restarts=3),
        },
        "counters": {"discovery": {"ws_events": 483}},
        "database_connected": True,
        "database_latency_ms": 52.17,
        "uptime_seconds": 2772.0,
        "research_ready_threshold": 100,
    }
    defaults.update(overrides)
    return render(**defaults)  # type: ignore[arg-type]


def test_every_metric_declares_a_help_and_type_line() -> None:
    """Prometheus tolerates their absence; a human reading the endpoint does not."""
    rendered = body()
    names = {line.split()[0] for line in rendered.splitlines() if line and not line.startswith("#")}
    for name in names:
        bare = name.split("{")[0]
        assert f"# HELP {bare} " in rendered
        assert f"# TYPE {bare} gauge" in rendered


def test_counters_are_gauges_not_counters() -> None:
    """They reset on restart.

    Declaring a resetting series as a Prometheus counter makes rate() invent a
    spike at every restart, which is precisely when someone is looking at it.
    """
    assert "# TYPE hades_loop_counter gauge" in body()
    assert "counter\n" not in body().replace("hades_loop_counter", "")


def test_a_loop_in_backoff_is_not_reported_as_running() -> None:
    rendered = body()
    assert 'hades_loop_running{loop="tracking"} 0' in rendered
    assert 'hades_loop_state{loop="tracking"} 2' in rendered
    assert 'hades_loop_restarts{loop="tracking"} 3' in rendered


def test_a_loop_absent_from_the_status_map_still_exports_a_series() -> None:
    """A missing loop is a stopped loop, not a missing graph line."""
    rendered = body(loop_status={})
    assert 'hades_loop_running{loop="paper"} 0' in rendered
    assert 'hades_loop_state{loop="paper"} 0' in rendered


def test_aggregates_are_omitted_before_the_first_refresh_not_zeroed() -> None:
    """A zero looks like a measurement.

    Exporting zeros until the first refresh would put a false trough into every
    dashboard across every restart, which is worse than a short gap.
    """
    rendered = body(snapshot=None)
    assert "hades_tokens_total" not in rendered
    assert "hades_signals_total" not in rendered
    # The live, in-process metrics are still there.
    assert "hades_up 1" in rendered
    assert 'hades_loop_running{loop="discovery"} 1' in rendered


def test_the_age_of_the_cached_aggregates_is_exported() -> None:
    """Without it, a stuck refresher looks like a system that stopped changing."""
    assert "hades_stats_age_seconds" in body()
    assert "hades_stats_refresh_duration_seconds 13.900000" in body()


def test_metrics_endpoint_serves_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in response.headers["content-type"]
    assert "hades_up 1" in response.text


def test_metrics_endpoint_works_with_no_database(client: TestClient) -> None:
    """The `client` fixture has no PostgreSQL, so no refresh has ever succeeded.

    This is the case that matters for a scraper: the endpoint must answer
    quickly and truthfully while the database is unreachable, rather than
    blocking on it or 500ing -- an outage is when the metrics are most wanted.
    """
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "hades_database_connected 0" in response.text
    assert "hades_tokens_total" not in response.text
