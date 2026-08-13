"""Token validation (task.md §10). Nothing invalid reaches the database."""

from datetime import timedelta

from hades.clock import utc_now
from hades.discovery.models import DiscoveredToken
from hades.discovery.validation import RejectionReason, validate_batch, validate_token

VALID_ADDRESS = "9BsHRRVeCkKhLcTStBnUcHBmqssJgcbEcphgcopump"


def make_token(**overrides: object) -> DiscoveredToken:
    defaults: dict[str, object] = {
        "token_address": VALID_ADDRESS,
        "symbol": "TRENCHTOK",
        "name": None,
        "pool_address": None,
        "first_seen_at": None,
        "observed_at": utc_now(),
        "provider_name": "geckoterminal",
        "raw": {},
    }
    defaults.update(overrides)
    return DiscoveredToken(**defaults)  # type: ignore[arg-type]


def test_a_sound_token_passes() -> None:
    assert validate_token(make_token()) is None


def test_empty_address_is_rejected() -> None:
    rejection = validate_token(make_token(token_address=""))
    assert rejection is not None
    assert rejection.reason is RejectionReason.MISSING_ADDRESS


def test_non_base58_address_is_rejected() -> None:
    """An EVM address must never enter a Solana dataset."""
    evm_address = "0x311935Cd80B76769bF2ecC9D8Ab7635b2139cf82"
    rejection = validate_token(make_token(token_address=evm_address))
    assert rejection is not None
    assert rejection.reason is RejectionReason.INVALID_ADDRESS


def test_address_with_ambiguous_base58_characters_is_rejected() -> None:
    """0, O, I and l are not in the base58 alphabet."""
    rejection = validate_token(make_token(token_address="0OIl" + VALID_ADDRESS[4:]))
    assert rejection is not None
    assert rejection.reason is RejectionReason.INVALID_ADDRESS


def test_too_short_address_is_rejected() -> None:
    rejection = validate_token(make_token(token_address="abc"))
    assert rejection is not None
    assert rejection.reason is RejectionReason.INVALID_ADDRESS


def test_future_timestamp_is_rejected() -> None:
    """A pool cannot have been created an hour from now."""
    rejection = validate_token(make_token(first_seen_at=utc_now() + timedelta(hours=1)))
    assert rejection is not None
    assert rejection.reason is RejectionReason.TIMESTAMP_IN_FUTURE


def test_naive_timestamp_is_rejected() -> None:
    """A naive timestamp means a timezone was lost upstream (task.md §14)."""
    from datetime import datetime

    rejection = validate_token(
        make_token(first_seen_at=datetime(2026, 8, 13, 12, 0, 0))  # noqa: DTZ001 — naive on purpose
    )
    assert rejection is not None
    assert rejection.reason is RejectionReason.TIMESTAMP_NOT_UTC


def test_oversized_symbol_is_rejected() -> None:
    rejection = validate_token(make_token(symbol="X" * 200))
    assert rejection is not None
    assert rejection.reason is RejectionReason.SYMBOL_TOO_LONG


def test_batch_separates_valid_from_rejected_and_records_reasons() -> None:
    result = validate_batch(
        [make_token(), make_token(token_address="not-an-address"), make_token(token_address="")]
    )

    assert len(result.valid) == 1
    assert len(result.rejections) == 2
    # Every rejection carries a machine-readable reason for Phase 4 reporting.
    assert {r.reason for r in result.rejections} == {
        RejectionReason.INVALID_ADDRESS,
        RejectionReason.MISSING_ADDRESS,
    }


def test_duplicates_within_a_batch_are_collapsed() -> None:
    """Providers report the same token across pools; only one row should result."""
    result = validate_batch([make_token(), make_token(), make_token()])
    assert len(result.valid) == 1
