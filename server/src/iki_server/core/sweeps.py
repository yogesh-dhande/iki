"""Housekeeping sweeps that end what was left behind, one item at a time.

Task runners sweep every few seconds for attempts whose worker went away, waits that no
scan's decision released, firings whose task was lost, and so on. Each item is ended in a
transaction of its own: one that can't be (a bug, a constraint it breaks) is rolled back,
logged with what it was, and left for the next pass, and the sweep goes on to the next.
So no single item can keep a sweep from ending the rest.
"""

from collections.abc import Callable, Iterable

import structlog
from django.db import connection, transaction

__all__ = ["each"]

logger = structlog.get_logger("iki.housekeeping")


def each[T](items: Iterable[T], end: Callable[[T], bool], *, failed: str, key: str) -> int:
    """Calls `end` on each of `items` in a transaction of its own, and returns how many it
    said it ended. One that raises is logged as `failed`, with the item as `key`, and
    skipped. Only a lost database connection stops the sweep: every later item would fail
    the same way, and the runner's housekeeping reconnects."""
    ended = 0
    for item in items:
        try:
            with transaction.atomic():
                if end(item):
                    ended += 1
        except Exception:
            logger.exception(failed, **{key: str(item)})
            if not connection.is_usable():
                raise
    return ended
