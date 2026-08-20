"""Configuration.

One settings object, loaded from the environment (and ``.env`` in development).
Everything is explicit and typed; there are no implicit defaults for anything
that would change what the system *does* — only for what it *is*.

Phase 0 deliberately has no provider, tracking or strategy settings. Those
arrive with the phase that needs them, per the anti-over-engineering rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["local", "homelab", "test"]


class Settings(BaseSettings):
    """Runtime configuration for the whole process."""

    model_config = SettingsConfigDict(
        env_prefix="HADES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "local"
    log_level: LogLevel = "INFO"

    # PostgreSQL is the single source of truth. The async driver is mandatory:
    # a sync URL here would silently give us a blocking driver inside the
    # event loop, which is the exact failure mode that stalled Hades V1.
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://hades:hades@localhost:5432/hades"),
    )
    # Sized by an outage rather than by taste. On CT202 (2 cores, vfs, a host
    # already at load 5-6) all six background loops open connections within a
    # second of each other at startup; a pool of 5+5 could not seat them and the
    # ones that waited blew a 5s connect timeout 11 seconds after boot. Four of
    # the five collectors died at once, and with no restart that made a merely
    # slow startup permanent -- the system then sat idle for 7h20m.
    #
    # 10+10 seats every loop with room for /status and the heartbeat, and 30s is
    # chosen to outlast a slow host rather than to be comfortable: failing to
    # connect is a real outage, while waiting 30s once at boot costs nothing.
    database_pool_size: int = Field(default=10, ge=1, le=50)
    database_pool_max_overflow: int = Field(default=10, ge=0, le=50)
    database_connect_timeout_seconds: float = Field(default=30.0, gt=0)

    # Container-internal bind; what is actually exposed is a compose concern.
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # --- Discovery (Phase 2) -------------------------------------------------
    # Off by default: starting the API must never start writing to the database
    # as a side effect. The operator turns collection on deliberately.
    discovery_enabled: bool = False
    # The WebSocket is the primary discovery path; polling only backfills what a
    # disconnect lost. 60s is not a throughput lever — Hades V1 raised its poll
    # interval 12x and its intake did not fall, because the rate at which tokens
    # appear on Solana is not set by our clock.
    discovery_poll_interval_seconds: float = Field(default=60.0, gt=0)
    discovery_poll_limit: int = Field(default=50, ge=1, le=200)
    # Tokens per pass whose missing created_at we chase. Deferred rather than
    # fetched on arrival: measured live, 49 of 51 immediate fetches 404'd
    # because the socket announces a mint before pump.fun indexes it.
    discovery_backfill_limit: int = Field(default=50, ge=0, le=200)
    # Attempts before we stop asking about a given mint. Some tokens reach the
    # WebSocket but never enter pump.fun's index at all (external launchpads),
    # and without a budget they occupy the queue forever.
    discovery_backfill_max_attempts: int = Field(default=5, ge=1, le=50)

    # --- Tracking (Phase 3) --------------------------------------------------
    tracking_enabled: bool = False
    # The binding constraint, and it is not a preference. Measured: the primary
    # sustains 1.64 req/s while Pump.fun creates 0.24-0.55 tokens/s, and the
    # spec's schedule costs 434 snapshots per token per day -- tracking every
    # token would need 104-239 req/s, 64x to 146x over capacity. So the system
    # tracks a bounded sample. 40 concurrent tokens over a 1h horizon works out
    # to roughly 1.2 req/s; `/status` reports the estimate so the arithmetic is
    # visible rather than folded into a magic number.
    tracking_max_concurrent: int = Field(default=40, ge=1, le=5000)
    tracking_batch_size: int = Field(default=20, ge=1, le=200)
    tracking_pass_interval_seconds: float = Field(default=2.0, gt=0)
    # Spacing inside a batch. The primary's limit is unpublished and is the
    # largest technical risk in the project; a batch fired at once is the shape
    # most likely to find the ceiling.
    tracking_request_spacing_seconds: float = Field(default=0.2, ge=0)
    # Spec §13: data older than this is stale. Recorded on the snapshot rather
    # than applied at read time, so changing the threshold cannot rewrite what
    # was already observed.
    tracking_stale_after_seconds: float = Field(default=60.0, gt=0)
    tracking_max_snapshot_failures: int = Field(default=3, ge=1, le=20)
    tracking_failure_retry_seconds: float = Field(default=30.0, gt=0)

    # Schedule (spec §8). Exact values configurable, as required.
    early_tracking_seconds: float = Field(default=300.0, gt=0)
    early_snapshot_interval_seconds: float = Field(default=10.0, gt=0)
    medium_tracking_seconds: float = Field(default=1800.0, gt=0)
    medium_snapshot_interval_seconds: float = Field(default=30.0, gt=0)
    normal_tracking_seconds: float = Field(default=7200.0, gt=0)
    normal_snapshot_interval_seconds: float = Field(default=60.0, gt=0)
    long_term_snapshot_interval_seconds: float = Field(default=300.0, gt=0)
    # One hour: covers every outcome horizon spec §15 asks for (return_1m
    # through return_1h) at 110 snapshots per token, which the budget affords
    # for ~785 tokens/day. A 24h horizon costs 4x per token and buys nothing
    # §15 currently asks for.
    tracking_retire_after_seconds: float = Field(default=3600.0, gt=0)

    # --- Signal research (Phase 5) -------------------------------------------
    # Research signals only. Nothing downstream of this executes anything, and
    # nothing here asserts the hypothesis is profitable.
    signals_enabled: bool = False
    signal_batch_size: int = Field(default=25, ge=1, le=200)
    signal_pass_interval_seconds: float = Field(default=5.0, gt=0)
    signal_series_lookback_seconds: float = Field(default=300.0, gt=0)
    # 0 means store a vector for every new snapshot, which is the research
    # ideal. At 40 tracked tokens that is roughly 100k rows/day at ~1 KB each --
    # ~100 MB/day, enough to fill a homelab rootfs in months. Raise it to thin
    # the dataset deliberately rather than discovering the limit as an outage.
    signal_observation_min_interval_seconds: float = Field(default=0.0, ge=0)

    # EARLY MOMENTUM thresholds (spec §12). Every one of these is a plausible
    # starting point, NOT a value derived from evidence -- there is no evidence
    # yet, and producing it is what Phase 7 is for. The hypothesis may well have
    # no edge.
    signal_window: str = "30s"
    signal_min_token_age_seconds: float = Field(default=30.0, ge=0)
    signal_max_token_age_seconds: float = Field(default=300.0, gt=0)
    signal_min_market_cap_velocity: float = 0.05
    signal_min_market_cap_acceleration: float = 0.0
    signal_min_liquidity_velocity: float = 0.0
    signal_min_price_movement_ratio: float = Field(default=0.5, ge=0, le=1)
    signal_max_seconds_since_last_trade: float = Field(default=30.0, gt=0)
    signal_min_liquidity_sol: float = Field(default=1.0, ge=0)
    signal_min_observations: float = Field(default=3.0, ge=2)
    signal_max_freshness_seconds: float = Field(default=30.0, gt=0)

    # --- Risk engine (Phase 6, spec §13) --------------------------------------
    # All configurable, none validated by evidence yet -- see RiskLimits.
    risk_max_token_age_seconds: float = Field(default=300.0, gt=0)
    risk_min_liquidity_sol: float = Field(default=1.0, ge=0)
    risk_max_slippage_fraction: float = Field(default=0.05, gt=0, le=1)
    risk_max_position_sol: float = Field(default=0.05, gt=0)
    risk_max_open_positions: int = Field(default=5, ge=1)
    # Not in §13. Required by measurement: Phase 5's live run produced four
    # signals on the same token inside a minute, correct for research and
    # ruinous for position sizing.
    risk_max_open_per_token: int = Field(default=1, ge=1)
    risk_max_daily_loss_sol: float = Field(default=0.5, gt=0)
    risk_max_drawdown_fraction: float = Field(default=0.25, gt=0, le=1)
    risk_max_data_age_seconds: float = Field(default=30.0, gt=0)

    # --- Exit rules (Phase 6, spec §14) ---------------------------------------
    exit_take_profit_fraction: float = Field(default=0.30, gt=0)
    exit_stop_loss_fraction: float = Field(default=0.20, gt=0)
    exit_trailing_stop_fraction: float = Field(default=0.15, gt=0)
    exit_trailing_arm_fraction: float = Field(default=0.15, gt=0)
    exit_max_hold_seconds: float = Field(default=600.0, gt=0)

    # --- Paper trading (Phase 6) -----------------------------------------------
    # Simulated fills only. No signer, no wallet, no RPC exists anywhere in this
    # codebase -- see docs/SAFETY.md and the AST scan in tests/test_safety.py.
    paper_trading_enabled: bool = False
    paper_starting_balance_sol: float = Field(default=1.0, gt=0)
    paper_position_size_sol: float = Field(default=0.02, gt=0)
    paper_fee_rate: float = Field(default=0.01, ge=0, lt=1)
    # Modelled delay between a decision and the order reaching the chain
    # (spec §14). The fill uses the first snapshot at or after this, never the
    # decision's own snapshot -- that would hand the simulator a price nobody
    # could have traded at.
    paper_latency_seconds: float = Field(default=2.0, ge=0)
    paper_pass_interval_seconds: float = Field(default=3.0, gt=0)
    paper_batch_size: int = Field(default=25, ge=1, le=200)

    # --- Outcome engine (Phase 7, spec §15-16) --------------------------------
    # Off by default, same reasoning as every other writer: starting the API
    # must never start writing to the database as a side effect.
    outcomes_enabled: bool = False
    outcomes_batch_size: int = Field(default=200, ge=1, le=5000)
    outcomes_pass_interval_seconds: float = Field(default=30.0, gt=0)

    # Optional. None means no notifier is built at all -- a missing webhook is
    # not a degraded feature, it is simply absent, matching how every other
    # optional integration in this codebase behaves.
    discord_webhook_url: str | None = Field(default=None, repr=False)
    # 0 disables the heartbeat even if a webhook is configured -- trade
    # notifications and the heartbeat are independent features.
    discord_status_interval_seconds: float = Field(default=3600.0, ge=0)
    # Finalised, signalled outcomes needed before the one-off @everyone ping
    # that says "enough evidence exists to run scripts/research_report.py".
    # Checked on the same clock as the heartbeat, so it fires at most once per
    # discord_status_interval_seconds, and never more than once per process
    # lifetime once it has fired.
    research_ready_threshold: int = Field(default=100, ge=1)

    provider_timeout_seconds: float = Field(default=10.0, gt=0)
    provider_max_attempts: int = Field(default=3, ge=1, le=10)
    provider_max_connections: int = Field(default=10, ge=1, le=100)

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            msg = (
                f"database_url must use the postgresql+asyncpg driver, got {value.scheme!r}. "
                "A sync driver would block the event loop."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _tiers_must_be_ordered(self) -> Settings:
        """Tier boundaries must increase, or a tier silently never applies.

        A MEDIUM boundary below the EARLY one does not error anywhere: it just
        means no token is ever in the medium tier, and the dataset quietly has
        a sampling rate nobody chose.
        """
        if not (
            self.early_tracking_seconds
            < self.medium_tracking_seconds
            < self.normal_tracking_seconds
        ):
            msg = (
                "tracking tier boundaries must increase: "
                f"early={self.early_tracking_seconds} < "
                f"medium={self.medium_tracking_seconds} < "
                f"normal={self.normal_tracking_seconds}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _signal_window_must_exist(self) -> Settings:
        """The window suffix must name one the feature engine actually computes.

        A typo here would make every windowed condition read a missing feature
        and fail forever — and the hypothesis would look like one that never
        triggers rather than like a configuration error.
        """
        from hades.features.engine import FeatureWindows

        available = {f"{int(s)}s" if s == int(s) else f"{s}s" for s in FeatureWindows().seconds}
        if self.signal_window not in available:
            msg = f"signal_window {self.signal_window!r} is not computed; available: {available}"
            raise ValueError(msg)
        return self

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


def load_settings() -> Settings:
    """Build a Settings instance from the environment.

    Not cached: caching a frozen global makes tests lie to each other. Callers
    that need it repeatedly hold it on the application state instead.
    """
    return Settings()
