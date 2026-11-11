"""Identifiers.

Primary keys are UUIDv7 (RFC 9562): globally unique like UUIDv4, but
time-ordered, so new rows land at the end of B-tree indexes instead of at
random pages. Within a process they also strictly increase, even within a
millisecond, so lists can page by them.
"""

import os
import threading
import time
import uuid

_COUNTER_BITS = 42  # rand_a (12 bits) and the top 30 of rand_b, as Python 3.14 does
_RANDOM_BITS = 32
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def _fresh_counter() -> int:
    # The top bit stays clear, leaving room to count up within a millisecond.
    return int.from_bytes(os.urandom(6), "big") & (_COUNTER_MAX >> 1)


def uuid7() -> uuid.UUID:
    """Returns a new UUIDv7. Uses the standard library's on Python 3.14+."""
    native = getattr(uuid, "uuid7", None)
    if native is not None:
        return native()  # type: ignore[no-any-return]
    global _last_ms, _counter
    with _lock:
        unix_ms = time.time_ns() // 1_000_000
        if unix_ms > _last_ms:
            _counter = _fresh_counter()
        else:
            # The same millisecond, or the clock went back: count on from the last.
            unix_ms = _last_ms
            _counter += 1
            if _counter > _COUNTER_MAX:
                unix_ms += 1
                _counter = _fresh_counter()
        _last_ms = unix_ms
        counter = _counter
    rand_a = counter >> (_COUNTER_BITS - 12)
    rand_b = (counter & ((1 << 30) - 1)) << _RANDOM_BITS | int.from_bytes(os.urandom(4), "big")
    value = (
        (unix_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76  # version
        | rand_a << 64
        | 0b10 << 62  # RFC 9562 variant
        | rand_b
    )
    return uuid.UUID(int=value)
