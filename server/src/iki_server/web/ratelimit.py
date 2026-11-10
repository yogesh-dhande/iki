"""Rate limits shared by every web pod, counted in Postgres.

Each limit counts requests per key (usually a client address or a
principal) in fixed one-minute windows. One atomic UPSERT counts a request
and returns the window's total, so concurrent pods never undercount.
``iki-server maintain`` prunes old windows.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection
from django.http import HttpRequest
from django.utils import timezone

from iki_server.core.models import RateLimitCounter

WINDOW = timedelta(minutes=1)
#: Windows older than this are pruned.
RETENTION = timedelta(hours=1)
_MAX_KEY = 200


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: datetime

    @property
    def retry_after(self) -> int:
        """Seconds until the window resets (at least 1)."""
        return max(1, int((self.reset_at - timezone.now()).total_seconds()) + 1)


def _window(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0)


def _key(scope: str, key: str) -> str:
    full = f"{scope}:{key}"
    if len(full) <= _MAX_KEY:
        return full
    return f"{scope}:sha256:{hashlib.sha256(key.encode()).hexdigest()}"


class RateLimiter:
    """A named limit, such as ``RateLimiter("sign_in", limit_per_minute=20)``."""

    def __init__(self, scope: str, limit_per_minute: int) -> None:
        self.scope = scope
        self.limit = limit_per_minute

    def check(self, key: str) -> Decision:
        """Counts one request for ``key`` and says whether it is within the limit."""
        start = _window(timezone.now())
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO iki_rate_limit (key, window_start, count) VALUES (%s, %s, 1) "
                "ON CONFLICT (key, window_start) "
                "DO UPDATE SET count = iki_rate_limit.count + 1 RETURNING count",
                [_key(self.scope, key), start],
            )
            count = int(cursor.fetchone()[0])
        return Decision(
            allowed=count <= self.limit,
            limit=self.limit,
            remaining=max(0, self.limit - count),
            reset_at=start + WINDOW,
        )

    def allow(self, key: str) -> bool:
        return self.check(key).allowed

    def reset(self) -> None:
        RateLimitCounter.objects.filter(key__startswith=f"{self.scope}:").delete()


def prune() -> int:
    deleted, _ = RateLimitCounter.objects.filter(
        window_start__lt=timezone.now() - RETENTION
    ).delete()
    return deleted


def client_key(request: HttpRequest) -> str:
    # uvicorn resolves X-Forwarded-For from trusted proxies into REMOTE_ADDR.
    return str(request.META.get("REMOTE_ADDR", ""))
