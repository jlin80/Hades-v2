"""Normalization and validation at the provider boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest
from pydantic import ValidationError

from hades.providers.models import DiscoveredToken, epoch_ms_to_datetime

VALID_MINT = "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump"
VALID_WALLET = "8i6qTrvQZ2c66GdPb8CgQh599CAMUGJPWqVWMwbtjGYf"


def test_valid_token_is_accepted() -> None:
    token = DiscoveredToken(token_address=VALID_MINT, source="pumpfun")
    assert token.token_address == VALID_MINT


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "tooshort",
        "0OIl" * 10,  # base58 excludes 0, O, I and l
        "nHxKqPLgixPc5BFF1PJsZt6YQJYKgYKGfPgiXCBpump!",
        "a" * 45,
    ],
)
def test_malformed_mint_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        DiscoveredToken(token_address=bad, source="pumpfun")


def test_blank_symbol_becomes_none_not_empty_string() -> None:
    """An empty string is a missing value and must be stored as one.

    Spec §9: if a metric is unavailable, NULL. `""` would later read as a token
    whose symbol is genuinely blank.
    """
    token = DiscoveredToken(token_address=VALID_MINT, source="pumpfun", symbol="  ", name="")
    assert token.symbol is None
    assert token.name is None


def test_bad_creator_is_dropped_not_fatal() -> None:
    """Losing one attribute beats losing the whole observation."""
    token = DiscoveredToken(
        token_address=VALID_MINT, source="pumpportal", creator_address="not-base58!"
    )
    assert token.creator_address is None
    assert token.token_address == VALID_MINT


def test_naive_created_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DiscoveredToken(
            token_address=VALID_MINT, source="pumpfun", created_at=datetime(2026, 8, 17, 12, 0)
        )


def test_created_at_is_normalized_to_utc() -> None:
    other = timezone.utc  # noqa: UP017 — explicit, to show the conversion happens
    token = DiscoveredToken(
        token_address=VALID_MINT,
        source="pumpfun",
        created_at=datetime(2026, 8, 17, 12, 0, tzinfo=other),
    )
    assert token.created_at is not None
    assert token.created_at.tzinfo == UTC


def test_token_is_frozen() -> None:
    token = DiscoveredToken(token_address=VALID_MINT, source="pumpfun")
    with pytest.raises(ValidationError):
        token.symbol = "NOPE"  # type: ignore[misc]


def test_unknown_field_is_rejected() -> None:
    """extra='forbid': a renamed provider field must fail, not be ignored."""
    with pytest.raises(ValidationError):
        DiscoveredToken(token_address=VALID_MINT, source="pumpfun", marketCap=123)  # type: ignore[call-arg]


class TestEpochConversion:
    """pump.fun sends created_timestamp in ms and updated_at in seconds."""

    def test_milliseconds_convert(self) -> None:
        assert epoch_ms_to_datetime(1786973934000, field="t") == datetime(
            2026, 8, 17, 13, 38, 54, tzinfo=UTC
        )

    def test_seconds_are_rejected_not_silently_misread(self) -> None:
        """The real trap: 1786973934 as ms is 1926-08-19, a token aged 100 years.

        Guessing the unit by magnitude would work until it didn't. Refusing is
        the only option that cannot produce a plausible wrong answer.
        """
        with pytest.raises(ValueError, match="wrong unit"):
            epoch_ms_to_datetime(1786973934, field="updated_at")

    def test_zero_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="plausible"):
            epoch_ms_to_datetime(0, field="t")
