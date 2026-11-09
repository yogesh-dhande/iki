"""Identifiers.

Primary keys are UUIDv7 (RFC 9562): globally unique like UUIDv4, but
time-ordered, so new rows land at the end of B-tree indexes instead of at
random pages.
"""

import os
import time
import uuid

_RAND_A_BITS = 12
_RAND_B_BITS = 62


def uuid7() -> uuid.UUID:
    """Returns a new UUIDv7. Uses the standard library's on Python 3.14+."""
    native = getattr(uuid, "uuid7", None)
    if native is not None:
        return native()  # type: ignore[no-any-return]
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> (80 - _RAND_A_BITS)
    rand_b = rand & ((1 << _RAND_B_BITS) - 1)
    value = (
        (unix_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76  # version
        | rand_a << 64
        | 0b10 << 62  # RFC 9562 variant
        | rand_b
    )
    return uuid.UUID(int=value)
