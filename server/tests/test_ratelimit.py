"""The shared rate limiter: counted in Postgres, so every web pod sees the same totals."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from iki_server.core.models import RateLimitCounter
from iki_server.web.ratelimit import RateLimiter

pytestmark = pytest.mark.django_db


def test_counts_are_shared_between_processes() -> None:
    # Two pods each build their own limiter object; the counts are the same row.
    pod_a, pod_b = RateLimiter("sign_in", 3), RateLimiter("sign_in", 3)
    assert [pod_a.allow("1.2.3.4"), pod_b.allow("1.2.3.4"), pod_a.allow("1.2.3.4")] == [True] * 3
    assert not pod_b.allow("1.2.3.4")
    assert pod_a.allow("5.6.7.8")  # other clients are unaffected
    assert RateLimitCounter.objects.get(key="sign_in:1.2.3.4").count == 4


def at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)


def test_limits_reset_each_minute_and_say_when(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = RateLimiter("api", 2)
    at(monkeypatch, datetime(2026, 11, 9, 12, 0, 30, tzinfo=UTC))
    first = limiter.check("user-1")
    assert (first.allowed, first.limit, first.remaining) == (True, 2, 1)
    limiter.check("user-1")
    refused = limiter.check("user-1")
    assert (refused.allowed, refused.remaining) == (False, 0)
    assert refused.reset_at == datetime(2026, 11, 9, 12, 1, tzinfo=UTC)
    assert refused.retry_after == 31
    at(monkeypatch, datetime(2026, 11, 9, 12, 1, 0, tzinfo=UTC))
    assert limiter.check("user-1").allowed


def test_scopes_are_independent_and_long_keys_are_hashed() -> None:
    RateLimiter("a", 1).check("k")
    assert RateLimiter("b", 1).allow("k")
    RateLimiter("a", 1).check("x" * 500)
    key = RateLimitCounter.objects.exclude(key__in=["a:k", "b:k"]).get().key
    assert key.startswith("a:sha256:")
    assert len(key) < 200


def test_reset_clears_only_its_scope() -> None:
    RateLimiter("a", 1).check("k")
    RateLimiter("b", 1).check("k")
    RateLimiter("a", 1).reset()
    assert list(RateLimitCounter.objects.values_list("key", flat=True)) == ["b:k"]


def test_the_counter_table_is_unlogged() -> None:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT relpersistence FROM pg_class WHERE relname = 'iki_rate_limit'")
        assert cursor.fetchone()[0] == "u"


def test_windows_are_kept_for_an_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.web.ratelimit import prune

    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    for minutes in (30, 59, 61, 120):
        RateLimitCounter.objects.create(
            key=f"t:{minutes}", window_start=now - timedelta(minutes=minutes), count=1
        )
    at(monkeypatch, now)
    assert prune() == 2
    assert sorted(RateLimitCounter.objects.values_list("key", flat=True)) == ["t:30", "t:59"]
