"""Domain rules, tested with no database, no network, no framework."""

from datetime import datetime, timedelta, timezone

from src.domain.entities import OAuthTokens, SyncBookmark


def test_lookback_uses_default_on_first_ever_sync():
    bookmark = SyncBookmark(connection_id=1)
    assert bookmark.lookback_days(default_days=30) == 30


def test_lookback_uses_real_elapsed_gap_after_a_previous_sync():
    now = datetime.now(timezone.utc)
    bookmark = SyncBookmark(connection_id=1, last_synced_at=now - timedelta(days=9))

    # Nine days idle should search nine days, not the 30-day default.
    assert bookmark.lookback_days(default_days=30, now=now) == 9


def test_lookback_never_returns_zero_days():
    now = datetime.now(timezone.utc)
    bookmark = SyncBookmark(connection_id=1, last_synced_at=now - timedelta(minutes=5))

    # A zero-day window would search nothing and silently skip recent mail.
    assert bookmark.lookback_days(default_days=30, now=now) == 1


def test_tokens_without_expiry_are_not_expired():
    assert OAuthTokens("t", "r", None, "scope").is_expired() is False


def test_token_expiry_is_detected():
    now = datetime.now(timezone.utc)
    assert OAuthTokens("t", "r", now - timedelta(minutes=1), "s").is_expired(now) is True
    assert OAuthTokens("t", "r", now + timedelta(minutes=1), "s").is_expired(now) is False
