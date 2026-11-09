"""A small in-process rate limiter for unauthenticated endpoints.

Each web process counts on its own (fixed one-minute windows), so the
effective limit is per pod. It bounds what one client can make Iki do
before signing in: start sign-ins, exchange setup links, or send CSP reports.
"""

import threading
import time

from django.http import HttpRequest

_WINDOW_SECONDS = 60
_MAX_KEYS = 10_000


class RateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        self._lock = threading.Lock()
        self._window = 0
        self._counts: dict[str, int] = {}

    def allow(self, key: str) -> bool:
        window = int(time.monotonic() // _WINDOW_SECONDS)
        with self._lock:
            if window != self._window:
                self._window, self._counts = window, {}
            if key not in self._counts and len(self._counts) >= _MAX_KEYS:
                return False  # under a flood from many addresses, refuse rather than grow
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            return count <= self.limit

    def reset(self) -> None:
        with self._lock:
            self._counts = {}


def client_key(request: HttpRequest) -> str:
    # uvicorn resolves X-Forwarded-For from trusted proxies into REMOTE_ADDR.
    return str(request.META.get("REMOTE_ADDR", ""))
