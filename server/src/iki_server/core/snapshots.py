"""One consistent view of the database across many reads.

Each statement outside a transaction sees what's committed when it starts, so reading a
record's revisions after its history can see a revision whose history it read before
the edit that made it. Inside :func:`snapshot`, every read sees the database as it was at
the first one: a ``REPEATABLE READ`` transaction, read only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction


@contextmanager
def snapshot() -> Iterator[None]:
    """Reads inside see one moment of the database, and can't write. Started inside a
    transaction already, as a test's is, its reads are that transaction's."""
    outer = connection.in_atomic_block
    with transaction.atomic():
        if not outer:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield
